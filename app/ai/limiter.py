"""限流与成本控制。

三道闸，任何一道被触发都直接降级到模板文本，绝不阻塞玩家操作：
    1. 日预算（分）与日调用次数硬上限
    2. 令牌桶：平滑突发，防止瞬间打爆 API
    3. 并发信号量
外加断路器：连续失败 N 次后冷静一段时间，不再无谓地消耗超时等待。
"""
from __future__ import annotations

import asyncio
import time

from ..config import settings
from ..db.pool import llm_db


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def today_usage() -> dict:
    try:
        with llm_db() as conn:
            row = conn.execute(
                "SELECT * FROM llm_usage WHERE day = ?", (_today(),)
            ).fetchone()
            return dict(row) if row else {
                "day": _today(), "calls": 0, "tokens_in": 0,
                "tokens_out": 0, "cost_cents": 0, "cache_hits": 0,
            }
    except Exception:
        return {"day": _today(), "calls": 0, "tokens_in": 0,
                "tokens_out": 0, "cost_cents": 0, "cache_hits": 0}


def budget_exceeded() -> bool:
    u = today_usage()
    return (
        u["cost_cents"] >= settings.llm_daily_budget_cents
        or u["calls"] >= settings.llm_max_calls_per_day
    )


def record_usage(tokens_in: int, tokens_out: int, cost_cents: int) -> None:
    try:
        with llm_db() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO llm_usage (day, tokens_in, tokens_out, cost_cents, calls)
                    VALUES (?,?,?,?,1)
                    ON CONFLICT(day) DO UPDATE SET
                        tokens_in = tokens_in + excluded.tokens_in,
                        tokens_out = tokens_out + excluded.tokens_out,
                        cost_cents = cost_cents + excluded.cost_cents,
                        calls = calls + 1
                    """,
                    (_today(), tokens_in, tokens_out, cost_cents),
                )
    except Exception:
        pass


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """取一个令牌。取不到立即返回 False（不阻塞玩家）。"""
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(
                float(self.capacity), self.tokens + (now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens < 1.0:
                return False
            self.tokens -= 1.0
            return True


class CircuitBreaker:
    """连续失败达阈值后熔断一段时间，期间不发请求。"""

    def __init__(self, threshold: int, cooldown: int) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self.opened_at and time.monotonic() - self.opened_at < self.cooldown:
            return True
        if self.opened_at:
            self.opened_at = 0.0  # 冷静期结束，半开
            self.failures = 0
        return False

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0


bucket = TokenBucket(settings.llm_rate_per_sec, settings.llm_rate_burst)
breaker = CircuitBreaker(settings.llm_circuit_threshold, settings.llm_circuit_cooldown)
semaphore = asyncio.Semaphore(settings.llm_max_concurrency)


def status() -> dict:
    u = today_usage()
    return {
        "ai_active": settings.ai_active,
        "circuit_open": breaker.is_open,
        "failures": breaker.failures,
        "budget_cents": settings.llm_daily_budget_cents,
        "used_cents": u["cost_cents"],
        "calls_today": u["calls"],
        "max_calls": settings.llm_max_calls_per_day,
        "cache_hits_today": u["cache_hits"],
        "budget_exceeded": budget_exceeded(),
    }


__all__ = [
    "bucket", "breaker", "semaphore", "budget_exceeded",
    "record_usage", "status", "today_usage",
]
