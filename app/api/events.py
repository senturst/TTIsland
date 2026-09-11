"""世界事件 SSE 流。

只做一件事：把 EventBus 里的"重大事件"以 SSE 形式推给浏览器。
浏览器用 EventSource 订阅，收到 broadcast 事件后弹一条世界播报（不出聊天框）。

为什么用 SSE 而不是轮询：播报是低频但即时的（有人刚死，你立刻看到），
SSE 的连接开销在一次握手后几乎为零，比每 3 秒打一次库优雅得多。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .eventbus import bus

router = APIRouter(prefix="/api/events", tags=["events"])


async def _event_stream() -> None:
    q = bus.subscribe()
    try:
        # 告诉客户端断线后 5 秒重连
        yield "retry: 5000\n\n"
        yield "event: hello\ndata: {}\n\n"
        while True:
            event = await q.get()
            payload = json.dumps(event, ensure_ascii=False)
            yield f"event: broadcast\ndata: {payload}\n\n"
    except asyncio.CancelledError:
        bus.unsubscribe(q)
        raise
    finally:
        bus.unsubscribe(q)


@router.get("/stream")
async def stream() -> StreamingResponse:
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则事件会被攒批
        },
    )
