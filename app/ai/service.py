"""AI 风味文本调度。

核心原则：**AI 永远不阻塞玩家操作**。

  * 装饰性文本（房间氛围、物品描述）——缓存未命中时先返回模板文本立即渲染，
    后台生成完成后随下一次响应作为 patch 下发，前端原地替换。
    AI 延迟 1-3s 玩家完全无感。
  * 强情感节点（墓志铭、死亡旁白）——允许同步等待 3s，超时即降级。
    这是玩家会停下来读的文字，值得等一下。

四级降级：
    缓存命中 → 模板兜底 → 熔断冷静 → 预算耗尽（响应头标记，前端显示"信号衰减"）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from ..config import settings
from . import cache, client, limiter
from .fallback import pick_deterministic
from .prompts import get_template

log = logging.getLogger("ai.flavor")

# 已生成但未下发的补丁：target_id -> (text, ts)
_PENDING: dict[str, tuple[str, float]] = {}
_INFLIGHT: set[str] = set()
_PATCH_TTL = 300


def _fallback(content_type: str, seed_key: str) -> str:
    return pick_deterministic(content_type, seed_key)


class FlavorService:
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            **limiter.status(),
            "cache": cache.stats(),
            "inflight": len(_INFLIGHT),
            "pending": len(_PENDING),
        }

    # ------------------------------------------------------------------
    async def render(
        self,
        content_type: str,
        variables: dict,
        *,
        target_id: str | None = None,
        state: dict | None = None,
        wait: bool = False,
    ) -> str:
        """取一段风味文本。

        wait=False 时绝不阻塞：缓存未命中就返回模板并后台生成。
        """
        seed_key = target_id or f"{content_type}:" + ":".join(map(str, variables.values()))

        if not settings.ai_active:
            return _fallback(content_type, seed_key)

        try:
            tpl = get_template(content_type)
        except FileNotFoundError:
            return _fallback(content_type, seed_key)

        system, user = tpl.render(variables)
        key = cache.make_key(content_type, tpl.version, settings.llm_model, system + user)

        # 1 级：缓存
        cached = await asyncio.to_thread(cache.get, key)
        if cached:
            return cached

        # 2 级：预算 / 熔断
        if limiter.breaker.is_open or limiter.budget_exceeded():
            return _fallback(content_type, seed_key)

        if wait:
            return await self._generate(key, content_type, system, user, seed_key,
                                        timeout=settings.llm_sync_timeout)

        # 装饰性文本：后台生成，先返回模板
        if target_id and state is not None and key not in _INFLIGHT:
            _INFLIGHT.add(key)
            state.setdefault("flavor_pending", {})[target_id] = {
                "turn": state.get("turn", 0),
                "type": content_type,
            }
            asyncio.create_task(
                self._background(key, content_type, system, user, target_id)
            )
        return _fallback(content_type, seed_key)

    # ------------------------------------------------------------------
    async def _generate(
        self,
        key: str,
        content_type: str,
        system: str,
        user: str,
        seed_key: str,
        *,
        timeout: float,
    ) -> str:
        if not await limiter.bucket.acquire():
            return _fallback(content_type, seed_key)

        max_tokens = client.MAX_CHARS.get(content_type, 40) * 2
        text, tin, tout = "", 0, 0
        # 推理模型（deepseek-flash）的 thinking 计入 max_tokens，偶尔把预算吃光导致
        # content 为空。空输出不记熔断失败，换更大预算重试一次（成本极低：正文才 30 字）。
        for attempt, budget in enumerate((max_tokens, 240)):
            try:
                async with limiter.semaphore:
                    text, tin, tout = await asyncio.wait_for(
                        client.chat(system, user, max_tokens=budget, timeout=timeout),
                        timeout=timeout + 1.0,
                    )
            except Exception as exc:  # noqa: BLE001
                log.info("AI 调用失败(%s): %s", content_type, type(exc).__name__)
                limiter.breaker.record_failure()
                return _fallback(content_type, seed_key)
            if text.strip():
                break
            if attempt == 0:
                log.info("AI 返回空 content（推理吃满 %d 预算），加大预算重试", max_tokens)

        ok, cleaned = client.validate(text, content_type)
        if not ok:
            log.info("AI 输出不合法(%s): %r", content_type, text[:60])
            limiter.breaker.record_failure()
            return _fallback(content_type, seed_key)

        limiter.breaker.record_success()
        await asyncio.to_thread(
            limiter.record_usage,
            tin,
            tout,
            client.estimate_cost_cents(tin, tout),
        )
        await asyncio.to_thread(
            cache.set_value, key, cleaned, settings.llm_model,
            hashlib.sha256((system + user).encode()).hexdigest()[:16],
        )
        return cleaned

    async def _background(
        self, key: str, content_type: str, system: str, user: str, target_id: str
    ) -> None:
        try:
            text = await self._generate(
                key, content_type, system, user, target_id,
                timeout=settings.llm_timeout,
            )
            _PENDING[target_id] = (text, time.time())
        except Exception:  # noqa: BLE001
            pass
        finally:
            _INFLIGHT.discard(key)

    # ------------------------------------------------------------------
    @staticmethod
    def drain(state: dict) -> list[dict]:
        """取出已完成的补丁，并从 state 的待办里移除。

        随每次响应下发。玩家每几秒就有一次操作，补丁几乎立刻到达。
        """
        pending = state.get("flavor_pending") or {}
        if not pending or not _PENDING:
            return []

        now = time.time()
        # 顺带清理过期补丁
        for tid, (_, ts) in list(_PENDING.items()):
            if now - ts > _PATCH_TTL:
                _PENDING.pop(tid, None)
                pending.pop(tid, None)

        out: list[dict] = []
        for tid in list(pending):
            item = _PENDING.pop(tid, None)
            if item:
                out.append({"target": tid, "text": item[0]})
                pending.pop(tid, None)

        # 超过 20 回合仍未下发的（玩家可能已离开该场景）直接丢弃
        turn = state.get("turn", 0)
        for tid, meta in list(pending.items()):
            if turn - int(meta.get("turn", 0)) > 20:
                pending.pop(tid, None)

        state["flavor_pending"] = pending
        return out


flavor = FlavorService()

__all__ = ["flavor", "FlavorService"]
