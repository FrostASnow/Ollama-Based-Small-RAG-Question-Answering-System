"""端到端集成测试（不需要 Ollama）。

用真实的向量库、真实的注册表、真实的 RAG 服务，只把 LLM 换成一个可控的假模型，
从而在**没有 Ollama** 的情况下也能验证：

    * 文档入库 → 切分 → 向量化 → FAISS 持久化 → 注册表
    * 检索是否按预期召回
    * 流式事件序列（meta / thinking / token / sources / done）
    * 推理链标签在**逐 token 被打碎**时能否被正确剥离
    * 引用编号 [n] 的提取
    * 无召回时的兜底回答

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

from app.config import Settings, settings  # noqa: E402
from app.core import browser  # noqa: E402
from app.services import rag  # noqa: E402
from app.services.ingest import clear_all, ingest_path  # noqa: E402
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
    """``import app.main`` 不能拉起重量级依赖。

    实测：``langchain_text_splitters`` 的包 ``__init__`` 会连带导入整个
    ``sentence_transformers``（含 torch 与全部 loss/trainer），单这一条就是
    9.5 秒。它挂在 ``app.main -> routers.documents -> services.loader`` 上，
    于是端口要 10 秒后才开始监听 —— 用户先看到的是浏览器
    「127.0.0.1 拒绝连接」。本用例把「导入链必须轻」钉死。
    """
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
        # 不用管道捕获输出：受限环境下管道可能不可用，子进程直接写文件更稳
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
    """便携版 Ollama 的完整性判定。

    真实故障：解压中断后只留下一个 ollama.exe，此时
    ``ollama serve`` 能启动、``/api/tags`` 能列出模型，一切看起来都正常，
    但一发提问就 500（llama-server binary not found）。
    所以判定必须落在「推理引擎文件在不在」上。
    """
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

    # 本机实际状态：只报告、不断言 —— 环境问题不该让代码测试变红
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
    work = TMP_ROOT / "e2e_corpus"
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

    # 保证从干净状态开始
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
        work = TMP_ROOT / "e2e_corpus"
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
        section("6. 索引持久化与重载")
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
        section("7. 文档删除")
        removed = vector_store.delete_ids(
            [c["faiss_id"] for c in vector_store.chunks_of(travel_id, limit=999)]
        )
        check("按文档删除向量", removed > 0, f"删除 {removed} 个")
        check("向量总数减少", vector_store.size == total_chunks - removed,
              f"{vector_store.size}")

        # ------------------------------------------------------------------
        section("8. 安装日志分段解析")
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
        section("9. 冷启动与自动打开浏览器")
        # 用户实测现象：双击启动后浏览器先显示「127.0.0.1 拒绝连接」，
        # 几秒后才正常。原因是脚本按固定延时开浏览器，而后端冷启动要 9 秒。
        # 这里把两个根因都钉死：导入链必须轻，开浏览器的时机必须由实际探测定。
        _check_cold_start()
        await _check_browser_open()

        # ------------------------------------------------------------------
        section("10. Ollama 安装完整性检测")
        _check_ollama_payload()

        # ------------------------------------------------------------------
        section("11. 原生 thinking 通道（reasoning_content）")
        # Ollama 的原生 thinking 通道下，思维链不在 content 里，而是由
        # langchain-ollama 放进 additional_kwargs["reasoning_content"]。
        # 只认 additional_kwargs["thinking"] 的话，这些内容会被静默丢弃：
        # 界面上就是「点了发送很久没反应」。
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
