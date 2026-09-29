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
import math
import re
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
# 页眉 / 页脚（重复行）过滤
# ---------------------------------------------------------------------------
# 期刊论文、报告、试卷类 PDF 每一页都会重复同一段页眉：刊名、期号、栏目名。
# 这些行在向量空间里是一份「关键词拼盘」——它同时含有刊名、栏目名、标题词，
# 于是对**任何**提问都能拿到不低的相似度。实测本仓库 data 里的 4 页期刊论文：
#
#   Q「请总结这篇文档的主要内容」top-6 分数 = .433 .430 .374 .352 .333 .298
#   其中 3 条都是同一段页眉在不同页上的副本，正文一条都没进来。
#
# 槽位被样板文本占满，模型自然「总结不出内容」。所以在切分之前，
# 先把「出现在足够多页上的短行」整行删掉。
_BOILERPLATE_MIN_PAGES = 3      # 页数太少时样本不足，容易误删正文
_BOILERPLATE_PAGE_RATIO = 0.6   # 出现在 ≥60% 页面上才算页眉 / 页脚
_BOILERPLATE_MAX_CHARS = 100    # 只把短行当样板；长段落重复更可能是正常引用


def _normalize_line(line: str) -> str:
    """归一化用于比对：**去掉**所有空白字符。

    不能只折叠空白：同一段页眉在抽取结果里常常时有时无地多一个空格
    （实测本仓库那篇期刊论文：奇数页是 `Communication 人工智能与识别技术`，
    偶数页是 `Communication人工智能与识别技术`），只折叠的话两边仍然不相等，
    重复行检测会整体失效。
    """
    return re.sub(r"\s+", "", line)


def strip_repeated_lines(
    pages: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """删除在多页上重复出现的短行（页眉 / 页脚 / 刊头）。

    返回 ``(处理后的页, 删除的行数)``。页数少于 3 时直接原样返回。
    """
    if len(pages) < _BOILERPLATE_MIN_PAGES:
        return pages, 0

    page_count = len(pages)
    seen_on: dict[str, set[int]] = {}
    for index, (content, _meta) in enumerate(pages):
        for line in {_normalize_line(item) for item in (content or "").splitlines()}:
            if not line or len(line) > _BOILERPLATE_MAX_CHARS:
                continue
            seen_on.setdefault(line, set()).add(index)

    need = max(_BOILERPLATE_MIN_PAGES, math.ceil(page_count * _BOILERPLATE_PAGE_RATIO))
    boilerplate = {line for line, hits in seen_on.items() if len(hits) >= need}
    if not boilerplate:
        return pages, 0

    cleaned: list[tuple[str, dict[str, Any]]] = []
    removed = 0
    for content, meta in pages:
        kept: list[str] = []
        for line in (content or "").splitlines():
            if _normalize_line(line) in boilerplate:
                removed += 1
                continue
            kept.append(line)
        cleaned.append(("\n".join(kept), meta))
    return cleaned, removed


# ---------------------------------------------------------------------------
# 版面还原：把 PDF 的硬换行补回段落边界
# ---------------------------------------------------------------------------
# PyPDF 抽出来的正文是「一行一行」的硬换行，段与段之间**没有空行**。
# 递归切分器只能在 800 字处硬切，于是中文正文经常和英文摘要、上一节的开头
# 粘进同一个块。实测那篇期刊论文：包含「1.1 多任务网络架构」的正文，
# 因为前半段是英文摘要，对中文提问「多任务网络的优势是什么」的相似度
# 从 0.367 掉到 0.147 —— 检索和总结同时变差。
#
# 这里用两条版面规则补回段落边界：
#   * 小节标题（`0 引言` / `1.1 多任务网络架构` / `2.2.3 基于多任务网络…`）另起一段；
#   * 中英文切换（中文正文接英文摘要、或反之）另起一段。
# 切分器优先按空行切，于是分块自然对齐到小节，而不是对齐到第 800 个字。
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_SECTION_HEADING_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}[\s、.]*\S")


def _script_counts(text: str) -> tuple[int, int]:
    return len(_CJK_RE.findall(text)), len(_LATIN_RE.findall(text))


def _is_section_heading(line: str) -> bool:
    """`1.1 多任务网络架构` 这类小节标题（不看内容、只看版面特征）。"""
    if not line or len(line) > 40:
        return False
    if line.endswith(("。", "，", "；", "：", ",", ";", ".")):
        return False
    return bool(_SECTION_HEADING_RE.match(line))


def _switches_script(previous: str, current: str) -> bool:
    """上一行是纯英文、这一行是中文（或反向）→ 版面换块。"""
    prev_cjk, prev_latin = _script_counts(previous)
    cur_cjk, cur_latin = _script_counts(current)
    latin_then_cjk = prev_latin >= 6 and prev_cjk == 0 and cur_cjk >= 4 and cur_latin <= 2
    cjk_then_latin = prev_cjk >= 6 and prev_latin == 0 and cur_latin >= 6 and cur_cjk == 0
    return latin_then_cjk or cjk_then_latin


def insert_paragraph_breaks(text: str) -> str:
    """给硬换行的 PDF 正文补回段落边界（见上文说明）。"""
    lines = (text or "").splitlines()
    out: list[str] = []
    last_content = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            last_content = ""
            continue
        if last_content and (
            _is_section_heading(stripped) or _switches_script(last_content, stripped)
        ):
            out.append("")
        out.append(line)
        last_content = stripped
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 参考文献列表
# ---------------------------------------------------------------------------
# 参考文献是「关键词最密集、语义最贫乏」的文本：条目标题里塞满了主题词，
# 于是对**任何**提问的相似度都不低。实测那篇期刊论文：
#   Q「请总结这篇文档的主要内容」→ 参考文献块 0.420（全场第一）
#   Q「多任务网络的优势是什么」  → 参考文献块 0.309（挤掉正文槽位）
# 而它提供不了任何可用信息。识别到「参考文献 / References」这种独占一行的
# 标题、且后面确实是编号条目时，直接截掉。
_REFERENCE_HEADINGS = {
    "参考文献",
    "参考文献:",
    "参考文献：",
    "references",
    "reference",
    "bibliography",
    "参考资料",
}
_REFERENCE_ENTRY_RE = re.compile(r"^\s*[\[\(]\d{1,3}[\]\)]")


def _is_reference_heading(line: str) -> bool:
    normalized = _normalize_line(line).lower().rstrip(":：")
    return normalized in {item.rstrip(":：") for item in _REFERENCE_HEADINGS}


def strip_reference_sections(
    pages: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """截掉「参考文献」及其后内容，返回 ``(处理后的页, 截掉的行数)``。

    只有在标题后面确实跟着至少两条编号条目时才动手，避免把正文里
    顺口提一句「参考文献」的段落也误删。
    """
    cleaned: list[tuple[str, dict[str, Any]]] = []
    removed = 0
    for content, meta in pages:
        lines = (content or "").splitlines()
        cut = None
        for index, line in enumerate(lines):
            if not _is_reference_heading(line):
                continue
            tail = lines[index + 1 :]
            entries = sum(1 for item in tail if _REFERENCE_ENTRY_RE.match(item))
            if entries >= 2:
                cut = index
                break
        if cut is None:
            cleaned.append((content, meta))
            continue
        removed += len(lines) - cut
        cleaned.append(("\n".join(lines[:cut]), meta))
    return cleaned, removed


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _load_pdf(path: Path) -> list[tuple[str, dict[str, Any]]]:
    from langchain_community.document_loaders import PyPDFLoader

    loader = PyPDFLoader(str(path))
    pages = loader.load()
    result: list[tuple[str, dict[str, Any]]] = []
    for doc in pages:
        content = doc.page_content or ""
        if settings.restore_pdf_paragraphs:
            content = insert_paragraph_breaks(content)
        result.append((content, {"page": doc.metadata.get("page")}))
    return result


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

    dropped_lines = 0
    if settings.strip_boilerplate:
        raw_parts, dropped_lines = strip_repeated_lines(raw_parts)
        if dropped_lines:
            logger.info(
                "过滤重复行（页眉/页脚）：%s 共 %d 行", original_filename, dropped_lines
            )

    dropped_refs = 0
    if settings.strip_references:
        raw_parts, dropped_refs = strip_reference_sections(raw_parts)
        if dropped_refs:
            logger.info("截掉参考文献列表：%s 共 %d 行", original_filename, dropped_refs)

    # 抽取质量体检：多页 PDF 平均每页字符数过低，通常意味着扫描件/图片版，
    # 此时检索不出内容不是检索的问题，而是压根没抽到文本，要明确告诉用户。
    if len(raw_parts) >= 3:
        extracted = sum(len(content or "") for content, _ in raw_parts)
        per_page = extracted / len(raw_parts)
        if per_page < 120:
            logger.warning(
                "%s 平均每页仅抽取到 %.0f 字符，可能是扫描件或图片版 PDF，"
                "正文无法检索；建议改用文字版 PDF",
                original_filename,
                per_page,
            )

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
