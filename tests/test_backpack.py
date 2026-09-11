"""回归测试：背包容量系统。

设计要点（用户需求）：
  - 单格 = 一个物品条目；可堆叠物资（弹药/绷带/材料/纪念品）只占 1 格，qty 累加不占新格，
    天然限制"无限囤积"。
  - 初始容量 = balance.player.bag_slots（当前 8）；满包拾取非堆叠新物 → bag_full 决策。
  - 背包天赋（packrat）背包 +5；背包本身是可装备槽（slots 叠加容量）；护甲口袋（pockets）也叠加。
  - 换上更小装备导致容量缩水 → bag_overflow（必须丢到装得下为止）。

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

    asyncio.run(run())


def test_bag_full_triggers_on_new_nonstackable():
    """背包满时拾取一个全新非堆叠物品，应触发 bag_full 决策并暂存待拾取物。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)

        ok = eng._acquire("hiking_pack", 1)  # 全新非堆叠
        assert not ok, "满包应拒绝入包"
        assert eng.state["pending_decision"] == "bag_full", "应进入 bag_full 决策"
        assert eng.state.get("bag_pending", {}).get("id") == "hiking_pack", "应暂存待拾取物"

    asyncio.run(run())


def test_drop_frees_slot_then_acquires_pending():
    """bag_full 时丢弃一件 → 腾出空间自动装入暂存的待拾取物，决策解除。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        eng._acquire("hiking_pack", 1)
        assert eng.state["pending_decision"] == "bag_full"

        await eng._act_discard({"choice": "drop", "item": "small_pack"})

        assert eng.state["pending_decision"] is None, "腾位后应解除决策"
        assert loot.count(eng.state, "hiking_pack") == 1, "暂存物应已入包"
        assert len(eng.state["inventory"]) == eng._bag_cap(), "丢 1 拿 1 仍是满格"

    asyncio.run(run())


def test_bag_full_skip_abandons_pending():
    """bag_full 时选择"放弃新物"，决策解除且不入包。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["talent"] = None
        _fill_to_cap(eng, cfg)
        eng._acquire("hiking_pack", 1)
        assert eng.state["pending_decision"] == "bag_full"

        await eng._act_discard({"choice": "skip"})

        assert eng.state["pending_decision"] is None
        assert loot.count(eng.state, "hiking_pack") == 0, "放弃则不应入包"

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
    print("✓ 默认容量 = bag_slots")
    test_stackable_does_not_consume_slots()
    print("✓ 可堆叠不占格")
    test_bag_full_triggers_on_new_nonstackable()
    print("✓ 满包触发 bag_full")
    test_drop_frees_slot_then_acquires_pending()
    print("✓ 丢弃腾位后装入新物")
    test_bag_full_skip_abandons_pending()
    print("✓ bag_full 放弃新物")
    test_packrat_talent_adds_capacity()
    print("✓ 背包天赋 +5")
    test_equip_backpack_adds_slots()
    print("✓ 装备背包叠加 slots")
    test_armor_pockets_add_capacity()
    print("✓ 护甲口袋叠加容量")
    test_unequip_smaller_armor_triggers_overflow()
    print("✓ 换小装备触发溢出")
    test_backpack_serialized_to_response()
    print("✓ 容量序列化到响应")
    print("\n背包容量回归测试全部通过")
