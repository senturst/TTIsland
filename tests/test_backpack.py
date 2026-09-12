"""回归测试：背包容量系统（先拿后丢模型）。

设计要点（用户需求）：
  - 单格 = 一个物品条目；可堆叠物资（弹药/绷带/材料/纪念品）只占 1 格，qty 累加不占新格，
    天然限制"无限囤积"。
  - 初始容量 = balance.player.bag_slots；获取总是直接入包——允许暂时超过上限；
    超过容量 → bag_overflow 决策：必须丢到容量以内才能继续任何行动。
  - 背包天赋（packrat）背包 +5；背包本身是可装备槽（slots 叠加容量）；护甲口袋（pockets）也叠加。
  - 换上更小装备导致容量缩水 → 同样触发 bag_overflow。

所有断言从「当前配置 + 当前开局库存」推导，不写死数字，避免配置微调后测试集体误报。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import loot  # noqa: E402
import app.core.talents as T  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    # 进地牢第一步可能随机撞进战斗：背包测试默认在非战斗基线上断言
    # （战斗中超容量按设计延后到战斗结束，会打破"拾取即弹决策"的旧断言）
    eng.state["in_combat"] = False
    eng.state.setdefault("combat", {})
    eng.state["combat"]["enemies"] = []
    return eng


def _base(cfg):
    return int(cfg.balance["player"]["bag_slots"])


def _fill_to_cap(eng, cfg, item="small_pack"):
    """用非堆叠背包把库存填到当前容量上限（loot.grant 直写，绕过容量检查）。"""
    cap = eng._bag_cap()
    while len(eng.state["inventory"]) < cap:
        loot.grant(cfg, eng.state, item, 1, 10)


def test_default_capacity_is_10():
    """裸开局（无天赋）背包容量应为 balance.player.bag_slots。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        assert eng._bag_cap() == _base(cfg), "默认容量应等于 bag_slots"

    asyncio.run(run())


def test_stackable_does_not_consume_slots():
    """可堆叠物资只占 1 格：满包后再拾取已有弹药，qty 累加但不新增格子。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        _fill_to_cap(eng, cfg)
        cap = eng._bag_cap()
        assert len(eng.state["inventory"]) == cap, "应已填满容量"

        # 再拾取可堆叠绷带（开局自带 1 个）→ 不溢出
        # （默认弹药已取消，不能再用 ammo_t2 当"已在背包里的堆叠物"）
        ok = eng._acquire("bandage", 5)
        assert ok, "已有绷带入包不应溢出"
        assert len(eng.state["inventory"]) == cap, "可堆叠物不占新格"
        assert eng.state["pending_decision"] is None, "未超容量不应触发决策"

    asyncio.run(run())


def test_acquire_over_cap_still_grants_and_blocks():
    """背包满时拾取全新非堆叠物品：直接入包（可超上限），但触发 bag_overflow 阻断行动。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        _fill_to_cap(eng, cfg)
        cap = eng._bag_cap()

        ok = eng._acquire("crowbar", 1, durability=20)  # 全新非堆叠（背包类会自动装备，不适用本用例）
        assert ok, "先拿后丢：获取应总是成功"
        assert loot.count(eng.state, "crowbar") == 1, "应已直接入包"
        assert len(eng.state["inventory"]) == cap + 1, "允许暂时超上限"
        assert eng.state["pending_decision"] == "bag_overflow", "超上限应触发决策"
        assert eng.state.get("bag_pending") is None, "不应再有暂存待拾取物"

    asyncio.run(run())


def test_discard_back_to_cap_releases_decision():
    """bag_overflow 时丢弃物品到容量以内 → 决策解除；否则保持阻断。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)  # 背包类会自动装备，换非背包物品
        assert eng.state["pending_decision"] == "bag_overflow"

        await eng._act_discard({"choice": "drop", "item": "small_pack"})

        assert eng.state["pending_decision"] is None, "回到容量内应解除决策"
        assert loot.count(eng.state, "crowbar") == 1, "先拿的物品保留在包里"
        assert len(eng.state["inventory"]) == eng._bag_cap(), "丢 1 拿 1 仍是满格"

    asyncio.run(run())


def test_discard_skip_keeps_blocking():
    """bag_overflow 时选择 skip 不能蒙混过关——必须真丢东西。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)  # 背包类会自动装备，换非背包物品
        assert eng.state["pending_decision"] == "bag_overflow"

        await eng._act_discard({"choice": "skip"})

        assert eng.state["pending_decision"] == "bag_overflow", "skip 应继续阻断"
        assert loot.count(eng.state, "crowbar") == 1, "先拿的物品不被没收"

    asyncio.run(run())


def test_use_consumable_back_to_cap_releases_decision():
    """bag_overflow 时「用了」消耗品腾出格子 → 决策应解除。

    修复前：只有丢弃路径（_act_discard）会检查解除，use 路径用完物品
    回到容量以内后 pending_decision 卡死在 bag_overflow——界面关不掉，
    必须再丢一件才能继续。走 eng.act 完整派发覆盖真实入口。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（快速凝血会多发绷带）
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)
        assert eng.state["pending_decision"] == "bag_overflow"

        # 开局自带 1 个绷带：用堆叠物"用了"替代丢弃（整份消耗才腾格）
        from app.core import loot as _loot
        assert _loot.count(eng.state, "bandage") >= 1, "前提：开局有绷带"
        before = _loot.count(eng.state, "bandage")

        # 脱离战斗：_act_use 战斗中会触发敌人回合，怪物命中附带感染会干扰
        eng.state["in_combat"] = False
        eng.state["enemies"] = []
        await eng.act("use", {"item": "bandage"})

        assert _loot.count(eng.state, "bandage") == before - 1, "应真的用掉 1 个绷带"
        assert eng.state["pending_decision"] is None, \
            "用完回到容量内应解除决策（修复前卡死在 bag_overflow）"

    asyncio.run(run())


def test_combat_drop_defers_overflow_to_combat_end():
    """战斗中掉落超容量不弹整理：延后到战斗结束（_enemy_round 清场分支）。

    修复前：杀怪掉落 → _acquire → _check_bag_overflow 在战斗中直接
    pending=bag_overflow，锁住攻击/逃跑，逼玩家边打边整理。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []
        # 先进战斗再超容量：复现"战斗中掉落"的时序
        eng.state["in_combat"] = True
        eng.state["combat"]["enemies"] = [{"id": "walker", "hp": 5}]
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)
        assert len(eng.state["inventory"]) == eng._bag_cap() + 1, "先拿后丢：战利品已入包"
        assert eng.state["pending_decision"] is None, "战斗中不得弹整理决策"
        assert eng._check_bag_overflow() is False, "战斗中检查应延后"

        # 打完最后一只 → 清场分支应把欠下的整理弹出来
        for e in eng.state["combat"]["enemies"]:
            e["hp"] = 0
        await eng._enemy_round()
        assert eng.state["in_combat"] is False
        assert eng.state["pending_decision"] == "bag_overflow", \
            "战斗结束应补弹欠下的整理决策"

    asyncio.run(run())


def test_flee_surfaces_deferred_overflow():
    """战斗中欠下的整理，逃跑成功后也要弹出来（逃得掉战斗逃不掉整理）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []
        eng.state["in_combat"] = True
        eng.state["combat"]["enemies"] = [{"id": "walker", "hp": 5, "speed": 5}]
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)
        assert eng.state["pending_decision"] is None

        # 强制逃跑成功（try_flee 随机），走真实 _act_flee 出口
        from app.core import combat as _combat
        orig = _combat.try_flee
        _combat.try_flee = lambda *a, **k: True
        try:
            await eng._act_flee({})
        finally:
            _combat.try_flee = orig

        assert eng.state["in_combat"] is False
        assert eng.state["pending_decision"] == "bag_overflow", \
            "逃跑成功后应补弹欠下的整理决策"

    asyncio.run(run())


def test_overflow_check_never_clobbers_other_decisions():
    """超容量检查不得覆盖正在显示的其他决策（天赋三选一被覆盖即丢失）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)
        # 此时 pending=bag_overflow；模拟更严重的场景：别的决策正在显示
        eng.state["pending_decision"] = "talent"
        eng.state["talent_options"] = [
            {"id": "tough", "name": "韧皮", "desc": "", "weight": 1, "mods": {}}
        ]
        assert eng._check_bag_overflow() is False, "有决策在排队时不得抢槽"
        assert eng.state["pending_decision"] == "talent", "原决策应保留"

    asyncio.run(run())


def test_overflow_release_settles_queued_levelups():
    """溢出决策解除后应补结算排队的升级（否则滞留到下一场战斗）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []
        _fill_to_cap(eng, cfg)
        eng._acquire("crowbar", 1, durability=20)
        assert eng.state["pending_decision"] == "bag_overflow"
        eng.state["pending_levelups"] = 1  # 模拟战斗期间攒下、被溢出压住的升级

        await eng._act_discard({"choice": "drop", "item": "small_pack"})

        assert eng.state["pending_decision"] == "talent", \
            "丢完腾出空间后应立刻弹出排队的升级三选一"

    asyncio.run(run())


def test_packrat_talent_adds_capacity():
    """背包客天赋应让容量 +5。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        T.apply(cfg, eng.state, T.get_by_id(cfg, "packrat"))
        assert eng._bag_cap() == _base(cfg) + 5, "packrat 应 +5 格"

    asyncio.run(run())


def test_equip_backpack_adds_slots():
    """装备背包应把其 slots 叠加进容量。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        loot.grant(cfg, eng.state, "large_pack", 1, 20)
        before = eng._bag_cap()
        await eng._act_equip({"item": "large_pack"})
        assert eng.state["backpack"] is not None, "应已装备背包"
        assert eng._bag_cap() == before + int(cfg.item("large_pack")["slots"]), "slots 应叠加进容量"

    asyncio.run(run())


def test_armor_pockets_add_capacity():
    """护甲口袋应叠加进容量。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        loot.grant(cfg, eng.state, "tactical_vest", 1)
        await eng._act_equip({"item": "tactical_vest"})
        assert eng._bag_cap() == _base(cfg) + int(cfg.item("tactical_vest")["pockets"]), "口袋应叠加进容量"

    asyncio.run(run())


def test_unequip_smaller_armor_triggers_overflow():
    """换上口袋更少的护甲导致容量缩水，应触发 bag_overflow，且必须丢到装得下。"""
    cfg = get_config()
    vest_pockets = int(cfg.item("tactical_vest")["pockets"])

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        # 装备 tactical_vest（口袋 +vest_pockets）
        loot.grant(cfg, eng.state, "tactical_vest", 1)
        await eng._act_equip({"item": "tactical_vest"})
        assert eng._bag_cap() == _base(cfg) + vest_pockets
        # 填满当前容量
        _fill_to_cap(eng, cfg)
        assert len(eng.state["inventory"]) == eng._bag_cap()

        # 换上 riot_gear（口袋 0）→ 容量缩水，旧 vest 回背包 → 溢出
        loot.grant(cfg, eng.state, "riot_gear", 1)
        await eng._act_equip({"item": "riot_gear"})
        assert eng.state["pending_decision"] == "bag_overflow", "缩水应触发溢出"

        # 丢到装得下为止
        target = eng._bag_cap()  # riot_gear 后容量
        dropped = 0
        while eng.state["pending_decision"] == "bag_overflow":
            await eng._act_discard({"choice": "drop", "item": "small_pack"})
            dropped += 1
            assert dropped <= 20, "丢太多仍解除不了，逻辑有问题"
        assert eng.state["pending_decision"] is None, "丢到容量内应解除"
        assert len(eng.state["inventory"]) == target

    asyncio.run(run())


def test_backpack_serialized_to_response():
    """响应里必须带 bag_cap / bag_used / 已装备背包，前端才能渲染。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talents"] = []  # 隔离随机天赋（packrat 会 +5；P7 后读 talents 列表）
        loot.grant(cfg, eng.state, "hiking_pack", 1, 20)
        await eng._act_equip({"item": "hiking_pack"})
        resp = eng._response()
        st = resp["state"]
        assert "bag_cap" in st, "响应缺 bag_cap"
        assert "bag_used" in st, "响应缺 bag_used"
        assert st["bag_cap"] == _base(cfg) + int(cfg.item("hiking_pack")["slots"]), "bag_cap 应含背包 slots"
        assert st["backpack"]["id"] == "hiking_pack", "应带上已装备背包"
        assert st["bag_used"] == len(eng.state["inventory"])

    asyncio.run(run())


if __name__ == "__main__":
    test_default_capacity_is_10()
    print("ok 默认容量 = bag_slots")
    test_stackable_does_not_consume_slots()
    print("ok 可堆叠不占格")
    test_acquire_over_cap_still_grants_and_blocks()
    print("ok 超上限先拿后丢并阻断")
    test_discard_back_to_cap_releases_decision()
    print("ok 丢弃到容量内解除决策")
    test_discard_skip_keeps_blocking()
    print("ok skip 不能绕过阻断")
    test_packrat_talent_adds_capacity()
    print("ok 背包天赋 +5")
    test_equip_backpack_adds_slots()
    print("ok 装备背包叠加 slots")
    test_armor_pockets_add_capacity()
    print("ok 护甲口袋叠加容量")
    test_unequip_smaller_armor_triggers_overflow()
    print("ok 换小装备触发溢出")
    test_backpack_serialized_to_response()
    print("ok 容量序列化到响应")
    print("\n背包容量回归测试全部通过")
