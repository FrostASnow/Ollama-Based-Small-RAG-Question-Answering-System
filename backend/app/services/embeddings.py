"""Embeddings 服务：本地加载 all-MiniLM-L6-v2（HuggingFace / sentence-transformers）。

设计要点
--------
1. **完全离线**：优先从 ``models/all-MiniLM-L6-v2`` 本地目录加载，
   配合 ``HF_HUB_OFFLINE=1`` 保证进程不做任何网络请求。
2. **懒加载 + 线程安全**：首次使用时才载入模型（约 2~4 秒），
   之后复用同一个实例，避免每次请求重复占用内存。
3. **向量归一化**：开启 ``normalize_embeddings``，使 FAISS 的内积等价于余弦相似度，
   分数区间落在 [-1, 1]，便于设置阈值与展示。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_instance: Any | None = None
_load_error: str | None = None


class EmbeddingNotReadyError(RuntimeError):
    """本地嵌入模型缺失，且当前离线，无法自动下载。"""


def _build() -> Any:
    # 延迟导入：确保 app.config 已经设置好离线环境变量
    from langchain_huggingface import HuggingFaceEmbeddings

    source = settings.embedding_source()
    is_local = source == str(settings.embedding_dir)

    if not is_local and not settings.is_embedding_ready():
        raise EmbeddingNotReadyError(
            "本地嵌入模型不存在，且当前处于离线模式。\n"
            f"期望路径：{settings.embedding_dir}\n"
            "请先在联网环境执行： scripts\\prepare.ps1"
        )

    logger.info("加载嵌入模型：%s (device=%s)", source, settings.embedding_device)
    started = time.perf_counter()

    embeddings = HuggingFaceEmbeddings(
        model_name=source,
        cache_folder=str(settings.embedding_dir.parent),
        model_kwargs={"device": settings.embedding_device},
        encode_kwargs={
            "normalize_embeddings": True,
            "batch_size": settings.embedding_batch_size,
        },
        show_progress=False,
    )

    elapsed = time.perf_counter() - started
    logger.info("嵌入模型加载完成，耗时 %.2fs", elapsed)
    return embeddings


def get_embeddings() -> Any:
    """返回全局唯一的 Embeddings 实例（首次调用时加载）。"""
    global _instance, _load_error
    if _instance is not None:
        return _instance

    with _lock:
        if _instance is not None:
            return _instance
        try:
            _instance = _build()
            _load_error = None
        except Exception as exc:  # noqa: BLE001 - 需要把原因透出给健康检查
            _load_error = str(exc)
            logger.error("嵌入模型加载失败：%s", exc)
            raise
    return _instance


def try_get_embeddings() -> Any | None:
    """健康检查用：加载失败时返回 None 而不是抛异常。"""
    try:
        return get_embeddings()
    except Exception:  # noqa: BLE001
        return None


def warmup() -> bool:
    """预热：把模型载入内存，避免用户第一条提问等待过久。"""
    try:
        embeddings = get_embeddings()
        vector = embeddings.embed_query("warmup")
        logger.info("嵌入预热完成，向量维度=%d", len(vector))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("嵌入预热失败：%s", exc)
        return False


def embedding_status() -> dict[str, Any]:
    return {
        "model_name": settings.embedding_model_name,
        "local_dir": str(settings.embedding_dir),
        "local_ready": settings.is_embedding_ready(),
        "loaded": _instance is not None,
        "device": settings.embedding_device,
        "dimension": settings.embedding_dimension,
        "error": _load_error,
    }
