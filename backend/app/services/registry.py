"""文档注册表：记录每个上传文档的元信息与其在 FAISS 中的 chunk id。
持久化到 ``data/index/registry.json``，用「临时文件 + os.replace」原子替换以防写坏。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.core.paths import REGISTRY_FILE

logger = get_logger(__name__)

# 版本 2：索引元信息里增加了切分参数与解析开关。旧文件不必迁移：
# 读取一律走 .get()，缺的字段就是 None（=「不知道」）。
_SCHEMA_VERSION = 2

#: 「这份索引是用什么建出来的」的全部登记项。
#: 它们描述的是**磁盘上那份索引**，所以知识库清空时必须一起清掉，
#: 否则会留下指向已不存在的索引的过期元信息。
_INDEX_META_KEYS = (
    "embedding_model",
    "dimension",
    "chunk_size",
    "chunk_overlap",
    "parse_options",
    "indexed_at",
)


class DocumentRegistry:
    def __init__(self, path: Path = REGISTRY_FILE) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": _SCHEMA_VERSION,
            "embedding_model": None,
            "dimension": None,
            "documents": {},
        }
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if not self._path.is_file():
                return
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("注册表读取失败（将重建）：%s", exc)
                return

            if not isinstance(payload, dict):
                logger.error("注册表格式异常（将重建）")
                return

            payload.setdefault("documents", {})
            payload.setdefault("version", _SCHEMA_VERSION)
            self._data = payload
            logger.info("注册表载入：%d 个文档", len(self._data["documents"]))

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 落盘的文件永远标成当前版本：旧文件在下次保存时就地升级
            self._data["version"] = _SCHEMA_VERSION
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    def set_index_meta(
        self,
        embedding_model: str,
        dimension: int,
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        parse_options: dict[str, Any] | None = None,
    ) -> None:
        """登记「这份索引是用什么建出来的」，包含切分参数与解析开关，以便体检发现配置漂移。"""
        with self._lock:
            self._data["embedding_model"] = embedding_model
            self._data["dimension"] = dimension
            self._data["chunk_size"] = chunk_size
            self._data["chunk_overlap"] = chunk_overlap
            self._data["parse_options"] = dict(parse_options) if parse_options else None
            self._data["indexed_at"] = now_iso()

    def get_index_meta(self) -> dict[str, Any]:
        with self._lock:
            return {key: self._data.get(key) for key in _INDEX_META_KEYS}

    def clear_index_meta(self) -> None:
        """只清索引元信息，不动文档列表（删掉最后一个文档时用）。"""
        with self._lock:
            for key in _INDEX_META_KEYS:
                self._data[key] = None
            self.save()

    # ------------------------------------------------------------------
    def add(self, doc: dict[str, Any]) -> None:
        with self._lock:
            self._data["documents"][doc["doc_id"]] = doc
            self.save()

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data["documents"].get(doc_id)

    def remove(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._data["documents"].pop(doc_id, None)
            self.save()
            return doc

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            docs = list(self._data["documents"].values())
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs

    def find_by_hash(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            for doc in self._data["documents"].values():
                if doc.get("sha256") == sha256:
                    return doc
        return None

    def total_chunks(self) -> int:
        with self._lock:
            return sum(int(d.get("chunk_count", 0)) for d in self._data["documents"].values())

    def clear(self) -> None:
        """清空知识库：文档列表**和索引元信息**一起清，否则会留下指向已删索引的过期登记。"""
        with self._lock:
            self._data["documents"] = {}
            for key in _INDEX_META_KEYS:
                self._data[key] = None
            self.save()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "document_count": len(self._data["documents"]),
                "chunk_count": sum(
                    int(d.get("chunk_count", 0)) for d in self._data["documents"].values()
                ),
            }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


registry = DocumentRegistry()
