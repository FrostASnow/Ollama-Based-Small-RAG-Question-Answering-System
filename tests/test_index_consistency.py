"""索引一致性 / 配置对称性 / 探测缓存失效 的回归测试。盯住「配置改了，索引没跟着
变」这类**静默**故障：换嵌入模型不校验（#2）、GET/PUT 字段不对称（#3）、改
chunk_size 后旧索引照常工作（#4）、registry.clear() 留过期元信息（#5）、能力缓存
不失效（#6）、扫描件只 warning（#8）。**本套件会清空知识库**，强制跑在临时数据目录
上（见下面的隔离自检）。运行：.venv\\Scripts\\python.exe tests\\test_index_consistency.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 必须在导入 app.* 之前设置：app.core.paths 在导入时就把数据目录定死了
os.environ["RAG_DATA_DIR"] = str(TMP_ROOT / "data_index_consistency")

from fastapi import HTTPException  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.paths import DATA_DIR, UPLOADS_DIR  # noqa: E402
from app.routers import chat as chat_router  # noqa: E402
from app.routers import documents as documents_router  # noqa: E402
from app.routers import health as health_router  # noqa: E402
from app.schemas import ChatRequest, ConfigResponse, ConfigUpdate, SearchRequest  # noqa: E402
from app.services import embeddings, index_health, ollama_client, rag  # noqa: E402
from app.services.ingest import (  # noqa: E402
    BACKUP_ROOT,
    backup_index,
    clear_all,
    ingest_path,
    reindex_all,
)
from app.services.installer import Installer  # noqa: E402
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


#: 逐字段 PUT 用的探针值，必须覆盖 ConfigUpdate 的每一个字段 ——
#: 少一个就说明「新加了配置项但没人验证它能不能写进去」，本套件会直接失败。
PROBE_VALUES: dict[str, object] = {
    "llm_model": "llama3.2",
    "llm_temperature": 0.5,
    "llm_num_ctx": 4096,
    "llm_num_predict": 512,
    "llm_repeat_penalty": 1.1,
    "llm_repeat_last_n": 256,
    "expose_thinking": False,
    "embedding_model_name": "BAAI/bge-small-zh-v1.5",
    "embedding_device": "cpu",
    "embedding_dimension": 512,
    "chunk_size": 600,
    "chunk_overlap": 80,
    "strip_boilerplate": False,
    "restore_pdf_paragraphs": False,
    "strip_references": False,
    "pdf_min_chars_per_page": 60,
    "top_k": 6,
    "score_threshold": 0.3,
    "score_window": 0.2,
    "score_floor": 0.05,
    "score_relax_limit": 0.4,
    "dedupe_ratio": 0.7,
    "max_context_chars": 3000,
    "summary_max_chunks": 6,
    "max_upload_mb": 10,
    "allowed_extensions": ["txt", "md"],
    "max_history_turns": 2,
    "backup_before_reindex": False,
    "index_backup_keep": 1,
}


def snapshot_config() -> dict[str, object]:
    return {name: getattr(settings, name) for name in ConfigResponse.model_fields}


def restore_config(snapshot: dict[str, object]) -> None:
    for name, value in snapshot.items():
        setattr(settings, name, value)


def write_doc(name: str, body: str) -> tuple[Path, int]:
    """往隔离的上传目录里写一篇文档，返回 (路径, 字节数)。"""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / name
    path.write_text(body, encoding="utf-8")
    return path, path.stat().st_size


BODY = (
    "《差旅报销管理办法》\n\n"
    "住宿费一线城市每晚 600 元，其他城市每晚 400 元。市内交通费每日上限 80 元。\n\n"
    "员工应在出差结束后十五个工作日内提交报销单并附全部原始票据。\n"
)


def main() -> int:
    print("=" * 70)
    print("索引一致性 / 配置对称性 回归测试")
    print("=" * 70)

    # 数据目录隔离自检：本套件会 clear_all()，绝不能碰真实知识库
    if DATA_DIR == PROJECT_ROOT / "data":
        print("\n[FAIL] 数据目录隔离失效：本套件会清空知识库，拒绝在真实 data/ 上运行")
        return 1
    print(f"数据目录（隔离）: {DATA_DIR}")

    saved = snapshot_config()

    # ==================================================================
    section("1. 配置字段表：GET 与 PUT 必须完全对称")
    response_fields = set(ConfigResponse.model_fields)
    update_fields = set(ConfigUpdate.model_fields)

    missing_in_update = sorted(response_fields - update_fields)
    extra_in_update = sorted(update_fields - response_fields)
    check("GET 与 PUT 的字段名集合完全一致",
          response_fields == update_fields,
          f"仅 GET 有: {missing_in_update or '无'}；仅 PUT 有: {extra_in_update or '无'}")

    not_on_settings = sorted(name for name in response_fields if not hasattr(settings, name))
    check("每个字段在 settings 上都有对应项", not not_on_settings,
          f"缺失: {not_on_settings}" if not_on_settings else f"{len(response_fields)} 个字段")

    undeclared = sorted(response_fields - set(PROBE_VALUES))
    check("探针值覆盖了每一个字段（新增配置项必须同步补测试）", not undeclared,
          f"未覆盖: {undeclared}" if undeclared else f"{len(PROBE_VALUES)} 项")

    # ---- 1a. 一次性把所有字段都 PUT 进去，再逐项核对回显 ----
    payload = ConfigUpdate(**PROBE_VALUES)
    echoed = asyncio.run(health_router.update_config(payload))

    mismatched: list[str] = []
    for name, expected in PROBE_VALUES.items():
        actual = getattr(echoed, name)
        if name == "allowed_extensions":
            expected = [".txt", ".md"]  # 会被归一化（保序去重）
        if actual != expected or getattr(settings, name) != expected:
            mismatched.append(f"{name}: 回显 {actual!r} / settings {getattr(settings, name)!r}")
    check("PUT 的每一项都真的生效并回显出来", not mismatched,
          "；".join(mismatched) if mismatched else f"{len(PROBE_VALUES)} 个字段")

    check("PUT 返回的就是 GET 的结构（同一个模型）", isinstance(echoed, ConfigResponse))
    check("扩展名归一化：'txt' → '.txt'（保序）",
          settings.allowed_extensions == [".txt", ".md"], str(settings.allowed_extensions))

    # ---- 1b. 只传一项时，其余项不得被改动 ----
    before = snapshot_config()
    partial = asyncio.run(health_router.update_config(ConfigUpdate(top_k=9)))
    changed = [name for name in before if getattr(partial, name) != before[name]]
    check("只传 top_k 时其它字段纹丝不动", changed == ["top_k"], str(changed))
    check("未传的字段不会被 None 覆盖（PUT 的语义是 patch）", partial.llm_model == before["llm_model"])

    # ---- 1c. 交叉校验：违反切分器约束的组合必须被拒绝 ----
    try:
        asyncio.run(health_router.update_config(ConfigUpdate(chunk_size=500, chunk_overlap=600)))
        check("chunk_overlap >= chunk_size 被拒绝", False, "竟然接受了")
    except HTTPException as exc:
        check("chunk_overlap >= chunk_size 被拒绝（400）", exc.status_code == 400, str(exc.detail)[:60])
    check("被拒绝的组合没有落到 settings 上", settings.chunk_size == before["chunk_size"])

    try:
        asyncio.run(health_router.update_config(ConfigUpdate(allowed_extensions=["  "])))
        check("空白扩展名列表被拒绝", False, "竟然接受了")
    except HTTPException as exc:
        check("空扩展名列表被拒绝（400）", exc.status_code == 400, str(exc.detail)[:40])

    restore_config(saved)
    check("配置已复原", settings.llm_model == saved["llm_model"] and settings.top_k == saved["top_k"])

    # ==================================================================
    section("2. 索引元信息：登记什么、清空时清什么")
    # 防「clear() 后留下过期元信息」：一致性判定会据此得出错误结论
    clear_all()
    cleared = registry.get_index_meta()
    check("知识库清空后没有残留的索引元信息",
          all(value is None for value in cleared.values()), str(cleared))
    check("清空后一致性状态为 empty", vector_store_state() == "empty")

    path, size = write_doc("consistency.txt", BODY * 4)
    info, _dup = ingest_path(path, "consistency.txt", size)
    meta = registry.get_index_meta()

    check("登记了嵌入模型名", meta["embedding_model"] == settings.embedding_model_name,
          str(meta["embedding_model"]))
    check("登记的是模型**实际**维度（不是手写的声明值）",
          meta["dimension"] == embeddings.loaded_dimension() == 384,
          f"登记 {meta['dimension']} / 实际 {embeddings.loaded_dimension()}")
    check("登记了切分参数",
          meta["chunk_size"] == settings.chunk_size and meta["chunk_overlap"] == settings.chunk_overlap,
          f"chunk_size={meta['chunk_size']}, chunk_overlap={meta['chunk_overlap']}")
    check("登记了解析开关",
          isinstance(meta["parse_options"], dict) and meta["parse_options"]["strip_boilerplate"] is True,
          str(meta["parse_options"]))
    check("登记了建立时间", bool(meta["indexed_at"]), str(meta["indexed_at"]))
    check("一致性状态为 ok", vector_store_state() == "ok", vector_store_state())

    registry.clear()
    check("registry.clear() 同样清掉索引元信息（issues #5）",
          all(value is None for value in registry.get_index_meta().values()),
          str(registry.get_index_meta()))

    # 重新入库，供后面几节使用
    clear_all()
    path, size = write_doc("consistency.txt", BODY * 4)
    info, _dup = ingest_path(path, "consistency.txt", size)
    check("重新入库成功", info.chunk_count > 0 and vector_store_state() == "ok",
          f"{info.chunk_count} 块")

    # ==================================================================
    section("3. 换嵌入模型：必须当场发现，并给出能照做的错误")
    saved_model = settings.embedding_model_name
    saved_dim = settings.embedding_dimension

    settings.embedding_model_name = "BAAI/bge-small-zh-v1.5"
    settings.embedding_dimension = 512

    report = vector_store.compatibility()
    check("判定为 incompatible（不是静默继续）",
          report["compatible"] is False and report["state"] == "incompatible",
          str(report["state"]))
    reasons = "；".join(report["reasons"])
    check("原因里点明新旧两个模型名",
          "all-MiniLM-L6-v2" in reasons and "bge-small-zh-v1.5" in reasons, reasons[:80])
    check("维度差异被记录成结构化数据（便于界面展示）",
          report["dimension"]["index"] == 384 and report["dimension"]["declared"] == 512,
          str(report["dimension"]))

    problems = index_health.health_problems(report)
    check("体检结论直接告诉用户去点「重建索引」",
          bool(problems) and "重建索引" in problems[0], problems[0][:60] if problems else "无")

    try:
        vector_store.search_detailed("住宿费每晚多少")
        check("检索应当被拦住", False, "竟然返回了结果")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("检索抛的是中文可照做错误（不是 FAISS 断言）",
              type(exc).__name__ == "IndexIncompatibleError"
              and "重建索引" in text and "bge-small" in text,
              f"{type(exc).__name__}: {text.splitlines()[0][:50]}")

    try:
        asyncio.run(chat_router.search(SearchRequest(query="住宿费每晚多少")))
        check("POST /api/search 应当返回 409", False, "竟然 200")
    except HTTPException as exc:
        check("POST /api/search 返回 409（有明确出路，而非 500）", exc.status_code == 409)

    try:
        asyncio.run(chat_router.chat(ChatRequest(question="住宿费每晚多少", stream=False)))
        check("POST /api/chat 应当返回 409", False, "竟然 200")
    except HTTPException as exc:
        check("POST /api/chat 返回 409", exc.status_code == 409, str(exc.detail).splitlines()[0][:50])

    health = asyncio.run(health_router.health())
    check("GET /api/health 把索引问题报成 degraded",
          health.status == "degraded" and health.detail["index"]["state"] == "incompatible")
    check("problems 里出现索引相关条目",
          any("索引" in item for item in health.detail["problems"]),
          str(health.detail["problems"])[:80])
    check("detail 里带结构化的一致性报告",
          health.detail["index_compatible"] is False and health.detail["index_stale"] is True)

    other_path, other_size = write_doc("new_after_switch.txt", BODY)
    try:
        ingest_path(other_path, "new_after_switch.txt", other_size)
        check("不兼容时拒绝把新模型的向量塞进旧索引", False, "竟然入库了")
    except Exception as exc:  # noqa: BLE001
        check("不兼容时拒绝继续入库（否则索引会变成混合向量空间）",
              "IndexIncompatibleError" == type(exc).__name__,
              type(exc).__name__)

    # 流式路径（前端实际走的那条）也必须给出可读错误，而不是一路空白
    async def collect_stream() -> list[dict]:
        return [event async for event in rag.rag_service.stream("住宿费每晚多少")]

    events = asyncio.run(collect_stream())
    error_events = [event for event in events if event["event"] == "error"]
    check("流式问答返回 error 事件（不是静默空回答）", bool(error_events),
          str([event["event"] for event in events]))
    check("流式错误信息同样给出重建索引的指引",
          bool(error_events) and "重建索引" in error_events[0]["data"]["message"],
          (error_events[0]["data"]["message"].splitlines()[0][:50] if error_events else "无"))
    check("不兼容时不会调用 LLM 生成", all(event["event"] != "token" for event in events))

    settings.embedding_model_name = saved_model
    settings.embedding_dimension = saved_dim
    check("改回原模型后立即恢复兼容",
          vector_store.compatibility()["compatible"] is True)
    results, _info = vector_store.search_detailed("住宿费一线城市每晚 600 元", top_k=2)
    check("恢复后检索正常返回", len(results) >= 1, f"{len(results)} 条")

    # ==================================================================
    section("4. 改 chunk_size：僵尸知识库必须被看见")
    saved_chunk = settings.chunk_size
    settings.chunk_size = 300  # 防「改 chunk_size 后旧分块继续用」的静默漂移

    report = vector_store.compatibility()
    check("判定为 stale（索引还能用，但内容对不上设置）",
          report["state"] == "stale" and report["stale"] is True and report["compatible"] is True,
          str(report["state"]))
    notices = "；".join(report["notices"])
    check("提示里同时给出旧值与新值",
          f"索引 {saved_chunk}" in notices and "当前 300" in notices, notices[:80])

    problems = index_health.health_problems(report)
    check("stale 也会出现在健康检查里（不再静默）",
          bool(problems) and "重建索引" in problems[0], problems[0][:60] if problems else "无")

    results, _info = vector_store.search_detailed("住宿费", top_k=2)
    check("stale 状态不阻断检索（只是结果对应旧分块）", isinstance(results, list), f"{len(results)} 条")

    reindex_report = reindex_all()
    check("重建报告里带重建前的索引状态",
          reindex_report["index_state_before"] == "stale", str(reindex_report["index_state_before"]))
    check("重建报告里带备份路径",
          bool(reindex_report["backup_dir"]), str(reindex_report["backup_dir"]))
    check("重建后登记了新的 chunk_size",
          registry.get_index_meta()["chunk_size"] == 300,
          str(registry.get_index_meta()["chunk_size"]))
    check("重建后状态回到 ok", vector_store_state() == "ok", vector_store_state())

    settings.chunk_size = saved_chunk
    reindex_all()
    check("复原 chunk_size 并重建后仍然一致", vector_store_state() == "ok", vector_store_state())

    # ==================================================================
    section("5. 备份：整体重建 + 保留份数")
    backup = backup_index()
    check("backup() 真的被调用并产出了目录", backup is not None and Path(backup).is_dir(), str(backup))
    if backup:
        files = sorted(item.name for item in Path(backup).iterdir())
        check("备份里有 .faiss 与 .pkl 两个文件",
              any(name.endswith(".faiss") for name in files)
              and any(name.endswith(".pkl") for name in files), str(files))

    # 不一致时走「整体重建」分支：向量空间变了，逐篇删旧向量已无意义
    settings.embedding_model_name = "BAAI/bge-small-zh-v1.5"
    report = reindex_all()
    check("向量空间变了时整体重建（不再逐篇删旧向量）",
          report["rebuilt_from_scratch"] is True and report["index_state_before"] == "incompatible",
          f"{report['index_state_before']} / from_scratch={report['rebuilt_from_scratch']}")
    check("整体重建后登记为当前模型",
          registry.get_index_meta()["embedding_model"] == "BAAI/bge-small-zh-v1.5")
    check("整体重建后恢复兼容", vector_store.compatibility()["compatible"] is True)

    settings.embedding_model_name = saved_model
    reindex_all()
    check("复原模型名并重建后兼容", vector_store.compatibility()["compatible"] is True)

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    fake = ("20000101-000000", "20000102-000000", "20000103-000000")
    for name in fake:
        (BACKUP_ROOT / name).mkdir(exist_ok=True)
    backup_index(keep=2)
    remaining = sorted(item.name for item in BACKUP_ROOT.iterdir() if item.is_dir())
    check("备份份数不超过保留策略", len(remaining) == 2, str(remaining))
    check("被清掉的是最旧的那几份", not any(name in remaining for name in fake), str(remaining))

    # ==================================================================
    section("6. 探测缓存：该失效的时候必须真的失效")
    # 防「刚 pull 的模型被按旧结论对待」：主动失效之外，兜底 TTL 也收紧到 60 秒
    ollama_client._cache.update({"at": time.monotonic(), "reachable": True, "names": ["m1"]})
    ollama_client._capability_cache["probe"] = (time.monotonic(), True)

    ollama_client.invalidate_all()
    check("invalidate_all() 清掉可达性/模型列表缓存", ollama_client._cache["at"] == 0.0)
    check("invalidate_all() 清掉模型能力缓存", not ollama_client._capability_cache)
    check("能力缓存兜底 TTL 收紧到 60 秒以内（原来 300 秒）",
          ollama_client.CAPABILITY_TTL_SECONDS <= 60.0,
          f"{ollama_client.CAPABILITY_TTL_SECONDS}s")

    counter = {"n": 0}
    original_invalidate = ollama_client.invalidate_all

    def counting_invalidate() -> None:
        counter["n"] += 1
        original_invalidate()

    ollama_client.invalidate_all = counting_invalidate  # type: ignore[assignment]
    try:
        asyncio.run(health_router.update_config(ConfigUpdate(llm_model="llama3.2")))
        check("运行期切换 LLM 模型 → 探测缓存立即失效", counter["n"] == 1, f"{counter['n']} 次")

        asyncio.run(health_router.update_config(ConfigUpdate(embedding_model_name="BAAI/bge-small-zh-v1.5")))
        check("运行期切换嵌入模型 → 探测缓存立即失效", counter["n"] == 2, f"{counter['n']} 次")

        counter["n"] = 0
        asyncio.run(health_router.update_config(ConfigUpdate(top_k=5)))
        check("只改检索参数时不浪费一次缓存刷新", counter["n"] == 0, f"{counter['n']} 次")
    finally:
        ollama_client.invalidate_all = original_invalidate  # type: ignore[assignment]

    asyncio.run(health_router.update_config(ConfigUpdate(llm_model=saved["llm_model"])))
    asyncio.run(health_router.update_config(ConfigUpdate(embedding_model_name=saved["embedding_model_name"])))

    # 用户刷新模型列表（通常意味着刚 ollama pull 完）→ 能力缓存必须失效
    ollama_client._capability_cache["after_pull"] = (time.monotonic(), False)
    asyncio.run(health_router.list_models())
    check("GET /api/models 会清掉能力缓存（刚 pull 的模型不该按旧结论对待）",
          "after_pull" not in ollama_client._capability_cache)

    # 一键安装成功后 → 两个缓存都要失效
    ollama_client._capability_cache["after_install"] = (time.monotonic(), False)
    ollama_client._cache["at"] = time.monotonic()
    Installer._invalidate_probe_caches()
    check("一键安装成功后清掉能力/可达性缓存",
          "after_install" not in ollama_client._capability_cache and ollama_client._cache["at"] == 0.0)

    # setup 体检的「重新检测」也要清（fresh=True）
    ollama_client._capability_cache["before_recheck"] = (time.monotonic(), False)
    asyncio.run(health_router.setup_report(fresh=True))
    check("「重新检测」会绕开并清空两个缓存",
          "before_recheck" not in ollama_client._capability_cache)

    # ==================================================================
    section("7. 扫描版 PDF：警告要一路走到用户面前")
    # 防「扫描件只在日志里 warning」：全空白 → 报错点明扫描件/OCR；
    # 抽到一点但每页字符数过低 → 结构化警告，跟着最近一次解析走。
    import app.services.loader as loader_module

    original_parser = loader_module._PARSERS[".txt"]
    try:
        # (a) 完全抽不到文字 → 报错信息必须点明「扫描件 / OCR」
        loader_module._PARSERS[".txt"] = lambda _path: [("", {}), ("", {}), ("", {})]
        scan_path, scan_size = write_doc("all_blank.txt", "blank-body")
        try:
            loader_module.parse_document(scan_path, "scan1", "all_blank.txt")
            check("全空白文档应当报错", False, "竟然解析成功")
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            check("抽不到文字时报错点明扫描件/OCR",
                  "扫描件" in text and "OCR" in text, text[-60:])

        # (b) 抽到一点文字（能入库）但平均每页字符数过低 → 结构化警告
        loader_module._PARSERS[".txt"] = lambda _path: [
            ("短", {}),
            ("短", {}),
            ("这是一段足够长的正文内容，用来通过最短分块长度过滤。" * 4, {}),
        ]
        warn_path, warn_size = write_doc("low_text.txt", "low-text-body")
        warn_info, _dup = ingest_path(warn_path, "low_text.txt", warn_size)
        check("低抽取质量时给出面向用户的警告",
              bool(warn_info.warnings) and "扫描件" in warn_info.warnings[0],
              (warn_info.warnings or ["(无)"])[0][:50])
        check("警告同样落进注册表（重新打开页面也还在）",
              bool(registry.get(warn_info.doc_id)["warnings"]))
        listed = asyncio.run(documents_router.list_documents())
        check("/api/documents 会把 warnings 交给前端",
              any(doc.warnings for doc in listed.documents),
              str([len(doc.warnings) for doc in listed.documents]))

        # (c) 重建索引后警告要跟着最新一次解析走
        rebuilt = reindex_all()
        detail = next((d for d in rebuilt["details"] if d["doc_id"] == warn_info.doc_id), {})
        check("重建索引的报告里带这次的警告",
              bool(detail.get("warnings")), str(detail.get("warnings"))[:50])
    finally:
        loader_module._PARSERS[".txt"] = original_parser

    # ==================================================================
    section("8. 死代码清理：不该再有第二条检索路径")
    from app.services.vectorstore import vector_store as store_singleton

    check("search_documents() 已移除（它与 search_detailed 重复且返回值形状不同）",
          not hasattr(store_singleton, "search_documents"))
    check("try_get_embeddings() 被 setup 体检真实调用",
          "try_get_embeddings" in (PROJECT_ROOT / "backend" / "app" / "services" / "setup.py").read_text(encoding="utf-8"))
    check("backup() 由重建流程经 backup_index() 调用",
          "backup_index()" in (PROJECT_ROOT / "backend" / "app" / "services" / "ingest.py").read_text(encoding="utf-8"))

    # 收尾：恢复配置并重建，让目录回到一致状态
    restore_config(saved)
    clear_all()
    print()
    print("=" * 70)
    print(f"结果：通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 70)
    return 0 if FAILED == 0 else 1


def vector_store_state() -> str:
    return str(vector_store.compatibility()["state"])


if __name__ == "__main__":
    raise SystemExit(main())
