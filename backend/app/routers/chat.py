"""问答与检索接口。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.logging import get_logger
from app.core.sse import stream_with_heartbeat
from app.schemas import ChatRequest, ChatResponse, SearchRequest, SearchResponse
from app.services.rag import LLMUnavailableError, rag_service
from app.services.vectorstore import IndexIncompatibleError, vector_store

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 反向代理下禁用缓冲，保证逐字输出
}


@router.post("/chat")
async def chat(payload: ChatRequest):
    """RAG 问答。

    ``stream=true`` 返回 ``text/event-stream``，事件序列为 ``meta`` → (``thinking``)*
    → (``token``)* → ``sources`` → ``done``；``stream=false`` 返回一次性 JSON。
    """
    if not vector_store.ready and not vector_store.exists_on_disk():
        raise HTTPException(
            status_code=409,
            detail="知识库为空，请先上传文档。",
        )

    if not payload.stream:
        try:
            result = await rag_service.answer(
                question=payload.question,
                history=payload.history,
                top_k=payload.top_k,
                score_threshold=payload.score_threshold,
                doc_ids=payload.doc_ids,
            )
        except IndexIncompatibleError as exc:
            # 索引与当前嵌入配置不一致 → 409 而不是 500：这不是服务端故障，
            # 而是有明确出路的状态（重建索引 / 改回原模型）。
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LLMUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ChatResponse(
            answer=result["answer"],
            thinking=result["thinking"],
            sources=result["sources"],
            model=result["model"],
            elapsed_ms=result["elapsed_ms"],
            mode=result.get("mode", "qa"),
            relaxed=bool(result.get("relaxed")),
            best_score=float(result.get("best_score") or 0.0),
        )

    async def factory():
        async for item in rag_service.stream(
            question=payload.question,
            history=payload.history,
            top_k=payload.top_k,
            score_threshold=payload.score_threshold,
            doc_ids=payload.doc_ids,
        ):
            yield item

    return StreamingResponse(
        stream_with_heartbeat(factory),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/search", response_model=SearchResponse)
async def search(payload: SearchRequest) -> SearchResponse:
    """纯检索接口：只返回相关片段，不调用 LLM（便于调参与排错）。

    返回体的 ``info`` 会带上本轮实际生效阈值、是否放宽、最佳分数，便于解释「为什么没召回」。
    """
    started = time.perf_counter()
    try:
        results, info = vector_store.search_detailed(
            query=payload.query,
            top_k=payload.top_k,
            score_threshold=payload.score_threshold,
        )
    except IndexIncompatibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SearchResponse(
        query=payload.query,
        results=results,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        info=info,
    )
