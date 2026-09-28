"""文档注册表：记录每个上传文档的元信息与其在 FAISS 中的 chunk id。

持久化到 ``data/index/registry.json``，采用「写临时文件 + 原子替换」，
避免进程被强杀时把注册表写坏。
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

_SCHEMA_VERSION = 1


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
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    def set_index_meta(self, embedding_model: str, dimension: int) -> None:
        with self._lock:
            self._data["embedding_model"] = embedding_model
            self._data["dimension"] = dimension

    def get_index_meta(self) -> dict[str, Any]:
        with self._lock:
            return {
                "embedding_model": self._data.get("embedding_model"),
                "dimension": self._data.get("dimension"),
            }

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
        with self._lock:
            self._data["documents"] = {}
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
