"""AI 结果二级缓存：内存 LRU → SQLite。

缓存键设计是整个 AI 层最关键的一处：
    key = "{content_type}:{template_version}:{model}:{sha256(prompt)[:16]}"

  * template_version 让改提示词自动失效
  * **变量必须粗粒度**——房间氛围只用 (房间模板, 层数)。
    绝不把玩家名、HP、背包塞进 prompt，否则命中率永远接近 0。
    个性化放到模板层做："你走进{ai_desc}"

房间氛围 5 层 × 约 16 个模板 = 80 个 key，几个玩家跑完就全部命中，稳态 >90%。
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any

from ..config import settings
from ..db.pool import llm_db

MEMORY_MAX = 1000
MEMORY_TTL = 3600


class LRU:
    def __init__(self, maxsize: int, ttl: int) -> None:
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: str) -> None:
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


_memory = LRU(MEMORY_MAX, MEMORY_TTL)


def make_key(content_type: str, template_version: str, model: str, prompt: str) -> str:
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    return f"{content_type}:{template_version}:{model}:{h}"


def get(key: str) -> str | None:
    hit = _memory.get(key)
    if hit is not None:
        _bump_hits(key)
        return hit

    found: str | None = None
    try:
        with llm_db() as conn:
            row = conn.execute(
                "SELECT output FROM llm_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row:
                with conn:
                    conn.execute(
                        "UPDATE llm_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                        (key,),
                    )
                found = row["output"]
                _memory.set(key, found)
    except Exception:
        # 缓存库出任何问题都不应该影响游戏，直接当未命中
        return None

    # 记命中数要等 llm_db() 的连接释放之后再做——它会自己再去拿那把锁。
    if found is not None:
        _bump_hits(key)
    return found


def set_value(key: str, output: str, model: str, prompt_hash: str) -> None:
    _memory.set(key, output)
    try:
        with llm_db() as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache "
                    "(cache_key, prompt_hash, output, model, created_at, hit_count) "
                    "VALUES (?,?,?,?,?,0)",
                    (key, prompt_hash, output, model, int(time.time())),
                )
    except Exception:
        pass


def _bump_hits(key: str) -> None:
    """记录当日缓存命中数，用于 /api/health 观测命中率。"""
    try:
        day = time.strftime("%Y-%m-%d")
        with llm_db() as conn:
            with conn:
                conn.execute(
                    "INSERT INTO llm_usage (day, cache_hits) VALUES (?,1) "
                    "ON CONFLICT(day) DO UPDATE SET cache_hits = cache_hits + 1",
                    (day,),
                )
    except Exception:
        pass


def stats() -> dict[str, Any]:
    try:
        day = time.strftime("%Y-%m-%d")
        with llm_db() as conn:
            row = conn.execute(
                "SELECT * FROM llm_usage WHERE day = ?", (day,)
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        return {
            "day": day,
            "calls": row["calls"] if row else 0,
            "cache_hits": row["cache_hits"] if row else 0,
            "tokens_in": row["tokens_in"] if row else 0,
            "tokens_out": row["tokens_out"] if row else 0,
            "cost_cents": row["cost_cents"] if row else 0,
            "rows": total,
        }
    except Exception:
        return {"day": None, "calls": 0, "cache_hits": 0, "rows": 0}


__all__ = ["make_key", "get", "set_value", "stats", "settings"]
