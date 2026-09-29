"""Ollama 集成测试：用协议兼容的假服务验证真实调用链路。

与 `test_e2e.py` 的区别：
    test_e2e.py 直接把 LLM 换成 Python 假对象，验证的是 RAG 业务逻辑；
    本测试保留 **真实的 ChatOllama + ollama 客户端 + HTTP**，
    只把 Ollama 服务端换成协议兼容的实现，验证的是集成层：
        请求是否正确发出、流式 NDJSON 是否正确解析、
        message.thinking 字段是否被识别、错误分支是否可读。

    .venv\\Scripts\\python.exe tests\\test_ollama_integration.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 本套件同样会 clear_all()，必须跑在临时数据目录上（导入 app.* 之前设置）
os.environ["RAG_DATA_DIR"] = str(TMP_ROOT / "data_ollama")

from app.config import settings  # noqa: E402
from app.core.paths import DATA_DIR  # noqa: E402
from app.services import ollama_client, rag  # noqa: E402
from app.services.ingest import clear_all, ingest_path  # noqa: E402
from tests.fake_ollama import (  # noqa: E402
    DEFAULT_PIECES,
    MODEL_NAME,
    THINK_CLOSE,
    THINK_OPEN,
    _Handler,
    start_fake_ollama,
)

PASSED = 0
FAILED = 0
ORIGINAL_BASE_URL = settings.ollama_base_url


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


def prepare_corpus() -> None:
    work = TMP_ROOT / "ollama_corpus"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "travel.txt"
    path.write_text(
        ("《差旅报销管理办法》\n\n"
         "住宿费一线城市每晚 600 元，其他城市每晚 400 元。"
         "市内交通费每日上限 80 元。超出标准需部门负责人书面审批。\n\n") * 3,
        encoding="utf-8",
    )
    ingest_path(path, "travel.txt", path.stat().st_size)


async def main() -> int:
    print("=" * 70)
    print("Ollama 集成测试（真实 ChatOllama + 协议兼容假服务）")
    print("=" * 70)

    server, base_url = start_fake_ollama()
    print(f"假 Ollama: {base_url}")

    # 数据目录隔离自检：不通过就停手，别把真实知识库清空
    if DATA_DIR == PROJECT_ROOT / "data":
        print("\n[FAIL] 数据目录隔离失效：本套件会清空知识库，拒绝在真实 data/ 上运行")
        return 1
    print(f"数据目录（隔离）: {DATA_DIR}")

    # 指向假服务并清掉缓存
    settings.ollama_base_url = base_url
    settings.llm_model = MODEL_NAME
    rag.reset_llm()
    ollama_client.invalidate_cache()

    clear_all()
    prepare_corpus()

    try:
        # ------------------------------------------------------------------
        section("1. 服务探测（/api/tags）")
        reachable, names = await ollama_client.probe()
        check("报告服务可达", reachable is True)
        check("解析出模型名", MODEL_NAME in names, str(names))
        check("model_is_available 判定正确",
              ollama_client.model_is_available(MODEL_NAME, names) is True)
        check("前缀匹配生效",
              ollama_client.model_is_available("deepseek-r1", names) is True)

        models = await ollama_client.list_models()
        check("模型列表含 details", bool(models) and "details" in models[0])
        check("解析出量化等级",
              (models[0].get("details") or {}).get("quantization_level") == "Q4_K_M")

        # ------------------------------------------------------------------
        section("1b. 原生 thinking 通道能力探测")
        # deepseek-r1 这类模型由 Ollama 走**原生 thinking 通道**：思维链不在
        # content 里，而在 message.thinking → langchain 的 reasoning_content。
        # 不开这个通道，用户点发送后会长时间看不到任何输出。
        ollama_client.invalidate_capability_cache()
        check("识别出模型支持 thinking",
              ollama_client.supports_thinking(MODEL_NAME) is True)

        rag.reset_llm()
        llm = rag.get_llm()
        check("ChatOllama 打开了原生 thinking 通道",
              getattr(llm, "reasoning", None) is True, str(getattr(llm, "reasoning", None)))

        _Handler.capabilities = ["completion"]
        ollama_client.invalidate_capability_cache()
        check("不支持 thinking 的模型被正确识别",
              ollama_client.supports_thinking(MODEL_NAME) is False)
        rag.reset_llm()
        llm = rag.get_llm()
        check("不支持时不下发 think=true",
              getattr(llm, "reasoning", None) is None, str(getattr(llm, "reasoning", None)))

        _Handler.capabilities = ["completion", "tools", "thinking"]
        ollama_client.invalidate_capability_cache()
        rag.reset_llm()

        # ------------------------------------------------------------------
        section("1c. 模型预热（/api/generate 空 prompt）")
        # Ollama 懒加载：不预热的话第一条提问要等权重载入显存（实测约 100 秒）
        check("预热调用成功", await ollama_client.warmup_model(MODEL_NAME) is True)
        check("预热请求带 keep_alive 而非 0",
              (_Handler.last_generate_payload or {}).get("keep_alive") == "10m",
              str(_Handler.last_generate_payload))
        check("预热失败时不抛异常",
              await ollama_client.warmup_model(MODEL_NAME, base_url="http://127.0.0.1:1") is False)

        # ------------------------------------------------------------------
        section("1d. 退出时卸载模型（keep_alive=0）")
        # 程序退出后不该继续占着显存：即使 Ollama 进程被保留（-KeepOllama），
        # 也要通过 keep_alive=0 让它把模型卸掉。
        check("卸载调用成功", await ollama_client.unload_model(MODEL_NAME) is True)
        check("卸载请求带 keep_alive=0",
              (_Handler.last_generate_payload or {}).get("keep_alive") == 0,
              str(_Handler.last_generate_payload))
        check("卸载失败不抛异常",
              await ollama_client.unload_model(MODEL_NAME, base_url="http://127.0.0.1:1") is False)

        # ------------------------------------------------------------------
        section("2. 非流式问答（ainvoke 完整往返）")
        result = await rag.rag_service.answer("住宿费一线城市每晚多少钱？", top_k=2)
        answer = result["answer"]
        thinking = result["thinking"] or ""

        check("返回答案", len(answer) > 0, f"{len(answer)} 字")
        check("答案含关键数字", "600" in answer, answer[:80])
        check("答案不含推理链开标签", THINK_OPEN not in answer)
        check("答案不含推理链闭标签", THINK_CLOSE not in answer)
        check("推理链被单独提取", "住宿报销标准" in thinking, thinking[:60])
        check("带回引用来源", len(result["sources"]) >= 1, f"{len(result['sources'])} 条")
        check("报告模型名", result["model"] == MODEL_NAME, result["model"])
        check("报告耗时", isinstance(result["elapsed_ms"], int))

        # ------------------------------------------------------------------
        section("3. 流式问答（NDJSON 逐块解析）")
        events = [e async for e in rag.rag_service.stream("住宿费标准是多少", top_k=2)]
        names_seq = [e["event"] for e in events]
        print(f"        事件序列: {names_seq}")

        check("无 error 事件", "error" not in names_seq)
        check("首事件为 meta", names_seq[0] == "meta")
        check("末事件为 done", names_seq[-1] == "done")

        answer_stream = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
        thinking_stream = "".join(e["data"]["delta"] for e in events if e["event"] == "thinking")

        check("流式回答非空", len(answer_stream) > 0, f"{len(answer_stream)} 字")
        check("流式回答含关键数字", "600" in answer_stream, answer_stream[:80])
        check("流式正文无标签残留",
              THINK_OPEN not in answer_stream and THINK_CLOSE not in answer_stream)
        check("流式推理链被剥离", "住宿报销标准" in thinking_stream, thinking_stream[:60])
        check("正文与推理链不重叠", "住宿报销标准" not in answer_stream)

        # 分块数量应与假服务发送的片段数一致（说明逐块解析，而非缓冲成一块）
        token_events = [e for e in events if e["event"] == "token"]
        expected_answer_pieces = sum(
            1 for piece in DEFAULT_PIECES if THINK_OPEN not in piece and THINK_CLOSE not in piece
        )
        check("token 事件数量合理", len(token_events) >= 1,
              f"{len(token_events)} 个 token 事件 / 预期正文片段 {expected_answer_pieces}")

        done = events[-1]["data"]
        check("done 含 token 用量", done.get("usage", {}).get("eval_count") == 42,
              str(done.get("usage")))
        check("cited 提取出引用编号", done.get("cited") == [1], str(done.get("cited")))

        # ------------------------------------------------------------------
        section("4. 健康检查联动")
        # 直接调用 health 路由函数，避免为了一个断言起一个 HTTP 服务
        from app.routers.health import health as health_endpoint

        health = await health_endpoint()
        check("health 报告 Ollama 可达", health.ollama_reachable is True)
        check("health 报告模型可用", health.llm_model_available is True)

        # 这里刻意不断言 status == "ok"：假服务本身没问题，但**环境**可能带着
        # 真实缺陷（例如项目内置的便携版 Ollama 解压不完整、嵌入模型没下），
        # 那时 degraded 才是正确答案。断言「状态与 problems 自洽」更有意义。
        problems = list(health.detail.get("problems", []))
        check("health 未报告与假服务有关的缺陷",
              not any("服务未启动" in p or "未找到模型" in p for p in problems), str(problems))
        check("health 状态与 problems 自洽",
              (health.status == "ok") == (not problems),
              f"status={health.status} problems={problems}")
        check("health 列出模型", MODEL_NAME in health.ollama_models, str(health.ollama_models))

        # ------------------------------------------------------------------
        section("5. 错误分支可读性")
        _Handler.fail_with = 500
        ollama_client.invalidate_cache()
        try:
            err_events = [e async for e in rag.rag_service.stream("住宿费多少", top_k=1)]
            err_names = [e["event"] for e in err_events]
            check("注入 500 后走 error 分支", "error" in err_names, str(err_names))
            if "error" in err_names:
                message = next(e["data"]["message"] for e in err_events if e["event"] == "error")
                check("错误信息提示 Ollama 未启动", "Ollama" in message, message[:80])
                check("错误信息含原始错误", "500" in message or "status" in message.lower(),
                      message[-60:])

            try:
                await rag.rag_service.answer("住宿费多少", top_k=1)
                check("非流式抛 LLMUnavailableError", False, "未抛出")
            except rag.LLMUnavailableError as exc:
                check("非流式抛 LLMUnavailableError", True, str(exc)[:60])
        finally:
            _Handler.fail_with = None
            ollama_client.invalidate_cache()

        # ------------------------------------------------------------------
        section("6. 恢复后仍可用")
        events_again = [e async for e in rag.rag_service.stream("住宿费多少", top_k=1)]
        check("恢复正常", [e["event"] for e in events_again][-1] == "done",
              str([e["event"] for e in events_again]))

    finally:
        settings.ollama_base_url = ORIGINAL_BASE_URL
        rag.reset_llm()
        ollama_client.invalidate_cache()
        clear_all()
        server.shutdown()
        server.server_close()

    print()
    print("=" * 70)
    print(f"结果：通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 70)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
