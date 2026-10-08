"""文档管理接口：上传、列表、详情、删除。"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.logging import get_logger
from app.schemas import (
    DeleteResponse,
    DocumentInfo,
    DocumentListResponse,
    UploadResponse,
)
from app.services.ingest import (
    IngestError,
    clear_all,
    delete_document,
    ingest_path,
    reindex_all,
    save_upload,
)
from app.services.loader import DocumentParseError, UnsupportedFileTypeError
from app.services.registry import registry
from app.services.vectorstore import IndexIncompatibleError, vector_store

logger = get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=DocumentListResponse)
async def list_documents() -> DocumentListResponse:
    records = registry.all()
    documents = [DocumentInfo(**record) for record in records]
    return DocumentListResponse(
        documents=documents,
        total=len(documents),
        total_chunks=sum(d.chunk_count for d in documents),
    )


@router.post("/upload", response_model=list[UploadResponse])
async def upload_documents(files: list[UploadFile] = File(...)) -> list[UploadResponse]:
    """上传并索引一个或多个文档。

    单个文件失败不会中断整批；每个文件各自返回结果或错误。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何文件")

    results: list[UploadResponse] = []

    for upload in files:
        name = upload.filename or "unnamed"
        try:
            path, original, size = await save_upload(upload)
            info, duplicated = ingest_path(path, original, size)
            if duplicated:
                message = f"《{original}》内容已存在，复用已有索引"
            else:
                message = f"《{original}》索引完成，共 {info.chunk_count} 个分块"
            # 解析警告必须出现在用户看得到的地方（上传结果就是第一现场）：
            # 只在日志里 warning 一句，用户只会看到「索引完成」。
            if info.warnings:
                message += "；⚠ " + "；".join(info.warnings)
            results.append(UploadResponse(document=info, message=message))
        except (IngestError, UnsupportedFileTypeError, DocumentParseError) as exc:
            logger.warning("入库失败 %s：%s", name, exc)
            results.append(
                UploadResponse(
                    document=DocumentInfo(
                        doc_id="",
                        filename=name,
                        stored_name="",
                        extension="",
                        size_bytes=0,
                        sha256="",
                        chunk_count=0,
                        char_count=0,
                        created_at="",
                        status="failed",
                        error=str(exc),
                    ),
                    message=f"《{name}》入库失败：{exc}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("入库异常 %s", name)
            results.append(
                UploadResponse(
                    document=DocumentInfo(
                        doc_id="",
                        filename=name,
                        stored_name="",
                        extension="",
                        size_bytes=0,
                        sha256="",
                        chunk_count=0,
                        char_count=0,
                        created_at="",
                        status="failed",
                        error=str(exc),
                    ),
                    message=f"《{name}》入库异常：{exc}",
                )
            )

    vector_store.persist()
    return results


@router.get("/{doc_id}/chunks")
async def document_chunks(doc_id: str, limit: int = 50) -> dict[str, object]:
    """查看某个文档已索引的分块内容（用于核对切分质量）。"""
    record = registry.get(doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    chunks = vector_store.chunks_of(doc_id, limit=limit)
    return {
        "doc_id": doc_id,
        "filename": record.get("filename"),
        "chunk_count": record.get("chunk_count"),
        "returned": len(chunks),
        "chunks": chunks,
    }


@router.post("/reindex")
def reindex_documents() -> dict[str, object]:
    """按当前的解析 / 切分 / 页眉过滤配置，用原文件重建整个索引。

    同步函数：解析 + 向量化是阻塞的 CPU 操作，交给 FastAPI 的线程池执行，
    避免卡住事件循环（否则重建期间前端连健康检查都拿不到响应）。
    """
    if not registry.all():
        raise HTTPException(status_code=409, detail="知识库为空，无需重建")
    try:
        report = reindex_all()
    except IndexIncompatibleError as exc:  # pragma: no cover - 正常会自动整体重建
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "重建索引完成：%s 个文档 / %s 块 -> %s 块（失败 %s，索引状态 %s，备份 %s）",
        report["documents"],
        report["chunks_before"],
        report["chunks_after"],
        report["failed"],
        report.get("index_state_before"),
        report.get("backup_dir") or "无",
    )
    return report


@router.delete("/{doc_id}", response_model=DeleteResponse)
async def remove_document(doc_id: str) -> DeleteResponse:
    try:
        removed = delete_document(doc_id)
    except IngestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return DeleteResponse(
        doc_id=doc_id,
        deleted_chunks=removed,
        message=f"已删除文档及其 {removed} 个分块",
    )


@router.delete("", response_model=DeleteResponse)
async def remove_all() -> DeleteResponse:
    removed = clear_all()
    return DeleteResponse(
        doc_id="*",
        deleted_chunks=removed,
        message=f"知识库已清空，共删除 {removed} 个分块",
    )
