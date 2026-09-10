"""回归测试：死亡/撤离后的流程必须可继续。

曾经的真实故障：`_available_actions()` 里"本局已结束就返回空列表"的守卫
写在了待决策判断**之前**。死亡会同时把 status 置为 dead、
pending_decision 置为 legacy，于是函数直接返回空数组——
玩家看不到遗物选项、也没有任何按钮，整局永久卡死。

这类 bug 不报错、不崩溃，只是返回空列表，非常容易被漏掉，
所以这里把"结束后仍有可点的动作"作为断言钉住。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")   # 测试不该消耗 API 额度
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine, RunEnded  # noqa: E402


async def _new_run(cfg):
    """开一局并跳过天赋三选一，直接进地牢。"""
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


async def _kill(eng):
    """把玩家直接打死，触发死亡流程。"""
    eng.state["hp"] = 1
    try:
        await eng._die("测试用死因")
    except RunEnded:
        pass


def test_death_offers_legacy_choices():
    """死亡后必须给出可点的动作，而不是空列表。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        await _kill(eng)

        st = eng.state
        assert st["status"] == "dead", f"状态应为 dead，实际 {st['status']}"
        assert st["pending_decision"] == "legacy", "死亡后应进入遗物待决策"

        acts = eng._available_actions()
        assert acts, "死亡后 available_actions 为空——玩家会卡死，这正是回归的 bug"
        ids = {a["id"] for a in acts}
        assert "legacy" in ids, f"缺少 legacy 动作，实际: {acts}"
        # 必须始终保留"什么都不留"的退路，否则没有可继承物品时又会卡住
        assert any(a.get("index") == -1 for a in acts), "缺少「什么都不留」的兜底选项"
        return eng

    return asyncio.run(run())


def test_death_with_no_inheritable_items_still_proceeds():
    """身上没有可继承物品时，也要能走完流程（只能选"什么都不留"）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 清空一切可继承物：换成消耗品 + 清掉武器
        eng.state["inventory"] = [{"id": "bandage", "qty": 1, "durability": None}]
        eng.state["weapon"] = None
        eng.state["armor"] = None
        await _kill(eng)

        acts = eng._available_actions()
        legacy_acts = [a for a in acts if a["id"] == "legacy"]
        assert legacy_acts, "即使没有可继承物，也必须给「什么都不留」的选项"

        # 选"什么都不留"后，待决策应被清空、局已结束
        await eng.act("legacy", {"index": -1})
        assert eng.state["pending_decision"] is None, "选定后待决策应清空"
        assert eng.state["status"] == "dead"
        assert eng._available_actions() == [], "彻底结束后才应为空（此时前端给重开按钮）"

    asyncio.run(run())


def test_escape_also_offers_legacy_choices():
    """撤离成功走的是同一套待决策流程，不能只修死亡那条路。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        # 直接跳到撤离点
        st["depth"] = cfg.max_level
        await eng._enter_level(cfg.max_level)
        st["boss_alive"] = False
        st["boss_seen"] = True

        try:
            await eng._act_evac({})
        except RunEnded:
            pass

        assert st["status"] == "escaped", f"状态应为 escaped，实际 {st['status']}"
        acts = eng._available_actions()
        assert acts, "撤离后 available_actions 为空——玩家同样会卡死"
        assert any(a["id"] == "legacy" for a in acts), f"缺少 legacy 动作: {acts}"

    asyncio.run(run())


def test_talent_choice_not_blocked_by_status_guard():
    """天赋三选一发生在开局，用来确认重构后这条路径没被弄坏。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") != "talent":
            return  # 未抽到天赋（理论上不会），跳过
        acts = eng._available_actions()
        assert acts and all(a["id"] == "talent" for a in acts), f"天赋选项异常: {acts}"
        assert all("label" in a for a in acts)

    asyncio.run(run())


if __name__ == "__main__":
    test_death_offers_legacy_choices()
    print("✓ 死亡后给出遗物选项")
    test_death_with_no_inheritable_items_still_proceeds()
    print("✓ 无可继承物时仍可继续")
    test_escape_also_offers_legacy_choices()
    print("✓ 撤离后同样给出选项")
    test_talent_choice_not_blocked_by_status_guard()
    print("✓ 天赋三选一未受影响")
    print("\n死亡流程回归测试全部通过")
