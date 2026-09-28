"""系统状态与配置接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.schemas import (
    ConfigResponse,
    ConfigUpdate,
    HealthResponse,
    InstallRequest,
    SetupReport,
)
from app.services import ollama_client
from app.services.embeddings import embedding_status
from app.services.installer import installer, stream_install
from app.services.registry import registry
from app.services.setup import build_report, portable_ollama_status
from app.services.vectorstore import vector_store

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # 一次探测同时得到「是否可达」和「已装模型」，避免串行等两次超时
    reachable, names = await ollama_client.probe()
    model_ok = ollama_client.model_is_available(settings.llm_model, names)

    embedding = embedding_status()
    stats = registry.stats()

    # 索引是否可用：磁盘上有索引文件，或内存里已经载入
    index_ready = vector_store.ready or vector_store.exists_on_disk()

    problems: list[str] = []
    if not reachable:
        problems.append("Ollama 服务未启动")
    elif not model_ok:
        problems.append(f"Ollama 中未找到模型 {settings.llm_model}")

    # 便携版装了一半：服务能起来、模型也列得出来，但一发提问就
    # "llama-server binary not found"。不点名的话用户根本无从判断。
    portable = portable_ollama_status()
    if portable["present"] and portable["complete"] is False:
        problems.append("Ollama 安装不完整（缺少推理引擎），请重新运行一键准备")

    if not embedding["local_ready"]:
        problems.append("本地嵌入模型缺失，请先执行 scripts\\prepare.ps1")

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
        },
    )


@router.get("/setup", response_model=SetupReport)
async def setup_report(fresh: bool = False) -> SetupReport:
    """首次配置体检。

    前端在启动时调用本接口；只要 ``ready`` 为 false 就弹出引导窗口，
    并把返回的安装命令直接展示给用户复制执行。

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
    """在服务端直接执行环境准备脚本。

    等价于用户手动双击 ``scripts\\prepare.cmd``，但进度会实时推送到网页上。
    同一时间只允许一个任务；重复调用返回 409。
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


@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        llm_model=settings.llm_model,
        llm_temperature=settings.llm_temperature,
        llm_num_ctx=settings.llm_num_ctx,
        embedding_model_name=settings.embedding_model_name,
        embedding_device=settings.embedding_device,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        top_k=settings.top_k,
        score_threshold=settings.score_threshold,
        max_upload_mb=settings.max_upload_mb,
        allowed_extensions=settings.allowed_extensions,
    )


@router.put("/config", response_model=ConfigResponse)
async def update_config(payload: ConfigUpdate) -> ConfigResponse:
    """运行期调整参数。

    只作用于当前进程；要永久生效请写入 ``rag-qa/.env``。
    切换 LLM 模型时会重置已缓存的 ChatOllama 客户端。
    """
    from app.services import rag

    changed_model = False

    if payload.llm_model is not None and payload.llm_model != settings.llm_model:
        settings.llm_model = payload.llm_model
        changed_model = True
    if payload.llm_temperature is not None:
        settings.llm_temperature = payload.llm_temperature
        changed_model = True
    if payload.top_k is not None:
        settings.top_k = payload.top_k
    if payload.score_threshold is not None:
        settings.score_threshold = payload.score_threshold
    if payload.chunk_size is not None:
        settings.chunk_size = payload.chunk_size
    if payload.chunk_overlap is not None:
        settings.chunk_overlap = payload.chunk_overlap
    if payload.expose_thinking is not None:
        settings.expose_thinking = payload.expose_thinking

    if changed_model:
        rag.reset_llm()

    return await get_config()


@router.get("/models")
async def list_models() -> dict[str, object]:
    """列出 Ollama 中已安装的模型（用于前端下拉切换）。"""
    raw = await ollama_client.list_models()
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
