"""端到端集成测试（不需要 Ollama）。用真实向量库 / 注册表 / RAG 服务，只把 LLM
换成可控假模型：入库→切分→向量化→FAISS→注册表、检索召回、流式事件序列、
推理链剥离、引用编号与兜底回答。

    .venv\\Scripts\\python.exe tests\\test_e2e.py
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 本套件会 clear_all()，必须跑在临时数据目录上，否则会把用户文档和索引全删掉。
# 必须在导入 app.* 之前设置 —— 路径在 app.core.paths 导入时就固定了。
os.environ["RAG_DATA_DIR"] = str(TMP_ROOT / "data_e2e")

from app.config import Settings, settings  # noqa: E402
from app.core import browser  # noqa: E402
from app.core.paths import DATA_DIR, UPLOADS_DIR  # noqa: E402
from app.services import rag  # noqa: E402
from app.services import vectorstore as vector_store_module  # noqa: E402
from app.services.ingest import clear_all, ingest_path, reindex_all  # noqa: E402
from app.services.registry import registry  # noqa: E402
from app.services.vectorstore import vector_store  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))


def section(title: str) -> None:
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


# ---------------------------------------------------------------------------
# 冷启动 / 自动打开浏览器
# ---------------------------------------------------------------------------
def _recorder(sink: list[str]) -> Callable[[str], bool]:
    """构造一个假的浏览器打开器：记录 URL，返回 True。"""

    def record(url: str) -> bool:
        sink.append(url)
        return True

    return record


def _check_cold_start() -> None:
    """``import app.main`` 不能拉起重量级依赖（否则端口 10 秒后才监听）。"""
    report = TMP_ROOT / "cold_start_probe.json"
    if report.exists():
        report.unlink()

    code = (
        "import json, sys, time\n"
        "t = time.perf_counter()\n"
        "import app.main\n"
        "elapsed = time.perf_counter() - t\n"
        "heavy = [m for m in ('sentence_transformers', 'transformers', 'torch',\n"
        "                     'langchain_text_splitters', 'faiss',\n"
        "                     'langchain_community') if m in sys.modules]\n"
        f"open(r'{report}', 'w', encoding='utf-8').write("
        "json.dumps({'elapsed': elapsed, 'heavy': heavy}))\n"
    )

    try:
        # 不用管道捕获输出，子进程直接写文件更稳
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BACKEND_DIR),
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            timeout=180,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001
        check("子进程能导入 app.main", False, f"{type(exc).__name__}: {str(exc)[:100]}")
        return

    payload = json.loads(report.read_text(encoding="utf-8"))
    elapsed = float(payload["elapsed"])
    heavy = list(payload["heavy"])

    check("导入 app.main 不拉起重量级依赖", not heavy, str(heavy))
    check("导入 app.main 耗时 < 6s", elapsed < 6.0, f"{elapsed:.2f}s")


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """只为就绪探测服务：``/`` 返回 200，其它路径返回 404。"""

    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        status = 200 if self.path == "/" else 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


async def _check_browser_open() -> None:
    """就绪探测 + 打开时机 + 配置来源。"""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    live = f"http://127.0.0.1:{httpd.server_address[1]}/"

    # 绑定了但不 listen 的端口：连接会被立刻拒绝，比「随便挑个空闲端口」确定
    dead_sock = socket.socket()
    dead_sock.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{dead_sock.getsockname()[1]}/"

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        check("probe：200 视为就绪", browser.probe(live) is True)
        check("probe：404 不算就绪", browser.probe(live + "missing") is False)
        check("probe：未监听端口返回 False", browser.probe(dead) is False)

        elapsed = await browser.wait_until_ready(live, timeout=5.0, interval=0.05)
        check("wait_until_ready 命中就绪地址", elapsed is not None, f"{elapsed}")
        missed = await browser.wait_until_ready(dead, timeout=0.6, interval=0.05)
        check("wait_until_ready 超时返回 None", missed is None, str(missed))

        calls: list[str] = []
        ok = browser.open_browser(live, opener=_recorder(calls))
        check("open_browser 走注入的打开器", ok and calls == [live], str(calls))

        check("未配置地址时不打开浏览器", browser.schedule_auto_open("") is None)
        check("显式禁用时不打开浏览器",
              browser.schedule_auto_open(live, enabled=False) is None)

        # 完整路径：schedule_auto_open -> 轮询探测 -> 打开
        opened: list[str] = []
        browser._open_url = _recorder(opened)
        try:
            task = browser.schedule_auto_open(live)
            check("配置了地址就调度任务", task is not None)
            if task is not None:
                await asyncio.wait_for(task, timeout=15)
        finally:
            browser._open_url = None
        check("确认就绪后才打开浏览器", opened == [live], str(opened))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        dead_sock.close()

    os.environ["RAG_OPEN_BROWSER_URL"] = live
    os.environ["RAG_AUTO_OPEN_BROWSER"] = "0"
    try:
        fresh = Settings(_env_file=None)
        check("RAG_OPEN_BROWSER_URL 映射到配置项",
              fresh.open_browser_url == live, fresh.open_browser_url)
        check("RAG_AUTO_OPEN_BROWSER=0 映射到配置项",
              fresh.auto_open_browser is False, str(fresh.auto_open_browser))
    finally:
        os.environ.pop("RAG_OPEN_BROWSER_URL", None)
        os.environ.pop("RAG_AUTO_OPEN_BROWSER", None)

    default = Settings(_env_file=None)
    check("默认不自动打开浏览器", default.open_browser_url == "", repr(default.open_browser_url))
    check("默认允许自动打开（地址为空时同样不会开）",
          default.auto_open_browser is True, str(default.auto_open_browser))


def _check_ollama_payload() -> None:
    """便携版 Ollama 的完整性判定：只看推理引擎文件在不在。"""
    from app.services.setup import portable_ollama_status

    root = TMP_ROOT / "portable_probe"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    runtime = root / "lib" / "ollama"

    try:
        root.mkdir(parents=True, exist_ok=True)
        missing = portable_ollama_status(root)
        check("未安装时 present=False 且不下结论",
              missing["present"] is False and missing["complete"] is None, str(missing["complete"]))

        (root / "ollama.exe").write_bytes(b"MZ")
        broken = portable_ollama_status(root)
        check("只有 ollama.exe 判为不完整",
              broken["present"] is True and broken["complete"] is False,
              f"runtime_files={broken['runtime_files']}")

        runtime.mkdir(parents=True, exist_ok=True)
        check("运行时目录为空判为不完整", portable_ollama_status(root)["complete"] is False)

        (runtime / "llama-server.exe").write_bytes(b"MZ")
        check("有 llama-server.exe 判为完整", portable_ollama_status(root)["complete"] is True)

        (runtime / "llama-server.exe").unlink()
        for name in ("ggml.dll", "ggml-base.dll", "ggml-cpu.dll"):
            (runtime / name).write_bytes(b"MZ")
        check("引擎改名但运行时文件齐全时不误报",
              portable_ollama_status(root)["complete"] is True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 本机实际状态：只报告、不断言
    live = portable_ollama_status()
    if live["present"] and live["complete"] is False:
        print(
            f"  [INFO] 本机内置 Ollama 不完整：{live['runtime_dir']} 下只有 "
            f"{live['runtime_files']} 个文件（提问会失败，重跑 scripts\\prepare.cmd 可自动修复）"
        )
    elif live["present"]:
        print(f"  [INFO] 本机内置 Ollama 完整（运行时文件 {live['runtime_files']} 个）")


# ---------------------------------------------------------------------------
# 假 LLM：把推理链标签切碎成多个 chunk，验证解析器的缓冲逻辑
# ---------------------------------------------------------------------------
class FakeChunk:
    def __init__(self, content, thinking=None, meta=None, thinking_native=None):
        self.content = content
        self.additional_kwargs = {}
        if thinking:
            self.additional_kwargs["thinking"] = thinking
        # 原生 thinking 通道：langchain-ollama 1.x 用的键名是 reasoning_content
        if thinking_native:
            self.additional_kwargs["reasoning_content"] = thinking_native
        self.response_metadata = meta or {}


class FakeLLM:
    def __init__(self, pieces):
        self.pieces = pieces
        self.calls = 0
        self.last_messages = None

    async def astream(self, messages):
        self.calls += 1
        self.last_messages = messages
        for index, piece in enumerate(self.pieces):
            if isinstance(piece, FakeChunk):
                yield piece
            else:
                meta = {"eval_count": 42, "done_reason": "stop"} if index == len(self.pieces) - 1 else {}
                yield FakeChunk(piece, meta=meta)


CORPUS = [
    (
        "travel.txt",
        "《员工差旅费用报销管理办法》\n\n"
        "第一章 总则。为规范公司差旅费用管理，控制差旅成本，特制定本办法。"
        "本办法适用于公司全体正式员工因公出差产生的交通、住宿、餐饮等费用报销。"
        "员工出差前应通过 OA 系统提交出差申请，经直属主管审批后方可成行。\n\n"
        "第二章 交通费标准。市内交通费每日上限 80 元，凭票据实报销。"
        "城市间交通优先选择高铁二等座；确需乘坐飞机的，需提前三个工作日申请。"
        "出差期间产生的停车费、过路费凭票报销，单次不超过 200 元。\n\n"
        "第三章 住宿费标准。住宿费一线城市每晚 600 元，其他城市每晚 400 元。"
        "一线城市指北京、上海、广州、深圳。住宿费按实际发生额报销，"
        "超出上述标准的部分需由部门负责人书面审批后方可报销。"
        "同性别员工同行原则上应合住标准间。\n\n"
        "第四章 餐饮补贴。出差期间每人每日餐饮补贴 100 元，按自然日计算，"
        "不足一日的按半日计发。已由公司统一安排用餐的，不再重复发放补贴。\n\n"
        "第五章 报销流程。员工应在出差结束后十五个工作日内提交报销单，"
        "附全部原始票据。财务部在收到完整单据后十个工作日内完成审核与付款。"
        "票据不全或超标的，财务部有权退回补充说明。",
    ),
    (
        "leave.txt",
        "《员工休假管理制度》\n\n"
        "第一条 年假天数。员工入职满一年享有 5 天带薪年假，满三年享有 10 天，"
        "满五年享有 15 天，满十年享有 20 天。年假天数按司龄逐年递增，"
        "当年度未休完的年假最多可结转 5 天至次年第一季度。\n\n"
        "第二条 申请流程。年假需提前三个工作日通过 OA 系统申请，"
        "经直属主管审批后生效。连续休假超过五个工作日的，需报部门负责人审批。"
        "因项目紧急无法休假的，经批准可延后至项目结束后两个月内使用。\n\n"
        "第三条 病假。员工因病需要休假的，应提供二级以上医院出具的诊断证明。"
        "病假期间工资按国家及当地有关规定执行。连续病假超过三十日的，"
        "需提交复工体检报告。\n\n"
        "第四条 婚假与产假。员工依法办理结婚登记的，享有婚假十天。"
        "女员工生育享有产假一百五十八天，男员工享有陪产假十五天。"
        "上述假期均需提前十个工作日提出申请。\n\n"
        "第五条 加班调休。员工在法定节假日加班的，按国家规定支付加班工资；"
        "在休息日加班的，优先安排同等时间的调休。调休需在三个月内使用完毕。",
    ),
]


def prepare_corpus() -> list:
    # 语料直接落在 UPLOADS_DIR 里：贴近真实上传路径，也让「重建索引」有文件可用
    work = UPLOADS_DIR
    work.mkdir(parents=True, exist_ok=True)

    infos = []
    for name, text in CORPUS:
        path = work / name
        # 反复堆叠同一段内容，确保长度足够切出有意义的分块
        path.write_text((text + "\n\n") * 3, encoding="utf-8")
        info, duplicated = ingest_path(path, name, path.stat().st_size)
        infos.append((info, duplicated))
    return infos


async def main() -> int:
    print("=" * 70)
    print("端到端集成测试（RAG 核心，使用假 LLM）")
    print("=" * 70)

    # 数据目录隔离自检：不通过就立刻停手，绝不碰真实知识库
    if DATA_DIR == PROJECT_ROOT / "data":
        print("\n[FAIL] 数据目录隔离失效：本套件会清空知识库，拒绝在真实 data/ 上运行")
        return 1
    print(f"数据目录（隔离）: {DATA_DIR}")

    clear_all()

    try:
        # ------------------------------------------------------------------
        section("1. 文档入库与持久化")
        infos = prepare_corpus()
        check("入库 2 个文档", len(infos) == 2)
        check("均非重复", all(not dup for _, dup in infos))

        total_chunks = sum(info.chunk_count for info, _ in infos)
        check("生成分块", total_chunks > 0, f"{total_chunks} 块")
        check("FAISS 向量数与分块数一致", vector_store.size == total_chunks,
              f"ntotal={vector_store.size}")
        check("索引文件已落盘", vector_store.exists_on_disk())
        check("注册表已记录", len(registry.all()) == 2)
        check("注册表统计一致", registry.stats()["chunk_count"] == total_chunks)

        # 重复上传同一内容应被识别
        work = UPLOADS_DIR
        dup_path = work / "travel_again.txt"
        dup_path.write_text((CORPUS[0][1] + "\n\n") * 3, encoding="utf-8")
        _, duplicated = ingest_path(dup_path, "travel_again.txt", dup_path.stat().st_size)
        check("重复内容被识别复用", duplicated)
        check("重复上传未改变向量数", vector_store.size == total_chunks)

        # ------------------------------------------------------------------
        section("2. 检索")
        # 用接近原文的措辞提问：本测试验证的是检索管道，不该依赖模型的语义泛化能力
        results = vector_store.search("一线城市住宿费每晚 600 元", top_k=3, score_threshold=0.0)
        check("检索有结果", len(results) >= 1, f"{len(results)} 条")
        check("Top1 来自差旅文档", bool(results) and results[0].filename == "travel.txt",
              results[0].filename if results else "无")
        check("引用序号从 1 开始连续",
              [r.index for r in results] == list(range(1, len(results) + 1)))
        check("结果按分数降序",
              all(results[i].score >= results[i + 1].score for i in range(len(results) - 1)))
        check("召回结果中包含关键数字",
              any("600" in r.content for r in results),
              f"{len(results)} 条命中")

        # 按文档过滤
        travel_id = infos[0][0].doc_id
        filtered = vector_store.search("年假", top_k=3, score_threshold=0.0, doc_ids=[travel_id])
        check("按文档过滤生效", all(r.doc_id == travel_id for r in filtered),
              f"{len(filtered)} 条")

        # ------------------------------------------------------------------
        section("3. 提示词装配")
        sources = vector_store.search("一线城市住宿费每晚 600 元", top_k=2, score_threshold=0.0)
        context = rag.build_context(sources)
        check("上下文含编号与文件名", "[1]" in context and "travel.txt" in context)
        check("上下文不超上限", len(context) <= settings.max_context_chars + 200,
              f"{len(context)} 字符")

        messages = rag.build_messages("住宿费能报多少？", sources, [])
        check("消息含 system + human", len(messages) == 2)
        check("system 含参考资料", "参考资料" in messages[0].content)

        # ------------------------------------------------------------------
        section("4. 推理链解析（逐字符打碎标签）")
        open_tag = rag.THINK_OPEN
        close_tag = rag.THINK_CLOSE
        pieces = [
            open_tag[:3],          # 半个开标签
            open_tag[3:],          # 剩下半个
            "先看文档里关于住宿的条款。",
            "一线城市 600 元。",
            close_tag[:4],         # 半个闭标签
            close_tag[4:],         # 剩下半个
            "根据文档，住宿费一线城市每晚 600 元 [1]，其他城市 400 元 [1]。",
        ]
        fake = FakeLLM(pieces)
        original_get_llm = rag.get_llm
        rag.get_llm = lambda: fake  # type: ignore[assignment]

        try:
            events = []
            async for event in rag.rag_service.stream("住宿费一线城市每晚多少钱？", top_k=2):
                events.append(event)
        finally:
            rag.get_llm = original_get_llm  # type: ignore[assignment]

        names = [e["event"] for e in events]
        check("首个事件是 meta", names and names[0] == "meta", names[0] if names else "无")
        check("包含 token 事件", "token" in names)
        check("包含 thinking 事件", "thinking" in names)
        check("包含 sources 事件", "sources" in names)
        check("以 done 结尾", names and names[-1] == "done")
        check("无 error 事件", "error" not in names)

        answer = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
        thinking = "".join(e["data"]["delta"] for e in events if e["event"] == "thinking")

        check("正文不含开标签", open_tag not in answer, repr(answer[:60]))
        check("正文不含闭标签", close_tag not in answer, repr(answer[:60]))
        check("正文保留完整答案", "600" in answer and "[1]" in answer, repr(answer))
        check("推理链内容被正确剥离", "先看文档里关于住宿的条款" in thinking,
              repr(thinking[:60]))
        check("推理链不含闭标签残留", close_tag not in thinking)

        sources_event = next(e for e in events if e["event"] == "sources")
        check("sources 事件带回引用片段", len(sources_event["data"]["sources"]) >= 1)
        check("cited 提取出 [1]", sources_event["data"]["cited"] == [1],
              str(sources_event["data"]["cited"]))

        done = events[-1]["data"]
        check("done 含耗时", isinstance(done.get("elapsed_ms"), int))
        check("done 含 token 用量", done.get("usage", {}).get("eval_count") == 42,
              str(done.get("usage")))

        # additional_kwargs 通道的推理链
        fake2 = FakeLLM([
            FakeChunk("", thinking="这是来自 thinking 字段的推理"),
            FakeChunk("最终答案 [1]。"),
        ])
        rag.get_llm = lambda: fake2  # type: ignore[assignment]
        try:
            events2 = [e async for e in rag.rag_service.stream("问题", top_k=1)]
        finally:
            rag.get_llm = original_get_llm  # type: ignore[assignment]
        thinking2 = "".join(e["data"]["delta"] for e in events2 if e["event"] == "thinking")
        check("支持 message.thinking 通道", "thinking 字段" in thinking2, repr(thinking2))

        # ------------------------------------------------------------------
        section("5. 无召回兜底")
        fake3 = FakeLLM(["这段不应被生成"])
        rag.get_llm = lambda: fake3  # type: ignore[assignment]
        try:
            events3 = [
                e async for e in rag.rag_service.stream(
                    "量子色动力学的渐近自由如何证明", top_k=2, score_threshold=0.99
                )
            ]
        finally:
            rag.get_llm = original_get_llm  # type: ignore[assignment]

        answer3 = "".join(e["data"]["delta"] for e in events3 if e["event"] == "token")
        check("高阈值下不调用 LLM", fake3.calls == 0, f"调用 {fake3.calls} 次")
        check("返回兜底话术", "无法回答" in answer3)
        check("兜底标记正确",
              events3[-1]["data"].get("fallback") is True)

        # ------------------------------------------------------------------
        section("6. 概览检索、阈值兜底与近重复去重")
        # 防「总结类提问被阈值筛光」：概览模式改成按全篇均匀取样（换概览提示词），
        # 阈值内无命中则自动放宽并标记 relaxed，近重复片段只留分数最高的一条。
        from app.services.rag import detect_intent

        check("「用三句话总结这些文档的主要内容」→ 概览意图",
              detect_intent("用三句话总结这些文档的主要内容") == "overview")
        check("「总结一下」→ 概览意图", detect_intent("总结一下") == "overview")
        check("具体问题仍然是问答意图",
              detect_intent("一线城市住宿费每晚多少钱") == "qa")

        overview_sources, overview_info = vector_store.overview_chunks(
            "总结这些文档的主要内容", doc_ids=None, limit=8
        )
        check("概览模式有召回", len(overview_sources) >= 4, f"{len(overview_sources)} 段")
        check("概览模式标记 mode=overview", overview_info.get("mode") == "overview")
        check("概览模式不做阈值过滤",
              overview_info.get("effective_threshold") == 0.0,
              str(overview_info.get("effective_threshold")))
        overview_names = {item.filename for item in overview_sources}
        check("多文档概览覆盖到每一篇文档", len(overview_names) >= 2,
              " / ".join(sorted(overview_names)))
        check("概览取样带来源编号与页码",
              all(item.index >= 1 and item.filename for item in overview_sources))
        check("概览取样按文档分配名额（不是全给长文档）",
              all(value >= 1 for value in (overview_info.get("quotas") or {}).values()),
              str(overview_info.get("quotas")))

        # 概览问题必须换用概览提示词，否则模型会只抓一个片段复述
        overview_prompt = rag.build_messages("总结一下", overview_sources, [], mode="overview")
        check("概览模式使用概览提示词",
              "文档片段" in overview_prompt[0].content
              and "整体" in overview_prompt[0].content)
        check("概览提示词要求用无序列表（避免编号错乱）",
              "- **要点名称**" in overview_prompt[0].content)
        qa_prompt = rag.build_messages("住宿费多少", overview_sources, [], mode="qa")
        check("问答模式仍使用问答提示词", "参考资料" in qa_prompt[0].content)

        # 流式链路：meta 事件要把 mode / relaxed 带给前端
        fake_overview = FakeLLM(["文档整体讲了两件事 [1]。"])
        original_get_llm = rag.get_llm
        rag.get_llm = lambda: fake_overview  # type: ignore[assignment]
        try:
            events_overview = [
                e async for e in rag.rag_service.stream("总结一下这些文档的主要内容", top_k=2)
            ]
        finally:
            rag.get_llm = original_get_llm  # type: ignore[assignment]
        meta_overview = next(e for e in events_overview if e["event"] == "meta")["data"]
        check("meta 事件声明 mode=overview", meta_overview.get("mode") == "overview")
        check("概览模式召回数量不受 top_k 限制",
              meta_overview.get("source_count", 0) > 2,
              f"top_k=2 实召回 {meta_overview.get('source_count')} 段")
        check("概览模式 meta 带 chunks_total",
              isinstance(meta_overview.get("chunks_total"), int)
              and meta_overview["chunks_total"] > 0)

        # 阈值兜底：阈值内一条都没有时给「低置信度答案 + 标记」，不回「无法回答」
        probe_question = "量子色动力学的渐近自由如何证明"
        _all_hits, baseline_info = vector_store.search_detailed(
            probe_question, top_k=3, score_threshold=0.0
        )
        best_score = float(baseline_info.get("best_score") or 0.0)
        # 本语料只有两篇、主题集中，任何中文提问的最佳相似度都在 0.4 上下，
        # 高于默认放宽上限（0.35）；这里临时抬高上限以验证「放宽」机制本身。
        saved_limit = settings.score_relax_limit
        settings.score_relax_limit = min(0.99, round(best_score + 0.05, 4))
        try:
            strict = round(best_score + 0.02, 4)
            relaxed_sources, relaxed_info = vector_store.search_detailed(
                probe_question, top_k=3, score_threshold=strict
            )
            check("严格阈值下自动放宽而不是空手而归",
                  len(relaxed_sources) >= 1,
                  f"阈值 {strict} / 最佳 {best_score}")
            check("放宽被标记出来", relaxed_info.get("relaxed") is True)
            check("放宽后的生效阈值低于用户设定值",
                  relaxed_info.get("effective_threshold", 1.0) < strict,
                  f"{relaxed_info.get('effective_threshold')} < {strict}")
        finally:
            settings.score_relax_limit = saved_limit

        extreme_sources, extreme_info = vector_store.search_detailed(
            probe_question, top_k=3, score_threshold=0.99
        )
        check("阈值高到 0.99 时不擅自放宽",
              not extreme_sources and extreme_info.get("relaxed") is False)

        # 相对窗口：只保留与最佳片段相差不超过 score_window 的候选
        wide_sources, wide_info = vector_store.search_detailed(
            "一线城市住宿费每晚 600 元", top_k=6, score_threshold=0.0
        )
        windowed_sources, windowed_info = vector_store.search_detailed(
            "一线城市住宿费每晚 600 元", top_k=6
        )
        if len(wide_sources) > 1:
            floor = wide_info["best_score"] - settings.score_window - 0.001
            check("相对窗口裁掉了远低于最佳的片段",
                  all(item.score >= floor for item in windowed_sources),
                  f"窗口下界 {floor:.3f}，实际最低 "
                  f"{min((item.score for item in windowed_sources), default=0):.3f}")
            check("阈值设 0 时不做窗口裁剪（调用方明确要求不过滤）",
                  len(wide_sources) >= len(windowed_sources),
                  f"{len(wide_sources)} vs {len(windowed_sources)}")
        else:
            check("相对窗口场景可构造", False, f"命中过少：{len(wide_sources)}")

        # 近重复去重：同一段文本被重复切进多个 chunk 时只保留分数最高的一条
        from langchain_core.documents import Document
        duplicated = [
            (Document(page_content="页眉样板文本 " + "住宿费一线城市每晚 600 元。" * 8), 0.61),
            (Document(page_content="页眉样板文本 " + "住宿费一线城市每晚 600 元。" * 8), 0.60),
            (Document(page_content="年假满一年 5 天，满三年 10 天，满五年 15 天。"), 0.30),
        ]
        kept_pairs, dropped_pairs = vector_store_module._dedupe_pairs(duplicated, 0.8)
        check("近乎相同的片段只保留一条", dropped_pairs == 1 and len(kept_pairs) == 2,
              f"保留 {len(kept_pairs)} / 丢弃 {dropped_pairs}")
        check("保留的是分数更高的那条", kept_pairs[0][1] == 0.61)
        check("检索结果里会报告去重数量",
              "dropped_duplicates" in windowed_info,
              str(windowed_info.get("dropped_duplicates")))

        # 重建索引：解析/切分逻辑升级后，旧索引必须能按原文件重跑
        before_chunks = registry.stats()["chunk_count"]
        report = reindex_all()
        check("重建覆盖所有文档", report["rebuilt"] == len(infos) and report["failed"] == 0,
              f"重建 {report['rebuilt']} / 失败 {report['failed']}")
        check("重建后向量数与注册表一致",
              vector_store.size == registry.stats()["chunk_count"],
              f"faiss={vector_store.size} registry={registry.stats()['chunk_count']}")
        check("重建后分块数不变（TXT 解析逻辑未变）",
              report["chunks_after"] == before_chunks,
              f"{before_chunks} -> {report['chunks_after']}")

        # ------------------------------------------------------------------
        section("7. 索引持久化与重载")
        vector_store.persist()
        vector_store._store = None  # 模拟进程重启
        reloaded = vector_store.load(force=True)
        check("索引可重新载入", reloaded is not None)
        check("重载后向量数不变", vector_store.size == total_chunks,
              f"{vector_store.size} vs {total_chunks}")
        results_after = vector_store.search("年假有多少天", top_k=2, score_threshold=0.0)
        check("重载后可正常检索", len(results_after) >= 1,
              results_after[0].filename if results_after else "无")

        # ------------------------------------------------------------------
        section("8. 文档删除")
        removed = vector_store.delete_ids(
            [c["faiss_id"] for c in vector_store.chunks_of(travel_id, limit=999)]
        )
        check("按文档删除向量", removed > 0, f"删除 {removed} 个")
        check("向量总数减少", vector_store.size == total_chunks - removed,
              f"{vector_store.size}")

        # ------------------------------------------------------------------
        section("9. 安装日志分段解析")
        # 纯函数，直接断言：进度行（\r 结尾）与普通行（\n 结尾）必须区分开，
        # 否则一次 1.4GB 下载能刷出上千行 "0 0 0 0 0"，把真正的错误冲出视野。
        from app.services.installer import Installer

        segments = Installer._parse_segments("普通行\n进度1\r进度2\r普通行2\n".encode("utf-8"))
        kinds = [(s["text"], s["progress"]) for s in segments]
        check("普通行识别为 progress=False", kinds[0] == ("普通行", False), str(kinds[0]))
        check("回车结尾识别为 progress=True",
              kinds[1] == ("进度1", True) and kinds[2] == ("进度2", True), str(kinds[1:3]))
        check("行序与终止符对应", kinds[3] == ("普通行2", False), str(kinds[3]))

        multi = Installer._parse_segments("中文内容：完成\n".encode("utf-8"))
        check("多字节字符完整", multi[0]["text"] == "中文内容：完成", multi[0]["text"])

        collapsed = Installer._collapse_progress(segments)
        progress_texts = [s["text"] for s in collapsed if s["progress"]]
        check("回放时折叠进度帧", progress_texts == ["进度2"], str(progress_texts))

        probe = Installer()
        many = Installer._parse_segments(
            "".join(f"frame{i}\r" for i in range(30)).encode("utf-8")
        )
        throttled = probe._throttle_progress(many)
        check("进度帧被节流", len(throttled) < len(many), f"{len(throttled)} / {len(many)}")
        check("最后一条进度必定发出",
              bool(throttled) and throttled[-1]["text"] == "frame29",
              throttled[-1]["text"] if throttled else "空")

        # ------------------------------------------------------------------
        section("10. 冷启动与自动打开浏览器")
        # 用户实测现象：双击启动后浏览器先显示「127.0.0.1 拒绝连接」，
        # 几秒后才正常。原因是脚本按固定延时开浏览器，而后端冷启动要 9 秒。
        # 这里把两个根因都钉死：导入链必须轻，开浏览器的时机必须由实际探测定。
        _check_cold_start()
        await _check_browser_open()

        # ------------------------------------------------------------------
        section("11. Ollama 安装完整性检测")
        _check_ollama_payload()

        # ------------------------------------------------------------------
        section("12. 原生 thinking 通道（reasoning_content）")
        # 防「点了发送很久没反应」：原生 thinking 通道下思维链不在 content 里，
        # 而在 additional_kwargs["reasoning_content"]，只认 thinking 就会静默丢弃。
        native = FakeLLM([
            FakeChunk("", thinking_native="我先看看文档里的住宿条款。", meta={"eval_count": 12}),
            FakeChunk("根据文档，住宿费一线城市每晚 600 元 [1]。",
                      meta={"eval_count": 42, "done_reason": "stop"}),
        ])
        original_get_llm = rag.get_llm
        rag.get_llm = lambda: native  # type: ignore[assignment]
        try:
            events = [e async for e in rag.rag_service.stream("住宿费多少？", top_k=2)]
        finally:
            rag.get_llm = original_get_llm  # type: ignore[assignment]

        think_text = "".join(e["data"]["delta"] for e in events if e["event"] == "thinking")
        token_text = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
        check("原生通道内容变成 thinking 事件", "住宿条款" in think_text, think_text[:40])
        check("原生通道内容不混进正文", "住宿条款" not in token_text, token_text[:40])
        check("正文仍然完整", "600" in token_text, token_text[:60])
        sources_event = next(e for e in events if e["event"] == "sources")
        check("sources 事件带回原生推理链",
              "住宿条款" in (sources_event["data"]["thinking"] or ""),
              str(sources_event["data"]["thinking"])[:40])

        # ------------------------------------------------------------------
        section("13. 概览取样只给用到的片段打分（不再全库扫两遍）")
        # 防「大库上概览取样白白扫全库」：旧实现做一次 O(n·d) 全量扫描，
        # 现在对目标片段做 reconstruct（O(k·d)）。这里包一层计数器断言。
        class _GuardedIndex:
            def __init__(self, index):
                self._index = index
                self.searches = 0
                self.reconstructs = 0

            def search(self, *args, **kwargs):
                self.searches += 1
                return self._index.search(*args, **kwargs)

            def reconstruct(self, *args, **kwargs):
                self.reconstructs += 1
                return self._index.reconstruct(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._index, name)

        store = vector_store.ensure_loaded()
        original_index = store.index
        guarded = _GuardedIndex(original_index)
        store.index = guarded
        try:
            ov_sources, ov_info = vector_store.overview_chunks("总结这些文档的主要内容", limit=4)
        finally:
            store.index = original_index

        check("概览取样没有触发全库扫描", guarded.searches == 0,
              f"index.search 被调用 {guarded.searches} 次")
        check("只为取到的片段按需算分",
              guarded.reconstructs == len(ov_sources) and len(ov_sources) > 0,
              f"reconstruct {guarded.reconstructs} 次 / 取样 {len(ov_sources)} 段")
        check("按需算出的分数是真实相似度（不是 0）",
              all(item.score > 0 for item in ov_sources),
              str([round(item.score, 3) for item in ov_sources]))

        # 与「全库扫描」的旧算法逐条比对，确保优化没有改变分数
        import numpy as np

        from app.services.embeddings import get_embeddings

        probe = "总结这些文档的主要内容"
        probe_vector = get_embeddings().embed_query(probe)
        scan_scores, scan_indices = original_index.search(
            np.asarray(probe_vector, dtype="float32").reshape(1, -1), vector_store.size
        )
        expected = {
            store.index_to_docstore_id[int(index)]: float(score)
            for score, index in zip(scan_scores[0], scan_indices[0])
            if int(index) >= 0
        }
        entries = vector_store._ordered_entries(None)
        actual = vector_store._score_map(probe_vector, entries)
        deltas = [abs(actual[entry.docstore_id] - expected[entry.docstore_id]) for entry in entries]
        check("按需取分与全库扫描结果逐条一致（数值等价）",
              bool(entries) and len(actual) == len(entries) and max(deltas) < 1e-4,
              f"{len(actual)}/{len(entries)} 条，最大偏差 {max(deltas, default=0):.2e}")

    finally:
        # ------------------------------------------------------------------
        section("清理")
        clear_all()
        check("知识库已清空", registry.stats()["document_count"] == 0)

    print()
    print("=" * 70)
    print(f"结果：通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 70)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
