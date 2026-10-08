"""系统状态与配置接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.core.logging import get_logger
from app.core.paths import DATA_DIR
from app.schemas import (
    ConfigResponse,
    ConfigUpdate,
    HealthResponse,
    InstallRequest,
    SetupReport,
)
from app.services import ollama_client
from app.services.embeddings import embedding_status
from app.services.index_health import health_problems
from app.services.installer import installer, stream_install
from app.services.registry import registry
from app.services.setup import build_report, portable_ollama_status
from app.services.vectorstore import vector_store

router = APIRouter(prefix="/api", tags=["system"])

logger = get_logger(__name__)

#: 改了这些键 → 必须重建 LLM 客户端：模型名与生成参数都固定在 ChatOllama
#: 实例上，不重建的话改动只改了配置、没改实际行为。
_LLM_KEYS = frozenset(
    {
        "llm_model",
        "llm_temperature",
        "llm_num_ctx",
        "llm_num_predict",
        "llm_repeat_penalty",
        "llm_repeat_last_n",
        "expose_thinking",
    }
)

#: 改了这些键 → 向量空间变了：必须丢掉已加载的嵌入模型与内存索引，
#: 否则「换了模型却还在用旧模型编码」根本看不出来。
_EMBEDDING_KEYS = frozenset(
    {"embedding_model_name", "embedding_device", "embedding_dimension"}
)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # 一次探测同时得到「是否可达」和「已装模型」，避免串行等两次超时
    reachable, names = await ollama_client.probe()
    model_ok = ollama_client.model_is_available(settings.llm_model, names)

    embedding = embedding_status()
    stats = registry.stats()

    index_ready = vector_store.ready or vector_store.exists_on_disk()

    # 索引是离线产物：换过嵌入模型 / 改过切分参数后它会与当前配置分叉；
    # 只在提问时才发现的话，用户看到的是 FAISS 的内部断言错误（见 index_health）。
    index_report = vector_store.compatibility()

    problems: list[str] = []
    if not reachable:
        problems.append("Ollama 服务未启动")
    elif not model_ok:
        problems.append(f"Ollama 中未找到模型 {settings.llm_model}")

    # 便携版装了一半：服务能起来、模型也列得出来，但一发提问就
    # "llama-server binary not found"。不点名的话用户无从判断。
    portable = portable_ollama_status()
    if portable["present"] and portable["complete"] is False:
        problems.append("Ollama 安装不完整（缺少推理引擎），请重新运行一键准备")

    if not embedding["local_ready"]:
        problems.append("本地嵌入模型缺失，请先执行 scripts\\prepare.ps1")

    problems.extend(health_problems(index_report))

    return HealthResponse(
        status="ok" if not problems else "degraded",
        version=settings.version,
        app_name=settings.app_name,
        offline=True,
        ollama_reachable=reachable,
        llm_model=settings.llm_model,
        llm_model_available=model_ok,
        ollama_models=sorted(names),
        embedding_model=settings.embedding_model_name,
        embedding_ready=bool(embedding["local_ready"]),
        embedding_device=settings.embedding_device,
        index_ready=index_ready,
        document_count=stats["document_count"],
        chunk_count=stats["chunk_count"],
        detail={
            "problems": problems,
            "embedding": embedding,
            "ollama_install": portable,
            "vector_store": vector_store.stats(),
            "index_meta": registry.get_index_meta(),
            # 索引一致性体检（state / compatible / stale / reasons / notices）
            "index": index_report,
            "index_compatible": index_report["compatible"],
            "index_stale": index_report["stale"],
            # 当前实例实际使用的数据目录：测试据此确认自己面对的是临时目录
            # 而不是用户真实知识库（HTTP 验收会清空知识库）。
            "data_dir": str(DATA_DIR),
        },
    )


@router.get("/setup", response_model=SetupReport)
async def setup_report(fresh: bool = False) -> SetupReport:
    """首次配置体检。

    前端启动时调用本接口；``ready`` 为 false 就弹出引导窗口并展示安装命令。

    ``fresh=true`` 会绕过 Ollama 探测缓存，用于用户装完东西后点「重新检测」。
    """
    return await build_report(fresh=fresh)


# ---------------------------------------------------------------------------
# 一键安装
# ---------------------------------------------------------------------------
@router.get("/setup/install")
async def install_status() -> dict[str, Any]:
    """查询一键安装任务的当前状态。"""
    return installer.snapshot()


@router.post("/setup/install")
async def install_start(payload: InstallRequest) -> dict[str, Any]:
    """在服务端直接执行环境准备脚本（等价于手动双击 ``scripts\\prepare.cmd``）。

    进度会实时推送到网页上；同一时间只允许一个任务，重复调用返回 409。
    """
    started, message = installer.start(mirror=payload.mirror, skip_ollama=payload.skip_ollama)
    if not started:
        raise HTTPException(status_code=409, detail=message)
    return {"started": True, "message": message, "status": installer.snapshot()}


@router.get("/setup/install/stream")
async def install_stream() -> StreamingResponse:
    """以 SSE 实时推送安装过程的输出。"""
    return StreamingResponse(
        stream_install(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/setup/install/cancel")
async def install_cancel() -> dict[str, Any]:
    """取消正在运行的安装任务（按进程树终止）。"""
    cancelled, message = installer.cancel()
    if not cancelled:
        raise HTTPException(status_code=409, detail=message)
    return {"cancelled": True, "message": message}


@router.post("/setup/install/reset")
async def install_reset() -> dict[str, Any]:
    """清空已结束的任务状态，让用户可以重新发起安装。"""
    installer.reset()
    return installer.snapshot()


def _config_payload() -> ConfigResponse:
    """按 :class:`ConfigResponse` 的字段表逐项从 settings 取值。

    刻意不手写一长串取值清单：那份清单就是字段漂移的源头（漏写一项前端永远读不到，
    而 PUT 却可能支持它）。字段表只有一个来源，漏定义会直接 AttributeError。
    """
    return ConfigResponse(
        **{name: getattr(settings, name) for name in ConfigResponse.model_fields}
    )


@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return _config_payload()


@router.put("/config", response_model=ConfigResponse)
async def update_config(payload: ConfigUpdate) -> ConfigResponse:
    """运行期调整参数。

    GET 与 PUT 的字段表完全一致（见 :class:`app.schemas.ConfigValues`）。
    只作用于当前进程；要永久生效请写入 ``rag-qa/.env``。
    """
    from app.services import embeddings, rag

    requested = {
        name: getattr(payload, name)
        for name in ConfigUpdate.model_fields
        if getattr(payload, name) is not None
    }
    if not requested:
        return _config_payload()

    # 先做交叉校验再落值：两个字段互相矛盾时（例如 overlap >= size），
    # 半套生效的配置比拒绝修改更难排查。
    chunk_size = int(requested.get("chunk_size", settings.chunk_size))
    chunk_overlap = int(requested.get("chunk_overlap", settings.chunk_overlap))
    if chunk_overlap >= chunk_size:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_overlap（{chunk_overlap}）必须小于 chunk_size（{chunk_size}），"
            "否则切分器会陷入死循环或直接报错。",
        )

    extensions = requested.get("allowed_extensions")
    if extensions is not None:
        normalized: list[str] = []
        for item in extensions:
            text = str(item).strip().lower()
            if not text:
                continue
            candidate = text if text.startswith(".") else f".{text}"
            if candidate not in normalized:
                # 保序去重（不排序）：get→put 原样回写必须幂等，
                # 排序会让「回显与提交值一致」都不成立。
                normalized.append(candidate)
        if not normalized:
            raise HTTPException(status_code=400, detail="allowed_extensions 不能为空")
        requested["allowed_extensions"] = normalized

    # 只对**真正变了**的项动手：把 embedding_model_name 原样传回来也会触发
    # 「重置嵌入实例 + 丢弃内存索引」，前端每次回写整份配置都会白扔一次模型。
    changed = {
        name: value
        for name, value in requested.items()
        if getattr(settings, name) != value
    }
    if not changed:
        return _config_payload()

    for name, value in changed.items():
        setattr(settings, name, value)

    if changed.keys() & _LLM_KEYS:
        rag.reset_llm()

    if changed.keys() & _EMBEDDING_KEYS:
        # 换了嵌入模型/设备/维度：旧嵌入实例与内存索引全部作废。
        # 不丢的话新配置只影响展示，向量化继续用旧模型 —— 改了却不生效。
        embeddings.reset_instance()
        vector_store.drop_memory()
        logger.warning(
            "嵌入配置已变更（%s），已重置嵌入实例与内存索引；"
            "若新模型与旧索引不一致，需重建索引（/api/health 会明确报出来）",
            "、".join(sorted(changed.keys() & _EMBEDDING_KEYS)),
        )

    if {"llm_model", "embedding_model_name", "embedding_device"} & changed.keys():
        # 模型可能刚被用户在面板里 pull 过/换过，两个探测缓存一起清，
        # 避免界面按旧结论跑（最长 60 秒）。
        ollama_client.invalidate_all()

    logger.info("运行期配置已更新：%s", "、".join(sorted(changed.keys())))
    return _config_payload()


@router.get("/models")
async def list_models() -> dict[str, object]:
    """列出 Ollama 中已安装的模型（用于前端下拉切换）。"""
    raw = await ollama_client.list_models()
    # 用户主动来看模型列表，通常意味着刚 pull 完或准备换模型：
    # 顺手让「是否支持原生 thinking」的缓存失效，免得新模型被按旧结论对待。
    ollama_client.invalidate_capability_cache()
    models = []
    for item in raw:
        name = item.get("name") or item.get("model") or ""
        details = item.get("details") or {}
        models.append(
            {
                "name": name,
                "size_bytes": item.get("size"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "current": name == settings.llm_model,
            }
        )
    models.sort(key=lambda m: str(m["name"]))
    return {"models": models, "current": settings.llm_model}
