"""RAG 核心：检索 → 组装提示词 → 调用本地 Ollama 生成（支持流式）。

针对 **1.5B 小模型** 做了几处工程化处理：
1. 上下文严格裁剪（``max_context_chars``），避免超出 ``num_ctx`` 导致截断或幻觉。
2. 提示词强约束「只依据资料作答、无依据则明确说不知道」，抑制小模型编造。
3. 要求用 ``[n]`` 标注来源，前端可点击溯源到原文分块。
4. ``deepseek-r1`` 的推理链被单独解析成 ``thinking`` 事件，
   既能让用户看到推理过程，又不会污染正文和引用编号。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.config import settings
from app.core.logging import get_logger
from app.schemas import ChatMessage, SourceChunk
from app.services import ollama_client
from app.services.vectorstore import vector_store

logger = get_logger(__name__)

_llm_lock = threading.RLock()
_llm: Any | None = None

# ---------------------------------------------------------------------------
# 推理链标签
# ---------------------------------------------------------------------------
# 注意：这里刻意用 chr() 拼接，而不是写字面量。
# deepseek-r1 的推理链标签属于分词器特殊 token，直接以字面量写入源码
# 会在部分工具链中被改写成语义等价但无法匹配的字符，导致解析静默失效。
# ---------------------------------------------------------------------------
_LT = chr(60)  # <
_GT = chr(62)  # >
_SLASH = chr(47)  # /
_THINK_WORD = "think"

THINK_OPEN = _LT + _THINK_WORD + _GT
THINK_CLOSE = _LT + _SLASH + _THINK_WORD + _GT

_THINK_BLOCK_RE = re.compile(
    re.escape(THINK_OPEN) + r"(.*?)" + re.escape(THINK_CLOSE),
    flags=re.DOTALL | re.IGNORECASE,
)

SYSTEM_PROMPT = """你是一个严谨的文档问答助手。你必须**只依据**下面给出的【参考资料】来回答用户问题。

严格遵守以下规则：
1. 如果【参考资料】中没有足够信息，直接回答「根据已有文档无法回答该问题」，禁止编造或使用你自己的先验知识补充。
2. 回答中凡引用到具体事实、数据或结论，必须在句末用方括号标注来源编号，例如 [1] 或 [1][3]。
3. 只标注真正支撑该句子的编号，不要为了凑数而随意标注。
4. 使用与用户提问相同的语言作答（中文提问用中文回答）。
5. 内容较多时用列表组织：每条要点独占一行，写成「- **要点**：说明 [n]」的形式；
   同一个要点不要重复出现，也不要连续写两遍编号。
6. 不要复述【参考资料】的全文，要提炼并直接回答。

【参考资料】
{context}

再次强调：回答里每一句涉及文档内容的话，句末都必须带上来源编号（如 [1]）；整段话都没有编号的回答不合格。
"""

# 概览 / 总结类问题专用的提示词。
#
# 为什么不能和普通问答共用一套提示词：概览模式喂进去的是**全篇均匀取样的片段**，
# 而不是「最相关的几段」。这时如果还写「只回答用户问的那一点」，模型会抓住
# 某一段细节展开，总结就退化成片段复述；同时必须显式要求合并重复内容 ——
# 取样片段来自不同章节，会有交叉重复。
OVERVIEW_PROMPT = """你是一个严谨的文档总结助手。【文档片段】是同一篇（或同一批）文档按阅读顺序均匀抽取的内容，用来让你把握整体。

请严格按下面的格式作答，不要增减小节、不要写开场白：

整体：用 2~4 句话概括文档的主题、目的与主要结论。
要点：
- **要点名称**：一句话说明 [n]
- **要点名称**：一句话说明 [n]

规则：
1. 只能依据【文档片段】作答；片段里没有的内容不要写，不要补充你自己的先验知识。
2. 每条要点末尾**必须**紧跟来源编号，写成「[3]」这样；编号只能取自【文档片段】，不要自己编号。
   没有编号的要点不合格。
3. 要点一律用「- 」开头，不要用「1. 2. 3.」编号；不要重复同一条要点。
4. 片段取自文档不同位置，可能有少量重复，请合并同类内容。
5. 如果问题里还指定了具体方面（例如「总结多任务网络的优势」），请在整体概括之后**重点回答该方面**。
6. 使用与用户提问相同的语言作答。

【文档片段】
{context}

再次强调：每条要点的末尾都必须带来源编号（如 [3]）；一个编号都没有的回答不合格。
"""

NO_CONTEXT_REPLY = (
    "根据已有文档无法回答该问题。\n\n"
    "当前知识库中没有检索到与该问题足够相关的片段。你可以：\n"
    "- 换一种更贴近文档原文的提问方式；\n"
    "- 在左侧上传与该问题相关的文档后再试；\n"
    "- 或在设置中调低「相似度阈值」以提高召回（当前阈值内的片段都没能被采用）。"
)

# ---------------------------------------------------------------------------
# 提问意图：普通问答 vs 全文概览
# ---------------------------------------------------------------------------
# 「总结 / 概述 / 讲了什么」问的是**整篇**，与任何一个片段都不相似。
# 实测（data 里的 4 页中文期刊论文 + all-MiniLM-L6-v2）：
#     「多任务网络的优势是什么」 最佳相似度 0.367
#     「请总结这篇文档的主要内容」 0.433（但 top-6 里 3 条是页眉副本）
#     「总结一下」              0.273  ← 只比随机噪声（0.25）高一点
# 可见靠相似度阈值筛「总结」类问题，筛出来的往往是页眉和噪声。
# 这类问题应该改成按全篇均匀取样，让模型看到整份文档。
_OVERVIEW_KEYWORDS = (
    "总结", "概述", "概括", "综述", "摘要", "主要内容", "大致内容", "讲了什么",
    "说了什么", "说的是什么", "讲的是什么", "讲了哪些", "都讲了", "整体内容",
    "全文", "文章结构", "结构是什么", "脉络", "提炼", "要点有哪些", "介绍一下这",
    "介绍一下文档", "介绍一下文章", "这篇讲", "本文讲", "这篇文档", "这篇文章",
    "summarize", "summary", "overview", "main idea", "tl;dr", "what is this about",
)
# 概览问题通常很短；很长的提问更可能是针对某个具体细节
_OVERVIEW_MAX_CHARS = 60

_CITATION_RE = re.compile(r"\[(\d+)\]")


def detect_intent(question: str) -> str:
    """判断提问意图，返回 ``"overview"`` 或 ``"qa"``。"""
    text = (question or "").strip().lower()
    if not text or len(text) > _OVERVIEW_MAX_CHARS:
        return "qa"
    if any(keyword in text for keyword in _OVERVIEW_KEYWORDS):
        return "overview"
    return "qa"


class LLMUnavailableError(RuntimeError):
    """无法连接 Ollama，或模型未就绪。"""


def _describe_llm_error(exc: Exception) -> str:
    """把底层异常翻译成用户能直接照做的提示。

    Ollama 不可用时，不同环境抛出的异常差别很大：
      * 本机未启动      -> Connection refused / ConnectError
      * 端口被代理拦截  -> HTTP 502 / 503（本次实测遇到的就是这种）
      * 模型未拉取      -> 404 model not found
    因此这里按「连接类」和「状态码类」两类特征分别兜底。
    """
    text = str(exc)
    lowered = text.lower()

    connection_markers = (
        "connect", "refused", "connection", "timeout", "timed out", "unreachable", "reset",
    )
    status_markers = (
        "status code", "502", "503", "504", "bad gateway", "service unavailable",
    )

    if any(marker in lowered for marker in connection_markers + status_markers):
        return (
            f"无法连接 Ollama（{settings.ollama_base_url}）。\n"
            "请确认 Ollama 已启动：运行 scripts\\start.ps1（会自动拉起），"
            "或手动执行 ollama serve。\n"
            f"原始错误：{text}"
        )

    if "not found" in lowered or ("model" in lowered and "pull" in lowered):
        return (
            f"Ollama 中没有模型 {settings.llm_model}。\n"
            f"请先执行：ollama pull {settings.llm_model}\n"
            f"原始错误：{text}"
        )

    return f"调用本地模型失败：{text}"


# ---------------------------------------------------------------------------
# 推理链流式解析
# ---------------------------------------------------------------------------
class ThinkSplitter:
    """把推理链标签及其中的内容从流式正文里剥离出来。

    小模型逐 token 输出时标签会被切碎（例如先来 ``<th`` 再来 ``ink>``），
    因此需要在缓冲区里保留「可能是标签前缀」的尾部字符，
    等后续 token 补齐后再判断，否则标签会漏进正文。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def _hold_length(self, tag: str) -> int:
        """返回缓冲区末尾需要暂扣的字符数（它是 tag 的某个前缀）。"""
        limit = min(len(self._buf), len(tag) - 1)
        for size in range(limit, 0, -1):
            if self._buf.endswith(tag[:size]):
                return size
        return 0

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂入一段增量文本，返回 ``[(channel, text)]``，channel 为 answer/thinking。"""
        if not text:
            return []
        self._buf += text
        out: list[tuple[str, str]] = []

        while self._buf:
            tag = THINK_CLOSE if self._in_think else THINK_OPEN
            channel = "thinking" if self._in_think else "answer"
            position = self._buf.find(tag)

            if position == -1:
                hold = self._hold_length(tag)
                if hold:
                    piece = self._buf[: len(self._buf) - hold]
                    self._buf = self._buf[len(self._buf) - hold :]
                else:
                    piece = self._buf
                    self._buf = ""
                if piece:
                    out.append((channel, piece))
                break

            if position > 0:
                out.append((channel, self._buf[:position]))
            self._buf = self._buf[position + len(tag) :]
            self._in_think = not self._in_think

        return [(ch, txt) for ch, txt in out if txt]

    def flush(self) -> list[tuple[str, str]]:
        """流结束时吐出缓冲区剩余内容。"""
        if not self._buf:
            return []
        channel = "thinking" if self._in_think else "answer"
        piece, self._buf = self._buf, ""
        return [(channel, piece)]


def strip_thinking(text: str) -> tuple[str, str]:
    """非流式路径：把完整回答拆成 ``(正文, 推理链)``。"""
    thinking_parts = [m.group(1) for m in _THINK_BLOCK_RE.finditer(text)]
    answer = _THINK_BLOCK_RE.sub("", text)

    # 模型偶发只输出开标签就结束，此时把开标签之后的内容整体视为推理链
    if THINK_OPEN in answer:
        head, _, tail = answer.partition(THINK_OPEN)
        answer = head
        thinking_parts.append(tail)

    return answer.strip(), "\n".join(p.strip() for p in thinking_parts).strip()


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def _build_llm() -> Any:
    from langchain_ollama import ChatOllama

    # 原生 thinking 通道：开启后 Ollama 会把思维链单独放在 message.thinking，
    # langchain-ollama 再把它放进 additional_kwargs["reasoning_content"]。
    # 不开的话（think=false），deepseek-r1 这类模型的推理过程就被吞掉，
    # 界面上表现为「点了发送很久没反应」，然后直接蹦出答案。
    #
    # 但不能对所有模型都开：不支持思考的模型（如 llama3.2）收到 think=true
    # 可能直接报错，所以先用 /api/show 的 capabilities 确认。
    reasoning: bool | None = None
    if settings.expose_thinking and ollama_client.supports_thinking(settings.llm_model):
        reasoning = True

    logger.info(
        "初始化 ChatOllama：model=%s base_url=%s 原生thinking=%s",
        settings.llm_model,
        settings.ollama_base_url,
        bool(reasoning),
    )
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=settings.llm_temperature,
        num_ctx=settings.llm_num_ctx,
        num_predict=settings.llm_num_predict,
        # 1.5B 小模型很容易陷进「同一句话连写四遍」的退化循环，
        # 提高重复惩罚是代价最小的缓解手段（真机实测有效）。
        repeat_penalty=settings.llm_repeat_penalty,
        repeat_last_n=settings.llm_repeat_last_n,
        keep_alive="10m",
        reasoning=reasoning,
    )


def get_llm() -> Any:
    global _llm
    if _llm is None:
        with _llm_lock:
            if _llm is None:
                _llm = _build_llm()
    return _llm


def reset_llm() -> None:
    """切换模型 / Ollama 重启后调用，强制下次重新建立连接。"""
    global _llm
    with _llm_lock:
        _llm = None


# ---------------------------------------------------------------------------
# 提示词装配
# ---------------------------------------------------------------------------
def build_context(sources: list[SourceChunk]) -> str:
    """拼装参考资料，并按上下文预算截断。"""
    budget = settings.context_char_budget
    blocks: list[str] = []
    used = 0
    for source in sources:
        location = f"第 {source.page} 页" if source.page else f"片段 {source.chunk_index}"
        header = f"[{source.index}] 来源：{source.filename}（{location}）\n"
        body = source.content.strip()
        block = f"{header}{body}\n"
        if used + len(block) > budget and blocks:
            break
        blocks.append(block)
        used += len(block)
    return "\n".join(blocks)


# 引用编号的「贴身提醒」。
#
# 为什么要把要求再说一遍：规则写在系统提示词里、离生成位置很远，1.5B 小模型
# 经常直接忽略（真机实测：系统提示词里已明确要求 [n]，三次回答里 0 次标注）。
# 放在用户消息末尾（离生成最近、且属于「用户要求」）后命中率明显提高。
_CITATION_REMINDER = "\n\n（回答时请给涉及文档内容的话标注来源编号，例如 [1]；不要凭空编号。）"


def build_messages(
    question: str,
    sources: list[SourceChunk],
    history: list[ChatMessage] | None = None,
    mode: str = "qa",
) -> list[Any]:
    template = OVERVIEW_PROMPT if mode == "overview" else SYSTEM_PROMPT
    context = build_context(sources)
    messages: list[Any] = [SystemMessage(content=template.format(context=context))]

    for turn in (history or [])[-settings.max_history_turns * 2 :]:
        content = turn.content.strip()
        if not content:
            continue
        if turn.role == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))

    tail = _CITATION_REMINDER if sources else ""
    messages.append(HumanMessage(content=question + tail))
    return messages


def extract_cited_indexes(answer: str) -> list[int]:
    """从回答里提取 ``[n]`` 引用编号（去重、保序）。"""
    found: list[int] = []
    for raw in _CITATION_RE.findall(answer):
        value = int(raw)
        if value not in found:
            found.append(value)
    return found


# ---------------------------------------------------------------------------
# RAG 服务
# ---------------------------------------------------------------------------
class RAGService:
    def retrieve(
        self,
        question: str,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> list[SourceChunk]:
        sources, _info = self.retrieve_detailed(question, top_k, score_threshold, doc_ids)
        return sources

    def retrieve_detailed(
        self,
        question: str,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> tuple[list[SourceChunk], dict[str, Any]]:
        """按提问意图选择检索策略，并返回检索诊断信息。"""
        if detect_intent(question) == "overview":
            return vector_store.overview_chunks(question, doc_ids=doc_ids)
        return vector_store.search_detailed(
            query=question,
            top_k=top_k,
            score_threshold=score_threshold,
            doc_ids=doc_ids,
        )

    async def stream(
        self,
        question: str,
        history: list[ChatMessage] | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式生成，产出 ``meta`` / ``thinking`` / ``token`` / ``sources`` / ``done`` / ``error``。"""
        started = time.perf_counter()

        # 1) 检索
        try:
            sources, info = self.retrieve_detailed(question, top_k, score_threshold, doc_ids)
        except Exception as exc:  # noqa: BLE001
            logger.exception("检索失败")
            yield {"event": "error", "data": {"message": f"检索失败：{exc}", "stage": "retrieve"}}
            return

        yield {
            "event": "meta",
            "data": {
                "model": settings.llm_model,
                "mode": info.get("mode", "qa"),
                "top_k": top_k or settings.top_k,
                "score_threshold": (
                    settings.score_threshold if score_threshold is None else score_threshold
                ),
                "effective_threshold": info.get("effective_threshold", 0.0),
                "best_score": info.get("best_score", 0.0),
                "relaxed": bool(info.get("relaxed")),
                "candidates": info.get("candidates", 0),
                "chunks_total": info.get("chunks_total", 0),
                "source_count": len(sources),
            },
        }

        # 2) 无召回 → 直接返回兜底话术，不浪费一次推理
        if not sources:
            yield {"event": "sources", "data": {"sources": [], "cited": [], "thinking": None}}
            yield {"event": "token", "data": {"delta": NO_CONTEXT_REPLY}}
            yield {
                "event": "done",
                "data": {
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "cited": [],
                    "fallback": True,
                    "mode": info.get("mode", "qa"),
                },
            }
            return

        # 3) 生成
        mode = str(info.get("mode", "qa"))
        messages = build_messages(question, sources, history, mode=mode)
        splitter = ThinkSplitter()
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        usage: dict[str, Any] = {}

        try:
            async for chunk in get_llm().astream(messages):
                # 先收集统计信息，再做内容处理。
                # 最后一个 chunk 的 content 通常是空的，但只有它带着
                # eval_count / done_reason 等元数据；若放在下面的 `continue` 之后
                # 收集，这些数据会被全部丢掉（token 数永远显示不出来）。
                meta = getattr(chunk, "response_metadata", None)
                if meta:
                    usage.update(
                        {
                            k: v
                            for k, v in meta.items()
                            if k
                            in (
                                "eval_count",
                                "prompt_eval_count",
                                "total_duration",
                                "load_duration",
                                "prompt_eval_duration",
                                "eval_duration",
                                "done_reason",
                            )
                        }
                    )

                extra = getattr(chunk, "additional_kwargs", None) or {}

                # 思维链有两个来源，都要认：
                #   * message.thinking        —— 部分 Ollama 版本/客户端直接透传
                #   * additional_kwargs.reasoning_content
                #     —— langchain-ollama 1.x 把**原生 thinking 通道**放在这里
                #        （见 chat_models.py：`additional_kwargs["reasoning_content"]`）
                #     只认前者的话，deepseek-r1 这种默认就走原生通道的模型会
                #     全程没有任何 thinking 事件：用户点发送后界面长时间空白。
                direct_thinking = extra.get("thinking") or extra.get("reasoning_content")
                if direct_thinking:
                    thinking_parts.append(str(direct_thinking))
                    if settings.expose_thinking:
                        yield {"event": "thinking", "data": {"delta": str(direct_thinking)}}

                content = chunk.content
                if isinstance(content, list):  # 兼容结构化内容块
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if not content:
                    continue

                for channel, piece in splitter.feed(str(content)):
                    if channel == "thinking":
                        thinking_parts.append(piece)
                        if settings.expose_thinking:
                            yield {"event": "thinking", "data": {"delta": piece}}
                    else:
                        answer_parts.append(piece)
                        yield {"event": "token", "data": {"delta": piece}}

            for channel, piece in splitter.flush():
                if channel == "thinking":
                    thinking_parts.append(piece)
                    if settings.expose_thinking:
                        yield {"event": "thinking", "data": {"delta": piece}}
                else:
                    answer_parts.append(piece)
                    yield {"event": "token", "data": {"delta": piece}}

        except Exception as exc:  # noqa: BLE001
            logger.exception("生成失败")
            yield {
                "event": "error",
                "data": {"message": _describe_llm_error(exc), "stage": "generate"},
            }
            return

        answer = "".join(answer_parts).strip()
        cited = extract_cited_indexes(answer)

        yield {
            "event": "sources",
            "data": {
                "sources": [s.model_dump() for s in sources],
                "cited": cited,
                "thinking": "\n".join(thinking_parts).strip() if thinking_parts else None,
            },
        }
        yield {
            "event": "done",
            "data": {
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "cited": cited,
                "answer_chars": len(answer),
                "usage": usage,
                "mode": mode,
                "relaxed": bool(info.get("relaxed")),
            },
        }

    async def answer(
        self,
        question: str,
        history: list[ChatMessage] | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
        doc_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """非流式问答（供脚本 / 第三方 API 集成使用）。"""
        started = time.perf_counter()
        sources, info = self.retrieve_detailed(question, top_k, score_threshold, doc_ids)
        mode = str(info.get("mode", "qa"))
        if not sources:
            return {
                "answer": NO_CONTEXT_REPLY,
                "thinking": None,
                "sources": [],
                "model": settings.llm_model,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "mode": mode,
                "relaxed": False,
                "best_score": info.get("best_score", 0.0),
            }

        messages = build_messages(question, sources, history, mode=mode)
        try:
            raw = await get_llm().ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成失败（非流式）")
            raise LLMUnavailableError(_describe_llm_error(exc)) from exc

        content = raw.content if isinstance(raw.content, str) else str(raw.content)
        answer, thinking = strip_thinking(content)

        # 原生 thinking 通道的内容不在 content 里，单独补上
        extra = getattr(raw, "additional_kwargs", None) or {}
        native_thinking = extra.get("thinking") or extra.get("reasoning_content")
        if native_thinking:
            thinking = "\n".join(part for part in (thinking, str(native_thinking).strip()) if part)

        return {
            "answer": answer,
            "thinking": thinking or None,
            "sources": sources,
            "model": settings.llm_model,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "mode": mode,
            "relaxed": bool(info.get("relaxed")),
            "best_score": info.get("best_score", 0.0),
        }


rag_service = RAGService()
