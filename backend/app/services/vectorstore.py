"""FAISS 向量库管理。

* 使用 ``IndexFlatIP``（内积）作为距离度量。
* **归一化在 Embeddings 层完成**（``encode_kwargs={"normalize_embeddings": True}``），
  因此内积等价于余弦相似度，分数落在 [-1, 1]，越高越相关。
  这里刻意不传 ``normalize_L2=True``：langchain 在 ``MAX_INNER_PRODUCT``
  下会忽略该参数并打印告警，属于误导性配置。
* 索引与 docstore 持久化在 ``data/index/``，重启服务自动恢复。
* 所有写操作加锁，读操作无锁（FAISS 查询本身线程安全）。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from app.config import settings
from app.core.logging import get_logger
from app.core.paths import FAISS_INDEX_NAME, INDEX_DIR
from app.schemas import SourceChunk
from app.services.embeddings import get_embeddings

logger = get_logger(__name__)


class VectorStoreManager:
    """FAISS 索引的单例管理器。"""

    def __init__(self) -> None:
        self._store: Any | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 建库 / 载入
    # ------------------------------------------------------------------
    @property
    def index_file(self) -> Path:
        return INDEX_DIR / f"{FAISS_INDEX_NAME}.faiss"

    @property
    def store_file(self) -> Path:
        return INDEX_DIR / f"{FAISS_INDEX_NAME}.pkl"

    def exists_on_disk(self) -> bool:
        return self.index_file.is_file() and self.store_file.is_file()

    def load(self, force: bool = False) -> Any | None:
        """从磁盘载入索引；不存在则返回 None。"""
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy

        with self._lock:
            if self._store is not None and not force:
                return self._store
            if not self.exists_on_disk():
                return None

            logger.info("载入 FAISS 索引：%s", self.index_file)
            self._store = FAISS.load_local(
                folder_path=str(INDEX_DIR),
                embeddings=get_embeddings(),
                index_name=FAISS_INDEX_NAME,
                allow_dangerous_deserialization=True,  # 索引由本程序生成，可信
                distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
            )
            logger.info("FAISS 索引载入完成，向量数=%d", self.size)
            return self._store

    def ensure_loaded(self) -> Any | None:
        return self._store if self._store is not None else self.load()

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------
    def add_chunks(self, chunks: list[Document]) -> list[str]:
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy

        if not chunks:
            return []

        with self._lock:
            embeddings = get_embeddings()
            if self._store is None:
                self._store = FAISS.from_documents(
                    documents=chunks,
                    embedding=embeddings,
                    distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
                )
                ids = list(self._store.index_to_docstore_id.values())
            else:
                ids = self._store.add_documents(chunks)

            self.persist_locked()
            logger.info("新增 %d 个向量，索引总量=%d", len(chunks), self.size)
            return list(ids)

    def delete_ids(self, ids: list[str]) -> int:
        """按 chunk id 删除向量；返回实际删除数量。

        会先过滤掉索引里已不存在的 id：注册表与索引可能因异常中断而不一致，
        直接把不存在的 id 交给 FAISS 会抛错，导致整批删除失败（连带删掉正常的部分）。
        """
        if not ids:
            return 0
        with self._lock:
            if self._store is None:
                return 0

            existing = set(self._store.index_to_docstore_id.values())
            targets = [item for item in ids if item in existing]

            if not targets:
                logger.debug("待删除的 %d 个 id 均不在索引中，跳过", len(ids))
                return 0

            if len(targets) != len(ids):
                logger.debug("过滤掉 %d 个不存在的 id", len(ids) - len(targets))

            before = self.size
            try:
                self._store.delete(targets)
            except Exception as exc:  # noqa: BLE001
                logger.warning("FAISS 删除失败：%s", exc)
                return 0
            self.persist_locked()
            removed = before - self.size
            logger.info("删除 %d 个向量，索引总量=%d", removed, self.size)
            return removed

    def persist_locked(self) -> None:
        if self._store is None:
            return
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        self._store.save_local(folder_path=str(INDEX_DIR), index_name=FAISS_INDEX_NAME)

    def persist(self) -> None:
        with self._lock:
            self.persist_locked()

    def reset(self) -> None:
        """清空内存索引并删除磁盘文件。"""
        with self._lock:
            self._store = None
            for path in (self.index_file, self.store_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("删除索引文件失败 %s：%s", path, exc)
            logger.info("FAISS 索引已重置")

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        if self._store is None:
            return 0
        try:
            return int(self._store.index.ntotal)
        except Exception:  # noqa: BLE001
            return 0

    @property
    def ready(self) -> bool:
        return self._store is not None and self.size > 0

    def search(
        self,
        query: str,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> list[SourceChunk]:
        """向量检索，返回按相似度降序排列的引用片段。"""
        store = self.ensure_loaded()
        if store is None or self.size == 0:
            return []

        k = top_k or settings.top_k
        threshold = settings.score_threshold if score_threshold is None else score_threshold
        ntotal = self.size

        # 需要按文档过滤时，先多取一些候选再筛
        search_k = min(ntotal, max(k * 20, 60)) if doc_ids else min(ntotal, k)

        query_vector = get_embeddings().embed_query(query)
        pairs = store.similarity_search_with_score_by_vector(query_vector, k=search_k)

        wanted = set(doc_ids) if doc_ids else None
        results: list[SourceChunk] = []
        for doc, score in pairs:
            meta = doc.metadata or {}
            if wanted is not None and meta.get("doc_id") not in wanted:
                continue
            if score < threshold:
                continue
            results.append(
                SourceChunk(
                    index=0,  # 之后统一编号
                    doc_id=str(meta.get("doc_id", "")),
                    filename=str(meta.get("filename", "未知文档")),
                    page=meta.get("page"),
                    chunk_index=meta.get("chunk_index"),
                    score=round(float(score), 4),
                    content=doc.page_content,
                )
            )
            if len(results) >= k:
                break

        for i, item in enumerate(results, start=1):
            item.index = i
        return results

    def search_documents(self, query: str, k: int = 8) -> list[tuple[Document, float]]:
        """返回原始 (Document, score)，供调试接口使用。"""
        store = self.ensure_loaded()
        if store is None or self.size == 0:
            return []
        query_vector = get_embeddings().embed_query(query)
        return store.similarity_search_with_score_by_vector(query_vector, k=min(self.size, k))

    def chunks_of(self, doc_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """列出某个文档已索引的分块（按 chunk_index 排序），用于核对切分质量。"""
        store = self.ensure_loaded()
        if store is None:
            return []

        collected: list[dict[str, Any]] = []
        for vector_id, docstore_id in store.index_to_docstore_id.items():
            try:
                doc = store.docstore.search(docstore_id)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, Document):
                continue
            meta = doc.metadata or {}
            if meta.get("doc_id") != doc_id:
                continue
            collected.append(
                {
                    "faiss_id": docstore_id,
                    "vector_id": vector_id,
                    "chunk_index": meta.get("chunk_index"),
                    "page": meta.get("page"),
                    "char_count": meta.get("char_count", len(doc.page_content)),
                    "content": doc.page_content,
                }
            )

        collected.sort(key=lambda item: (item.get("page") or 0, item.get("chunk_index") or 0))
        return collected[:limit]

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "vectors": self.size,
            "index_file": str(self.index_file),
            "exists_on_disk": self.exists_on_disk(),
        }

    def backup(self, target_dir: Path) -> None:
        with self._lock:
            self.persist_locked()
            target_dir.mkdir(parents=True, exist_ok=True)
            for path in (self.index_file, self.store_file):
                if path.is_file():
                    shutil.copy2(path, target_dir / path.name)


vector_store = VectorStoreManager()
