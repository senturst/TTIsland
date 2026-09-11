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
        eng.state["talent"] = None  # 隔离随机天赋（packrat 会 +5）
        assert eng._bag_cap() == _base(cfg), "默认容量应等于 bag_slots"

    asyncio.run(run())


def test_stackable_does_not_consume_slots():
    """可堆叠物资只占 1 格：满包后再拾取已有弹药，qty 累加但不新增格子。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        cap = eng._bag_cap()
        assert len(eng.state["inventory"]) == cap, "应已填满容量"

        # 再拾取可堆叠弹药（ammo_pistol 已在背包里）→ 不溢出
        ok = eng._acquire("ammo_pistol", 5)
        assert ok, "已有弹药入包不应溢出"
        assert len(eng.state["inventory"]) == cap, "可堆叠物不占新格"
        assert eng.state["pending_decision"] is None, "未超容量不应触发决策"

    asyncio.run(run())


def test_acquire_over_cap_still_grants_and_blocks():
    """背包满时拾取全新非堆叠物品：直接入包（可超上限），但触发 bag_overflow 阻断行动。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        cap = eng._bag_cap()

        ok = eng._acquire("hiking_pack", 1)  # 全新非堆叠
        assert ok, "先拿后丢：获取应总是成功"
        assert loot.count(eng.state, "hiking_pack") == 1, "应已直接入包"
        assert len(eng.state["inventory"]) == cap + 1, "允许暂时超上限"
        assert eng.state["pending_decision"] == "bag_overflow", "超上限应触发决策"
        assert eng.state.get("bag_pending") is None, "不应再有暂存待拾取物"

    asyncio.run(run())


def test_discard_back_to_cap_releases_decision():
    """bag_overflow 时丢弃物品到容量以内 → 决策解除；否则保持阻断。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        eng._acquire("hiking_pack", 1)
        assert eng.state["pending_decision"] == "bag_overflow"

        await eng._act_discard({"choice": "drop", "item": "small_pack"})

        assert eng.state["pending_decision"] is None, "回到容量内应解除决策"
        assert loot.count(eng.state, "hiking_pack") == 1, "先拿的物品保留在包里"
        assert len(eng.state["inventory"]) == eng._bag_cap(), "丢 1 拿 1 仍是满格"

    asyncio.run(run())


def test_discard_skip_keeps_blocking():
    """bag_overflow 时选择 skip 不能蒙混过关——必须真丢东西。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        eng._acquire("hiking_pack", 1)
        assert eng.state["pending_decision"] == "bag_overflow"

        await eng._act_discard({"choice": "skip"})

        assert eng.state["pending_decision"] == "bag_overflow", "skip 应继续阻断"
        assert loot.count(eng.state, "hiking_pack") == 1, "先拿的物品不被没收"

    asyncio.run(run())


def test_packrat_talent_adds_capacity():
    """背包客天赋应让容量 +5。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        T.apply(cfg, eng.state, T.get_by_id(cfg, "packrat"))
        assert eng._bag_cap() == _base(cfg) + 5, "packrat 应 +5 格"

    asyncio.run(run())


def test_equip_backpack_adds_slots():
    """装备背包应把其 slots 叠加进容量。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
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
        eng.state["talent"] = None
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
        eng.state["talent"] = None
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
        eng.state["talent"] = None
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
