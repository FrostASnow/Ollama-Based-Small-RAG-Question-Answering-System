"""文档摄取：落盘 → 解析 → 切分 → 向量化 → 写入 FAISS + 注册表。"""

from __future__ import annotations

import re
import shutil
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from app.config import settings
from app.core.logging import get_logger
from app.core.paths import INDEX_DIR, UPLOADS_DIR
from app.schemas import DocumentInfo
from app.services import index_health
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

#: 索引备份目录。重建索引是先删后建，中途失败会丢掉分块，故留一份备份。
BACKUP_ROOT = INDEX_DIR / "backups"


def sanitize_filename(name: str) -> str:
    name = Path(name).name  # 去掉路径部分，防止 ../ 目录穿越
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


def record_index_meta() -> None:
    """登记「这份索引是用什么建出来的」。

    维度取模型实际输出的维度而非配置声明值，否则声明值写错时会把错误固化进注册表，
    之后的一致性体检再也发现不了问题。
    """
    from app.services.embeddings import loaded_dimension

    registry.set_index_meta(
        settings.embedding_model_name,
        loaded_dimension() or settings.embedding_dimension,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        parse_options={
            name: bool(getattr(settings, name)) for name in index_health.PARSE_FIELDS
        },
    )
    registry.save()


def ingest_path(
    path: Path,
    original_filename: str,
    size_bytes: int,
    doc_id: str | None = None,
) -> tuple[DocumentInfo, bool]:
    """把一个已经落盘的文件索引进 FAISS，返回 ``(文档信息, 是否重复)``。"""
    digest = sha256_of(path)

    existing = registry.find_by_hash(digest)
    if existing is not None:
        logger.info("检测到重复文档：%s == %s", original_filename, existing.get("filename"))
        path.unlink(missing_ok=True)
        return DocumentInfo(**existing), True

    doc_id = doc_id or uuid.uuid4().hex
    outcome = parse_document(path, doc_id, original_filename)

    faiss_ids = vector_store.add_chunks(outcome.chunks)

    info = DocumentInfo(
        doc_id=doc_id,
        filename=original_filename,
        stored_name=path.name,
        extension=path.suffix.lower(),
        size_bytes=size_bytes,
        sha256=digest,
        chunk_count=len(faiss_ids),
        char_count=outcome.char_count,
        created_at=now_iso(),
        status="indexed",
        warnings=list(outcome.warnings),
    )

    record = info.model_dump()
    record["faiss_ids"] = faiss_ids
    registry.add(record)
    record_index_meta()

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

    # 文档全删光时连索引文件和索引元信息一起清掉，避免下次启动载入空索引报错，
    # 也避免留下指向不存在索引的过期登记。
    if not registry.all():
        vector_store.reset()
        registry.clear_index_meta()

    return removed


def clear_all() -> int:
    """清空知识库，返回删除的向量数。

    ``registry.clear()`` 会连索引元信息一起清掉：索引文件随后就被 reset() 删除，
    元信息留着只会指向一份不存在的索引。
    """
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


# ---------------------------------------------------------------------------
# 索引备份
# ---------------------------------------------------------------------------
def backup_index(keep: int | None = None) -> str | None:
    """把当前索引备份到 ``data/index/backups/<时间戳>/``，无索引可备份时返回 ``None``。

    返回值会出现在重建索引的响应里，界面上能看到「出事去哪儿捞」。
    """
    if not (vector_store.ready or vector_store.exists_on_disk()):
        return None

    target = BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        vector_store.backup(target)
    except Exception as exc:  # noqa: BLE001 - 备份失败不该让重建无法进行
        logger.warning("索引备份失败（继续重建）：%s", exc)
        return None

    _prune_backups(settings.index_backup_keep if keep is None else keep)
    logger.info("索引已备份到：%s", target)
    return str(target)


def _prune_backups(keep: int) -> None:
    """只保留最近 ``keep`` 份备份（目录名就是时间戳，按名字排序即按时间排序）。"""
    if keep < 0 or not BACKUP_ROOT.is_dir():
        return
    backups = sorted((item for item in BACKUP_ROOT.iterdir() if item.is_dir()), key=lambda p: p.name)
    for stale in backups[: max(0, len(backups) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def reindex_all() -> dict[str, Any]:
    """用 ``data/uploads`` 里的原文件按**当前配置**重建全部索引。

    解析/切分参数改动后磁盘上的旧索引不会自动更新，必须重跑。若向量空间变了
    （换过嵌入模型或维度），旧向量既无意义、维度不同时连删除都会出错，只能整体重建；
    只是切分参数变了则逐篇删旧向量再重新入库。
    """
    records = registry.all()
    details: list[dict[str, Any]] = []
    chunks_before = 0
    chunks_after = 0
    failed = 0

    backup_dir: str | None = None
    if settings.backup_before_reindex:
        backup_dir = backup_index()

    compat = vector_store.compatibility()
    rebuild_from_scratch = compat["state"] == "incompatible"
    if rebuild_from_scratch:
        logger.warning(
            "索引与当前嵌入配置不一致，将整体重建（旧的向量已不可用）：%s",
            "；".join(compat["reasons"]),
        )
        vector_store.reset()

    for record in records:
        doc_id = str(record.get("doc_id") or "")
        filename = str(record.get("filename") or record.get("stored_name") or doc_id)
        stored_name = record.get("stored_name")
        old_ids = list(record.get("faiss_ids", []))
        chunks_before += len(old_ids)

        path = UPLOADS_DIR / str(stored_name) if stored_name else None
        if path is None or not path.is_file():
            failed += 1
            details.append(
                {
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "missing",
                    "message": "原始文件已不在 data/uploads，无法重建",
                }
            )
            continue

        try:
            if not rebuild_from_scratch:
                vector_store.delete_ids(old_ids)
            outcome = parse_document(path, doc_id, filename)
            new_ids = vector_store.add_chunks(outcome.chunks)
        except Exception as exc:  # noqa: BLE001
            logger.exception("重建索引失败：%s", filename)
            failed += 1
            details.append(
                {
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "failed",
                    "message": str(exc),
                }
            )
            continue

        chunks_after += len(new_ids)
        updated = dict(record)
        updated.update(
            {
                "chunk_count": len(new_ids),
                "char_count": outcome.char_count,
                "faiss_ids": new_ids,
                "status": "indexed",
                "error": None,
                # 警告跟着本次解析结果走：换了文字版后旧警告必须消失，否则误导人
                "warnings": list(outcome.warnings),
            }
        )
        registry.add(updated)
        details.append(
            {
                "doc_id": doc_id,
                "filename": filename,
                "status": "ok",
                "chunks_before": len(old_ids),
                "chunks_after": len(new_ids),
                "char_count": outcome.char_count,
                "warnings": list(outcome.warnings),
            }
        )

    record_index_meta()
    vector_store.persist()

    return {
        "documents": len(records),
        "rebuilt": len(records) - failed,
        "failed": failed,
        "chunks_before": chunks_before,
        "chunks_after": chunks_after,
        "index_state_before": compat["state"],
        "rebuilt_from_scratch": rebuild_from_scratch,
        "backup_dir": backup_dir,
        "details": details,
    }


__all__ = [
    "IngestError",
    "DocumentParseError",
    "UnsupportedFileTypeError",
    "save_upload",
    "ingest_path",
    "delete_document",
    "clear_all",
    "reindex_all",
    "backup_index",
    "record_index_meta",
    "sanitize_filename",
    "BACKUP_ROOT",
]
