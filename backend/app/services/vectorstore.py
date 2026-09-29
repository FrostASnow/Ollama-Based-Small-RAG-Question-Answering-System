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

import re
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

# 近重复检测用的字符 n-gram 长度。中文没有空格分词，按 4 字滑窗做「集合指纹」
# 比按词更稳，而且完全不需要分词库。
_SHINGLE_SIZE = 4
# 候选池上限：去重是 O(n^2) 的集合比较，池子必须有界，否则单次提问会被拖慢
_MAX_CANDIDATES = 120


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
        sources, _info = self.search_detailed(query, top_k, score_threshold, doc_ids)
        return sources

    def search_detailed(
        self,
        query: str,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> tuple[list[SourceChunk], dict[str, Any]]:
        """检索并返回诊断信息 ``(片段, info)``。

        相比单纯的「分数 < 阈值就丢弃」，这里做了三件事：

        1. **相对窗口裁剪**：只保留与最佳片段相差不超过 ``score_window`` 的候选。
           all-MiniLM-L6-v2 的分数分布很扁，无关内容也能到 0.25，
           单靠绝对阈值区分不了「都相关」与「一片噪声」。
        2. **近重复去重**：期刊 PDF 的页眉会被切进多个 chunk，
           它们彼此几乎一样，只会白占 top_k 的槽位。
        3. **兜底放宽**：阈值内一条都没有时，退到 ``max(score_floor, 最佳-窗口)``
           再试一次，并把 ``relaxed`` 标出来给界面提示。
           否则「总结一下」这类问法（最佳仅 0.27）会直接被判成「无法回答」。

        ``score_threshold <= 0`` 表示调用方明确要求「不要过滤」，此时不做窗口裁剪，
        也不放宽（没有意义）。
        """
        store = self.ensure_loaded()
        info: dict[str, Any] = {
            "mode": "qa",
            "candidates": 0,
            "best_score": 0.0,
            "effective_threshold": 0.0,
            "relaxed": False,
            "dropped_duplicates": 0,
            "chunks_total": self.size,
        }
        if store is None or self.size == 0:
            return [], info

        k = top_k or settings.top_k
        threshold = settings.score_threshold if score_threshold is None else score_threshold
        ntotal = self.size

        # 需要按文档过滤时，先多取一些候选再筛
        fetch_k = min(ntotal, max(k * 8, 40), _MAX_CANDIDATES) if doc_ids else min(
            ntotal, max(k * 5, 20), _MAX_CANDIDATES
        )

        query_vector = get_embeddings().embed_query(query)
        pairs = store.similarity_search_with_score_by_vector(query_vector, k=fetch_k)

        wanted = set(doc_ids) if doc_ids else None
        candidates: list[tuple[Document, float]] = []
        for doc, score in pairs:
            meta = doc.metadata or {}
            if wanted is not None and meta.get("doc_id") not in wanted:
                continue
            candidates.append((doc, float(score)))

        info["candidates"] = len(candidates)
        if not candidates:
            return [], info

        deduped, dropped = _dedupe_pairs(candidates, settings.dedupe_ratio)
        info["dropped_duplicates"] = dropped
        best = max(score for _doc, score in deduped)
        info["best_score"] = round(best, 4)

        # 相对窗口只与「正向阈值」配合使用；threshold <= 0 视为关闭过滤
        window = settings.score_window if threshold > 0 else 0.0
        effective = max(threshold, best - window) if window > 0 else threshold

        kept = [item for item in deduped if item[1] >= effective]

        if not kept and 0 < threshold <= settings.score_relax_limit:
            relaxed_floor = (
                max(settings.score_floor, best - window) if window > 0 else settings.score_floor
            )
            kept = [item for item in deduped if item[1] >= relaxed_floor]
            if kept:
                effective = relaxed_floor
                info["relaxed"] = True

        info["effective_threshold"] = round(effective, 4)
        if info["relaxed"]:
            logger.info(
                "阈值 %.2f 无命中，已放宽到 %.2f（最佳 %.3f，候选 %d）",
                threshold,
                effective,
                best,
                len(candidates),
            )

        results: list[SourceChunk] = []
        for doc, score in kept[:k]:
            meta = doc.metadata or {}
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

        for i, item in enumerate(results, start=1):
            item.index = i
        info["selected"] = len(results)
        return results, info

    def overview_chunks(
        self,
        query: str = "",
        doc_ids: list[str] | None = None,
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> tuple[list[SourceChunk], dict[str, Any]]:
        """为「总结 / 概述」类问题按全篇均匀取样，而不是按相似度取 top-k。

        这类问题问的是整篇文档的要点，和任何一个片段都不相似（实测中文短问句
        「总结一下」的最佳相似度只有 0.27，比随机噪声高不了多少），
        用阈值筛必然漏。既然文档已经确定，就直接把全篇摊开给模型看：
        按阅读顺序均匀取 ``summary_max_chunks`` 段，先去掉近重复的页眉副本，
        再按 ``max_context_chars`` 的预算装箱。
        """
        store = self.ensure_loaded()
        info: dict[str, Any] = {
            "mode": "overview",
            "candidates": 0,
            "best_score": 0.0,
            "effective_threshold": 0.0,
            "relaxed": False,
            "dropped_duplicates": 0,
            "chunks_total": self.size,
            "selected": 0,
        }
        if store is None or self.size == 0:
            return [], info

        wanted = set(doc_ids) if doc_ids else None
        entries = self._ordered_entries(wanted)
        info["candidates"] = len(entries)
        if not entries:
            return [], info

        unique, dropped = _dedupe_entries(entries, settings.dedupe_ratio)
        info["dropped_duplicates"] = dropped

        budget = settings.context_char_budget if max_chars is None else max_chars
        want = min(limit or settings.summary_max_chunks, len(unique))
        if want <= 0:
            return [], info

        # 按文档分组取样：「用三句话总结这些文档」时，如果把所有块拉平后
        # 按长度均匀取样，一篇 131 块的论文会把 11 块的小文档整个挤掉 ——
        # 总结里就只剩长的这篇。所以先给每篇文档保底名额，再按块数分剩余额度。
        groups: list[list[tuple[str, Document]]] = []
        position_of: dict[str, int] = {}
        for entry in unique:
            doc_id = str((entry[1].metadata or {}).get("doc_id", ""))
            if doc_id not in position_of:
                position_of[doc_id] = len(groups)
                groups.append([])
            groups[position_of[doc_id]].append(entry)

        quotas = _allocate_quotas([len(group) for group in groups], want)
        info["documents"] = len(groups)
        info["quotas"] = dict(
            zip(
                (
                    str((group[0][1].metadata or {}).get("filename", "?"))
                    for group in groups
                ),
                quotas,
            )
        )

        scores: dict[str, float] = {}
        if query:
            try:
                scores = self._score_map(get_embeddings().embed_query(query))
            except Exception as exc:  # noqa: BLE001
                logger.debug("概览取样打分失败（不影响取样）：%s", exc)

        results: list[SourceChunk] = []
        used = 0
        for group, quota in zip(groups, quotas):
            for docstore_id, doc in _evenly_pick(group, quota):
                body = doc.page_content.strip()
                if not body:
                    continue
                if results and used + len(body) > budget:
                    continue
                meta = doc.metadata or {}
                results.append(
                    SourceChunk(
                        index=len(results) + 1,
                        doc_id=str(meta.get("doc_id", "")),
                        filename=str(meta.get("filename", "未知文档")),
                        page=meta.get("page"),
                        chunk_index=meta.get("chunk_index"),
                        score=round(scores.get(docstore_id, 0.0), 4),
                        content=body,
                    )
                )
                used += len(body)

        if results:
            info["best_score"] = round(max(item.score for item in results), 4)
        info["selected"] = len(results)
        return results, info

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ordered_entries(self, wanted: set[str] | None) -> list[tuple[str, Document]]:
        """按索引写入顺序（≈ 文档阅读顺序）列出所有片段。"""
        store = self.ensure_loaded()
        if store is None:
            return []
        entries: list[tuple[str, Document]] = []
        for vector_id in sorted(store.index_to_docstore_id):
            docstore_id = store.index_to_docstore_id[vector_id]
            try:
                doc = store.docstore.search(docstore_id)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, Document):
                continue
            meta = doc.metadata or {}
            if wanted is not None and meta.get("doc_id") not in wanted:
                continue
            entries.append((docstore_id, doc))
        return entries

    def _score_map(self, query_vector: list[float]) -> dict[str, float]:
        """返回 ``docstore_id -> 余弦相似度``（用于概览取样时补齐真实分数）。"""
        import numpy as np

        store = self.ensure_loaded()
        if store is None or self.size == 0:
            return {}
        vector = np.asarray(query_vector, dtype="float32").reshape(1, -1)
        scores, indices = store.index.search(vector, self.size)
        mapping: dict[str, float] = {}
        for score, index in zip(scores[0], indices[0]):
            key = int(index)
            if key < 0:
                continue
            docstore_id = store.index_to_docstore_id.get(key)
            if docstore_id is not None:
                mapping[docstore_id] = float(score)
        return mapping

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


# ---------------------------------------------------------------------------
# 近重复片段去重
# ---------------------------------------------------------------------------
# 期刊 PDF 的页眉会被切进多个 chunk（每页一份副本），它们对任何提问的相似度
# 都不低，于是 top_k 里塞进好几条一模一样的样板文本，正文反而进不来。
# 这里用字符 4-gram 集合的 Jaccard 相似度判断「几乎是同一段话」。
def _shingles(text: str, size: int = _SHINGLE_SIZE) -> frozenset[str]:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return frozenset()
    if len(compact) <= size:
        return frozenset({compact})
    return frozenset(compact[i : i + size] for i in range(len(compact) - size + 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if not intersection:
        return 0.0
    return intersection / len(left | right)


def _near_duplicate(signal: frozenset[str], length: int, priors: list[tuple[frozenset[str], int]], ratio: float) -> bool:
    for prior, prior_length in priors:
        # 长度差太多的两段话不可能是副本，先做廉价剪枝
        if length and prior_length:
            shorter, longer = sorted((length, prior_length))
            if shorter / longer < ratio:
                continue
        if _jaccard(signal, prior) >= ratio:
            return True
    return False


def _dedupe_pairs(
    pairs: list[tuple[Document, float]], ratio: float
) -> tuple[list[tuple[Document, float]], int]:
    """按分数降序（调用方已排好）保留每个近重复组里分数最高的那条。"""
    if ratio <= 0:
        return pairs, 0
    kept: list[tuple[Document, float]] = []
    priors: list[tuple[frozenset[str], int]] = []
    dropped = 0
    for doc, score in pairs:
        text = doc.page_content or ""
        signal = _shingles(text)
        if _near_duplicate(signal, len(text), priors, ratio):
            dropped += 1
            continue
        kept.append((doc, score))
        priors.append((signal, len(text)))
    return kept, dropped


def _dedupe_entries(
    entries: list[tuple[str, Document]], ratio: float
) -> tuple[list[tuple[str, Document]], int]:
    """同上，但作用于 ``(docstore_id, Document)`` 且保持原顺序。"""
    if ratio <= 0:
        return entries, 0
    kept: list[tuple[str, Document]] = []
    priors: list[tuple[frozenset[str], int]] = []
    dropped = 0
    for docstore_id, doc in entries:
        text = doc.page_content or ""
        signal = _shingles(text)
        if _near_duplicate(signal, len(text), priors, ratio):
            dropped += 1
            continue
        kept.append((docstore_id, doc))
        priors.append((signal, len(text)))
    return kept, dropped


def _evenly_pick(
    items: list[tuple[str, Document]], quota: int
) -> list[tuple[str, Document]]:
    """在保持顺序的前提下均匀取 ``quota`` 条（首尾必取）。"""
    if quota <= 0 or not items:
        return []
    if len(items) <= quota:
        return list(items)
    if quota == 1:
        return [items[0]]
    indices = sorted({round(i * (len(items) - 1) / (quota - 1)) for i in range(quota)})
    return [items[index] for index in indices]


def _allocate_quotas(sizes: list[int], budget: int) -> list[int]:
    """把 ``budget`` 个取样名额分配到若干文档上。

    规则：先留出一半名额在所有文档间平均分配（每篇至少 1 块），
    剩下的按各文档的块数比例分配，并保证不超过该文档实际块数。
    这样「总结这些文档」既能覆盖到短文档，又会给长文档更多篇幅。
    """
    count = len(sizes)
    if count == 0 or budget <= 0:
        return [0] * count

    quotas = [0] * count
    remaining = budget
    base = max(1, budget // (2 * count))
    for index, size in enumerate(sizes):
        take = min(base, size, remaining)
        quotas[index] = take
        remaining -= take

    total = sum(sizes) or 1
    if remaining > 0:
        shares = [remaining * size / total for size in sizes]
        for index, share in enumerate(shares):
            add = min(int(share), sizes[index] - quotas[index])
            quotas[index] += add
            remaining -= add
        for index in sorted(range(count), key=lambda i: shares[i] - int(shares[i]), reverse=True):
            if remaining <= 0:
                break
            if quotas[index] < sizes[index]:
                quotas[index] += 1
                remaining -= 1
    return quotas
