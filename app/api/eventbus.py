"""进程内事件总线：把"重大事件"实时推给所有在线客户端。

设计要点：
  * 世界播报是**单向广播**（死亡/撤离/破纪录），不做聊天。
  * 用 asyncio.Queue 做订阅者队列，publish 通过 call_soon_threadsafe 投递，
    这样在 run.py 的线程池上下文里也能安全触发（不依赖调用方是否在事件循环里）。
  * 单进程 uvicorn 下足够；将来若要多 worker，换成 Redis 发布订阅即可，接口不变。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()

    def subscribe(self) -> asyncio.Queue:
        """在事件循环里调用，返回一个队列并登记为订阅者。"""
        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for item in list(self._subscribers):
            if item[0] is q:
                self._subscribers.discard(item)
                break

    def publish(self, event: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        data = dict(event)
        data.setdefault("ts", int(time.time()))
        for q, loop in list(self._subscribers):
            loop.call_soon_threadsafe(q.put_nowait, data)


# 单例：整个进程共享一个总线
bus = EventBus()
