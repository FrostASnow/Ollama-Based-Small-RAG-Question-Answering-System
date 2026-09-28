"""文档解析与切分。

支持格式：.txt .md .markdown .log .json .csv .pdf .docx
每种格式都产出带统一 metadata 的 LangChain ``Document``：
    doc_id / filename / stored_name / page / chunk_index / ext
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 切分器延迟导入（冷启动性能的关键）
# ---------------------------------------------------------------------------
# `langchain_text_splitters` 的包 __init__ 会**立刻**导入
# SentenceTransformersTokenTextSplitter，于是整个 sentence_transformers
# （含 transformers / torch 以及全部 loss、trainer 子模块）被一起拉起来。
# 实测：`import langchain_text_splitters` 冷启动约 9.5 秒，而
# `import langchain_core.documents` 只要 0.12 秒。
#
# 本模块位于 `app.main` 的导入链上（main -> routers.documents -> services.loader），
# 所以顶层导入它会把「端口开始监听」推迟到进程启动后 10 秒左右 ——
# 用户看到的是浏览器 ERR_CONNECTION_REFUSED，误以为启动失败。
#
# 改成首次真正需要切分时才导入：导入 app.main 的耗时从 10.14s 降到 0.57s，
# 而 sentence_transformers 本来就要在后台预热里加载，
# 此时再补这个导入只要 0.23 秒（见 preload()）。
# ---------------------------------------------------------------------------
_splitter_class: Any | None = None


def _get_splitter_class() -> Any:
    """返回 RecursiveCharacterTextSplitter 类（首次调用时才导入）。"""
    global _splitter_class
    if _splitter_class is None:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        _splitter_class = RecursiveCharacterTextSplitter
    return _splitter_class


def preload() -> None:
    """预热：把切分器依赖提前导入。

    在后台初始化任务里、嵌入模型加载完成之后调用。那时 sentence_transformers
    已经在内存里，补上这个导入几乎不花时间，用户第一次上传文档就不会卡顿。
    """
    class_ = _get_splitter_class()
    logger.debug("文本切分器已就绪：%s", class_.__name__)


# 中英文混合场景下的切分优先级：先按段落，再按句子，最后才按字符
_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "……",
    ". ",
    "! ",
    "? ",
    "; ",
    "，",
    ", ",
    " ",
    "",
]


class UnsupportedFileTypeError(ValueError):
    pass


class DocumentParseError(RuntimeError):
    pass


def build_splitter() -> Any:
    """构造一个中文友好的递归字符切分器（延迟导入见文件头注释）。"""
    return _get_splitter_class()(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=_SEPARATORS,
        length_function=len,
        is_separator_regex=False,
        add_start_index=True,
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _load_pdf(path: Path) -> list[tuple[str, dict[str, Any]]]:
    from langchain_community.document_loaders import PyPDFLoader

    loader = PyPDFLoader(str(path))
    pages = loader.load()
    return [(doc.page_content, {"page": doc.metadata.get("page")}) for doc in pages]


def _load_docx(path: Path) -> list[tuple[str, dict[str, Any]]]:
    from langchain_community.document_loaders import Docx2txtLoader

    loader = Docx2txtLoader(str(path))
    docs = loader.load()
    return [(doc.page_content, {}) for doc in docs]


def _load_text(path: Path, encoding: str = "utf-8") -> list[tuple[str, dict[str, Any]]]:
    try:
        content = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        content = path.read_text(encoding="utf-8", errors="replace")
    return [(content, {})]


def _load_csv(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """把 CSV 逐行转成 "列名: 值" 的可读文本，比原文更利于检索。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    lines: list[str] = []
    if reader.fieldnames:
        lines.append(" | ".join(reader.fieldnames))
    for row in reader:
        lines.append(" | ".join(f"{k}: {v}" for k, v in row.items() if v))
    return [("\n".join(lines), {})]


def _load_json(path: Path) -> list[tuple[str, dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pretty = text
    return [(pretty, {})]


_PARSERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".csv": _load_csv,
    ".json": _load_json,
    ".txt": _load_text,
    ".md": _load_text,
    ".markdown": _load_text,
    ".log": _load_text,
}


def parse_document(
    path: Path,
    doc_id: str,
    original_filename: str,
) -> tuple[list[Document], int]:
    """解析文件并切分为 chunk。

    返回 ``(chunks, 原始字符数)``。
    """
    ext = path.suffix.lower()
    parser = _PARSERS.get(ext)
    if parser is None:
        raise UnsupportedFileTypeError(
            f"不支持的文件类型：{ext}；支持 {', '.join(settings.allowed_extensions)}"
        )

    try:
        raw_parts = parser(path)
    except UnsupportedFileTypeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(f"解析 {original_filename} 失败：{exc}") from exc

    base_meta: dict[str, Any] = {
        "doc_id": doc_id,
        "filename": original_filename,
        "stored_name": path.name,
        "ext": ext,
    }

    documents: list[Document] = []
    total_chars = 0
    for content, extra in raw_parts:
        content = (content or "").strip()
        if not content:
            continue
        total_chars += len(content)
        documents.append(Document(page_content=content, metadata={**base_meta, **extra}))

    if not documents:
        raise DocumentParseError(f"{original_filename} 中没有可索引的文本内容")

    splitter = build_splitter()
    chunks = splitter.split_documents(documents)

    # 过滤空 chunk，并补上 chunk_index / 来源信息
    cleaned: list[Document] = []
    counter: dict[str, int] = {}
    for chunk in chunks:
        text = chunk.page_content.strip()
        if len(text) < 10:  # 过短的碎片对检索无贡献，反而引入噪声
            continue
        key = str(chunk.metadata.get("page", "-"))
        idx = counter.get(key, 0)
        counter[key] = idx + 1

        page = chunk.metadata.get("page")
        page_no = int(page) + 1 if isinstance(page, int) else None  # PyPDF 从 0 开始

        chunk.metadata.update(
            {
                **base_meta,
                "chunk_index": idx,
                "page": page_no,
                "char_count": len(text),
            }
        )
        chunk.page_content = text
        cleaned.append(chunk)

    if not cleaned:
        raise DocumentParseError(f"{original_filename} 文本过短，无法生成有效分块")

    logger.info(
        "解析完成：%s -> %d chunks / %d chars", original_filename, len(cleaned), total_chars
    )
    return cleaned, total_chars
