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
from app.services.ingest import IngestError, clear_all, delete_document, ingest_path, save_upload
from app.services.loader import DocumentParseError, UnsupportedFileTypeError
from app.services.registry import registry
from app.services.vectorstore import vector_store

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
            results.append(
                UploadResponse(
                    document=info,
                    message=(
                        f"《{original}》内容已存在，复用已有索引"
                        if duplicated
                        else f"《{original}》索引完成，共 {info.chunk_count} 个分块"
                    ),
                )
            )
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

    # 只要有文件成功入库就落一次盘
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
