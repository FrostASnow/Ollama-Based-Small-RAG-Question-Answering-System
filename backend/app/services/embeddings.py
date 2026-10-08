"""Embeddings 服务：本地离线加载嵌入模型，懒加载 + 线程安全。
``normalize_embeddings`` 使 FAISS 内积等价于余弦相似度。
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
#: 模型**实际**输出的向量维度（载入后探测得到）。
#: 不能用 settings.embedding_dimension：那是手写声明值，换模型忘了同步就会与实际不符，
#: 而索引一致性判断完全依赖这个数字。
_dimension: int | None = None

#: 探测维度用的文本，只是为了让模型跑一次前向。
_DIMENSION_PROBE = "维度探测"


class EmbeddingNotReadyError(RuntimeError):
    """本地嵌入模型缺失，且当前离线，无法自动下载。"""


def _build() -> Any:
    global _dimension
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

    # 立刻探测真实维度（一次前向）。不要省掉：换模型后维度与旧索引不一致时，
    # FAISS 只会抛一句看不出原因的断言错误。
    try:
        _dimension = len(embeddings.embed_query(_DIMENSION_PROBE))
    except Exception as exc:  # noqa: BLE001 - 探测失败不影响基本使用
        _dimension = None
        logger.warning("嵌入模型维度探测失败（忽略）：%s", exc)
        return embeddings

    logger.info("嵌入模型实际维度：%d", _dimension)
    if _dimension != settings.embedding_dimension:
        logger.warning(
            "配置 RAG_EMBEDDING_DIMENSION=%d 与模型实际维度 %d 不一致。"
            "程序按实际维度工作，但建议把配置改成 %d（或留空由程序推导）。",
            settings.embedding_dimension,
            _dimension,
            _dimension,
        )
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
    """加载失败时返回 None 而不是抛异常（体检用）；体检报告不该整体变成 500。"""
    try:
        return get_embeddings()
    except Exception:  # noqa: BLE001
        return None


def loaded_dimension() -> int | None:
    """已载入模型的实际向量维度；尚未载入或探测失败时为 None。"""
    return _dimension


def reset_instance() -> None:
    """丢弃已加载的模型，让下次调用重新加载。
    运行期换模型必须调用：不丢的话向量化继续用旧模型，而一致性检查还以为一切正常。
    """
    global _instance, _load_error, _dimension
    with _lock:
        _instance = None
        _load_error = None
        _dimension = None
    logger.info("嵌入模型实例已重置，下次使用时重新加载")


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
        # dimension 是配置声明的，actual_dimension 是模型真实输出的；两者不一致
        # 说明索引可能被记成错误的维度，界面要能看出来。
        "dimension": settings.embedding_dimension,
        "actual_dimension": _dimension,
        "error": _load_error,
    }
