"""文档摄取：落盘 → 解析 → 切分 → 向量化 → 写入 FAISS + 注册表。"""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings
from app.core.logging import get_logger
from app.core.paths import UPLOADS_DIR
from app.schemas import DocumentInfo
from app.services.loader import (
    DocumentParseError,
    UnsupportedFileTypeError,
    parse_document,
    sha256_of,
)
from app.services.registry import now_iso, registry
from app.services.vectorstore import vector_store

logger = get_logger(__name__)

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """只用于展示与磁盘命名，去掉路径分隔符和非法字符。"""
    name = Path(name).name  # 防止 ../ 目录穿越
    name = unicodedata.normalize("NFKC", name)
    name = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
    return name or "unnamed"


def _unique_stored_name(doc_id: str, safe_name: str) -> str:
    suffix = Path(safe_name).suffix.lower()
    stem = Path(safe_name).stem[:60]
    return f"{stem}__{doc_id[:8]}{suffix}"


class IngestError(RuntimeError):
    pass


async def save_upload(upload: UploadFile) -> tuple[Path, str, int]:
    """把上传流写入 ``data/uploads``，返回 ``(路径, 原始文件名, 字节数)``。"""
    original = sanitize_filename(upload.filename or "unnamed")
    ext = Path(original).suffix.lower()

    if ext not in settings.allowed_extensions:
        raise IngestError(
            f"不支持的文件类型：{ext or '无扩展名'}；"
            f"支持 {', '.join(settings.allowed_extensions)}"
        )

    doc_id = uuid.uuid4().hex
    stored_name = _unique_stored_name(doc_id, original)
    target = UPLOADS_DIR / stored_name
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    try:
        with target.open("wb") as handle:
            while True:
                block = await upload.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise IngestError(f"文件超过 {settings.max_upload_mb} MB 上限")
                handle.write(block)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        target.unlink(missing_ok=True)
        raise IngestError("上传的文件为空")

    return target, original, size


def ingest_path(
    path: Path,
    original_filename: str,
    size_bytes: int,
    doc_id: str | None = None,
) -> tuple[DocumentInfo, bool]:
    """把一个已经落盘的文件索引进 FAISS。

    返回 ``(文档信息, 是否为重复文件)``。
    """
    digest = sha256_of(path)

    # 内容相同的文件不重复索引，直接复用已有记录
    existing = registry.find_by_hash(digest)
    if existing is not None:
        logger.info("检测到重复文档：%s == %s", original_filename, existing.get("filename"))
        path.unlink(missing_ok=True)
        return DocumentInfo(**existing), True

    doc_id = doc_id or uuid.uuid4().hex
    chunks, char_count = parse_document(path, doc_id, original_filename)

    faiss_ids = vector_store.add_chunks(chunks)

    info = DocumentInfo(
        doc_id=doc_id,
        filename=original_filename,
        stored_name=path.name,
        extension=path.suffix.lower(),
        size_bytes=size_bytes,
        sha256=digest,
        chunk_count=len(faiss_ids),
        char_count=char_count,
        created_at=now_iso(),
        status="indexed",
    )

    record = info.model_dump()
    record["faiss_ids"] = faiss_ids
    registry.add(record)
    registry.set_index_meta(settings.embedding_model_name, settings.embedding_dimension)
    registry.save()

    return info, False


def delete_document(doc_id: str) -> int:
    """删除文档：移除向量、注册表记录和磁盘文件。返回删除的向量数。"""
    record = registry.get(doc_id)
    if record is None:
        raise IngestError("文档不存在")

    removed = vector_store.delete_ids(list(record.get("faiss_ids", [])))

    stored_name = record.get("stored_name")
    if stored_name:
        target = UPLOADS_DIR / stored_name
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除上传文件失败 %s：%s", target, exc)

    registry.remove(doc_id)

    # 全部文档都被删掉时，清理空索引文件，避免下次启动载入空索引报错
    if not registry.all():
        vector_store.reset()

    return removed


def clear_all() -> int:
    """清空知识库，返回删除的向量数。"""
    records = registry.all()
    total_ids: list[str] = []
    for record in records:
        total_ids.extend(record.get("faiss_ids", []))
        stored_name = record.get("stored_name")
        if stored_name:
            try:
                (UPLOADS_DIR / stored_name).unlink(missing_ok=True)
            except OSError:
                pass

    removed = vector_store.delete_ids(total_ids)
    registry.clear()
    vector_store.reset()
    return removed


__all__ = [
    "IngestError",
    "DocumentParseError",
    "UnsupportedFileTypeError",
    "save_upload",
    "ingest_path",
    "delete_document",
    "clear_all",
    "sanitize_filename",
]
