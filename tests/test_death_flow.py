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

from app.core import combat, loot  # noqa: E402
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


def test_ranged_legacy_full_durability_and_matched_ammo():
    """死亡继承远程武器：满耐久占主手（无撬棍）、按武器弹药类型发放子弹。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, legacy={
            "id": "silenced_smg", "earned_by": "death",
            "passes": 0, "durability": 3,
        })
        st = eng.state
        assert st["weapon"]["id"] == "silenced_smg"
        # 远程武器无耐久概念（config 无 durability 字段）→ 原值透传
        assert st["weapon"]["durability"] == 3
        assert not any(e["id"] == "crowbar" for e in st["inventory"]), "不应再发撬棍"
        start = int(cfg.balance["player"]["ammo_start"])
        assert loot.count(st, "ammo_t3") == start, "继承枪械应发对口子弹（重弹药）"
        assert loot.count(st, "ammo_t2") == 0, "默认制式弹药已取消"

    asyncio.run(run())


def test_melee_legacy_no_ammo_granted():
    """继承近战武器：满耐久占主手（撬棍被替换），不发任何弹药。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, legacy={
            "id": "fire_axe", "earned_by": "death",
            "passes": 0, "durability": 2,
        })
        st = eng.state
        assert st["weapon"]["id"] == "fire_axe"
        assert st["weapon"]["durability"] == int(cfg.item("fire_axe").get("durability"))
        for a in ("ammo_t1", "ammo_t2", "ammo_t3"):
            assert loot.count(st, a) == 0, f"无远程继承不发 {a}"

    asyncio.run(run())


def test_plain_run_no_ammo():
    """无继承开局：撬棍在手、零弹药（默认制式弹药不再发放）。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg)
        st = eng.state
        assert st["weapon"]["id"] == "crowbar"
        for a in ("ammo_t1", "ammo_t2", "ammo_t3"):
            assert loot.count(st, a) == 0

    asyncio.run(run())


def test_punch_fallback_while_holding_ranged():
    """持枪按攻击 = 挥拳（拳头兜底）：造成伤害、不磨损枪、不再拒绝。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, legacy={
            "id": "silenced_smg", "earned_by": "death",
            "passes": 0, "durability": None,
        })
        st = eng.state
        st["in_combat"] = True
        st["combat"] = {"enemies": [combat.make_enemy(cfg, "walker", 1)], "round": 0}
        st["combat"]["enemies"][0]["hp"] = 50
        gun_dur = st["weapon"]["durability"]
        eng._acquire = lambda *a, **k: None  # 屏蔽掉落

        await eng._act_attack({})

        assert st["combat"]["enemies"][0]["hp"] < 50, "挥拳应造成伤害（拳头 1-2）"
        assert st["weapon"]["durability"] == gun_dur, "挥拳不磨损枪"
        assert not any("不适合近身挥" in l for l in st["log"]), "不应再拒绝近战"

    asyncio.run(run())


def test_armor_legacy_full_with_instance_max():
    """继承护甲：满耐久 + 实例上限字段（修甲磨上限的基础）。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, legacy={
            "id": "bike_helmet", "earned_by": "death",
            "passes": 0, "durability": 4,
        })
        a = eng.state["armor"]
        assert a["id"] == "bike_helmet"
        assert a["durability"] == 25, "继承护甲应满耐久"
        assert a["max_durability"] == 25, "实例上限应初始化为配置值"

    asyncio.run(run())


def test_boss_cannot_be_fled_and_reentry_retriggers():
    """Boss 战不可逃跑（软锁修复）：逃跑被拒、战斗保持；重进房间可重新接敌。

    修复前：逃跑只拦精英不拦 Boss——逃掉暴君后撤离点没有再战入口，对局卡死。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["depth"] = 5
        st["boss_alive"] = True
        st["boss_seen"] = True
        st["room"]["boss"] = True
        st["combat"] = {"enemies": [combat.make_enemy(cfg, "tyrant_t03", 5)], "round": 0}
        st["in_combat"] = True
        st["stamina"] = 20

        await eng._act_flee({})
        assert st["in_combat"] is True, "Boss 战不可逃跑"
        assert any("没有退路" in l for l in st["log"]), eng._log if hasattr(eng, "_log") else ""

        # 重进 Boss 房 → 战斗重新触发（兜底已处于逃出状态的旧存档）
        stairs = next(
            i for i, r in enumerate(st["level_map"]["rooms"])
            if r.get("special_kind") == "stairs"
        )
        st["in_combat"] = False
        st["room"] = {"type": "stairs", "name": "撤离点", "idx": stairs}
        await eng._enter_room(stairs)
        assert st["in_combat"] is True, "重进 Boss 房应重新接敌"
        assert st["combat"]["enemies"] and st["combat"]["enemies"][0]["name"] == "暴君 T-03"

    asyncio.run(run())


def test_armor_eva_wired():
    """护甲 eva 接线（用户拍板）：重甲闪避惩罚、头盔加成进入玩家闪避。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["armor"] = None
        base = combat.player_profile(cfg, st)["eva"]

        st["armor"] = {"id": "military_vest", "durability": 55}
        heavy = combat.player_profile(cfg, st)["eva"]
        assert heavy == base - 12, f"军用防弹服应 −12 闪避，实际 {base}→{heavy}"

        st["armor"] = {"id": "bike_helmet", "durability": 25}
        light = combat.player_profile(cfg, st)["eva"]
        assert light == base + 2, f"头盔应 +2 闪避，实际 {base}→{light}"

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
    test_ranged_legacy_full_durability_and_matched_ammo()
    print("✓ 远程继承：满耐久+对口弹药+无撬棍")
    test_melee_legacy_no_ammo_granted()
    print("✓ 近战继承：满耐久+零弹药")
    test_plain_run_no_ammo()
    print("✓ 裸开局：撬棍+零弹药")
    test_punch_fallback_while_holding_ranged()
    print("✓ 持枪挥拳兜底")
    test_armor_legacy_full_with_instance_max()
    print("✓ 护甲继承：满耐久+实例上限")
    test_armor_eva_wired()
    print("✓ 护甲 eva 接线（重甲-12/头盔+2）")
    test_boss_cannot_be_fled_and_reentry_retriggers()
    print("✓ Boss 战不可逃跑 + 重进房间重新接敌")
    print("\n死亡流程回归测试全部通过")
