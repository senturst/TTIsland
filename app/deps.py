"""依赖注入：身份识别、限流、DB 调用包装。"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .config import settings

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# client_id -> action -> deque[timestamp]
_HITS: dict[str, dict[str, deque[float]]] = defaultdict(lambda: defaultdict(deque))


class RateLimitExceeded(HTTPException):
    def __init__(self, detail: str = "操作太频繁了，慢一点。") -> None:
        super().__init__(status_code=429, detail=detail)


def check_rate(client_id: str, action: str, limit: int, window: float = 60.0) -> None:
    now = time.monotonic()
    q = _HITS[client_id][action]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise RateLimitExceeded()
    q.append(now)


def client_id_from(x_client_id: str | None = Header(default=None, alias="X-Client-Id")) -> str:
    if not x_client_id or not CLIENT_ID_RE.match(x_client_id):
        raise HTTPException(status_code=400, detail="缺少或非法的 X-Client-Id 头")
    return x_client_id


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def db_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """同步 sqlite 调用包一层线程池，避免在 async 端点里阻塞事件循环。"""
    return await run_in_threadpool(lambda: fn(*args, **kwargs))


def rate_action(client_id: str) -> None:
    check_rate(client_id, "action", settings.rate_action_per_min)


def rate_start(client_id: str) -> None:
    check_rate(client_id, "start", settings.rate_start_per_min)


__all__ = [
    "client_id_from",
    "db_call",
    "rate_action",
    "rate_start",
    "RateLimitExceeded",
    "check_rate",
]
