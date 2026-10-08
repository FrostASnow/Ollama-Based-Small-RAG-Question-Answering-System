"""Server-Sent Events 工具。

采用「生产者任务 + 队列 + 超时心跳」模式，而不是对异步生成器直接 ``wait_for``：
对 ``__anext__`` 施加超时会取消协程并关掉整个异步生成器，导致流式回答被截断。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

SENTINEL_DONE = object()


def format_sse(event: str, data: Any) -> str:
    """序列化为 SSE 报文。JSON 用 ``ensure_ascii=False`` 保留中文可读性。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def format_ping() -> str:
    """注释行心跳，防止反向代理/浏览器在长时间无输出时断开连接。"""
    return ": ping\n\n"


async def stream_with_heartbeat(
    factory: Callable[[], AsyncIterator[dict[str, Any]]],
    interval: float = 10.0,
) -> AsyncIterator[str]:
    """把 ``{event, data}`` 事件流转成 SSE 文本流，并周期性发送心跳。

    ``factory`` 必须每次调用都新建生成器，避免复用已被消费的生成器。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for item in factory():
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put({"event": "error", "data": {"message": str(exc), "stage": "stream"}})
        finally:
            await queue.put(SENTINEL_DONE)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield format_ping()
                continue

            if item is SENTINEL_DONE:
                break

            yield format_sse(item["event"], item["data"])
    finally:
        if not task.done():
            task.cancel()
        # 消化取消，避免 "Task exception was never retrieved"
        await asyncio.gather(task, return_exceptions=True)
