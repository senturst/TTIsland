"""回归测试：商人（现金买卖/废铁或现金修理/出售换现金/感染商人）+ 搜索多件 + 墓碑拾取远程。

背景：
  - 废铁/现金是商人的两类硬通货：废铁或现金都能修近战武器与护甲；现金用于买卖、出售换现金。
  - 商人房进入时一次性随机铺货（1 武器 / 1 装备 / 1 背包 / 3 其他），买卖/修理/出售集中在前端面板，
    命令区只保留「离开」。
  - 搜索原本一次只产单类单件；改为"拿到一件后按概率递减续 roll，封顶 N"。
  - 远程武器无耐久、只吃弹药，应能被墓碑拾取（tier≤2 进池，tier3 重火力除外）。
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
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


def _enter_merchant_room(eng, plagued=False):
    """把当前房间改成商人房并触发一次铺货（等价于走进商人房）。"""
    room = eng.state["level_map"]["rooms"][eng.state["level_map"]["current"]]
    room["type"] = "merchant"
    room["resolved"] = False
    eng.state["room"] = {"type": "merchant", "name": "流浪商人"}
    eng.state["in_combat"] = False
    eng.state["combat"] = {}
    # 直接走引擎的进房铺货逻辑（plagued 概率设为 0 得到普通商人）
    eng._enter_merchant(room)
    if plagued:
        eng.state["room"]["merchant"]["type"] = "plagued"
        eng.state["room"]["merchant"]["discount"] = 0.5
    return eng.state["room"]["merchant"]


# ----------------------------------------------------------------------
def test_merchant_repair_consumes_scrap_and_restores_durability():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 把开局近战武器弄残，再给足废铁与现金
        eng.state["weapon"]["durability"] = 5
        loot.grant(cfg, eng.state, "scrap", 20)
        loot.grant(cfg, eng.state, "cash", 20)
        _enter_merchant_room(eng)

        before = loot.count(eng.state, "scrap")
        await eng._act_merchant({"choice": "repair", "pay": "scrap"})

        after = loot.count(eng.state, "scrap")
        assert after < before, "修复应当消耗废铁"
        # 修复后当前耐久 = 新（已缩水）上限
        new_cap = int(round(
            int(cfg.item(eng.state["weapon"]["id"]).get("durability", 0) or 0)
            * (1.0 - float(cfg.balance["merchant"]["repair"]["durability_loss_pct"]
                          .get(cfg.item(eng.state["weapon"]["id"]).get("tier", 1), 0.25)))
        ))
        assert eng.state["weapon"]["durability"] == new_cap, "修复后耐久应等于缩水后的新上限"
        assert any("废铁" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_merchant_repair_insufficient_scrap_is_noop():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["weapon"]["durability"] = 5
        loot.grant(cfg, eng.state, "scrap", 0)  # 没废铁
        _enter_merchant_room(eng)

        await eng._act_merchant({"choice": "repair", "pay": "scrap"})
        assert eng.state["weapon"]["durability"] == 5, "废铁不足时不应修复"
        assert any("废铁不够" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_merchant_buy_spends_cash():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        loot.grant(cfg, eng.state, "cash", 10)
        m = _enter_merchant_room(eng)
        # 注入一个已知价货架条目（cost 走现金），避免依赖随机铺货
        m["shop"] = [{"id": "bandage", "cost": 2, "value": 3, "kind": "consumable"}]
        before = loot.count(eng.state, "cash")

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        after = loot.count(eng.state, "cash")
        assert after == before - 2, f"买绷带应耗 2 现金，实际 {before}→{after}"
        assert loot.count(eng.state, "bandage") >= 1, "应买到绷带"

    asyncio.run(run())


def test_merchant_panel_state_exposed():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        _enter_merchant_room(eng)
        # 造一把残血近战武器，确保 repair_options 出现它
        eng.state["weapon"]["durability"] = 8

        resp = eng._response()
        st = resp["state"]
        assert st["merchant"], "应暴露 merchant 状态"
        assert st["merchant"]["shop"], "应有铺货"
        assert st["repair_options"], "残血武器应进入 repair_options"
        assert any(r["id"] == eng.state["weapon"]["id"] for r in st["repair_options"])
        # 命令区只保留「离开」，买卖/修理交给面板
        acts = resp["available_actions"]
        assert any(a.get("choice") == "leave" for a in acts), "应有离开按钮"
        assert not any(a.get("choice") in ("repair", "buy") for a in acts), \
            "命令区不应再塞 repair/buy 按钮"

    asyncio.run(run())


def test_merchant_sell_grants_cash():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 纪念品（非弹药/非消耗品/非现金）可出售
        loot.grant(cfg, eng.state, "dog_tag", 3)
        _enter_merchant_room(eng)
        before = loot.count(eng.state, "cash")

        await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
        after = loot.count(eng.state, "cash")
        assert after > before, "出售应换得现金"
        assert loot.count(eng.state, "dog_tag") == 0, "出售后库存应清空"

    asyncio.run(run())


def test_merchant_plagued_min_hp_and_hp_cost():
    """感染商人：血量 ≥ 50% 最大生命即可交易；成交时抽走「缺失的血量」。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 25/40 = 62.5% ≥ 50% 门槛：可交易，抽走缺失的 15 点
        eng.state["hp"] = 25
        eng.state["hp_max"] = 40
        m = _enter_merchant_room(eng, plagued=True)
        m["shop"] = [{"id": "bandage", "cost": 2, "value": 3, "kind": "consumable"}]
        loot.grant(cfg, eng.state, "cash", 10)

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        assert any("缺的血全抽走" in line for line in eng._out), eng._out
        assert eng.state["hp"] == 10, f"应抽走缺失的 15 点（25→10），实际 {eng.state['hp']}"
        assert loot.count(eng.state, "cash") == 8, "应按折扣价扣现金"
        # 开局自带 1 个 bandage，买 1 个后应为 2
        assert loot.count(eng.state, "bandage") == 2, "应买到 1 个 bandage"

    asyncio.run(run())


def test_merchant_plagued_rejects_below_half_hp():
    """感染商人：血量低于 50% 最大生命 → 拒绝交易且不扣血。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["hp"] = 10
        eng.state["hp_max"] = 40  # 25% < 50% 门槛
        m = _enter_merchant_room(eng, plagued=True)
        m["shop"] = [{"id": "bandage", "cost": 2, "value": 3, "kind": "consumable"}]

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        assert any("至少要保有" in line for line in eng._out), eng._out
        assert eng.state["hp"] == 10, "拒绝交易不应扣血"
        # 开局自带 1 个 bandage，拒绝交易后不应增加
        assert loot.count(eng.state, "bandage") == 1, "拒绝交易不应拿到货"

    asyncio.run(run())


# ----------------------------------------------------------------------
def test_grave_pick_ranged_weapon():
    """墓碑应能让玩家拾取 tier≤2 的远程武器（无耐久，靠弹药平衡）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["room"] = {
            "type": "grave",
            "grave": {
                "player_name": "某人",
                "gear": [{
                    "id": "pistol_m9", "name": "M9 手枪", "kind": "weapon",
                    "tier": 2, "durability": None, "uid": "u123",
                }],
                "looted": False,
            },
        }
        eng.state["level_map"]["rooms"][eng.state["level_map"]["current"]]["type"] = "grave"

        await eng._act_grave({"choice": "loot"})
        assert eng.state["pending_decision"] == "grave_pick"
        await eng._act_grave({"choice": "take", "uid": "u123"})

        # 拾取成功，进入背包
        inv = {e["id"]: e for e in eng.state["inventory"]}
        assert "pistol_m9" in inv, "应拾取到 M9 手枪"
        # 远程武器无耐久字段
        assert inv["pistol_m9"].get("durability") is None
        # 标记给 API 层落盘
        assert eng.state.get("_grave_claim", {}).get("uid") == "u123"

    asyncio.run(run())


def test_grave_pool_eligibility():
    """遗物/墓碑池的约束：tier≤2 远程可被拾取，tier3 重火力被挡。"""
    cfg = get_config()
    assert cfg.legacy_allowed("pistol_m9"), "M9(tier2) 应可进墓碑池"
    assert cfg.legacy_allowed("silenced_smg"), "消音冲锋枪(tier2) 应可进墓碑池"
    assert not cfg.legacy_allowed("shotgun"), "霰弹枪(tier3) 不应进池——防滚雪球"


# ----------------------------------------------------------------------
def test_search_respects_max_items_cap():
    """单次搜索获得的物品数不得超过配置上限（默认 3，这里临时改成 2 验证封顶）。"""
    cfg = get_config()
    search_cfg = cfg.balance["loot"]["search"]
    orig = search_cfg.get("max_items")
    search_cfg["max_items"] = 2
    try:
        async def run():
            for _ in range(40):
                eng = await _new_run(cfg)
                # 强制当前房间为物资房（医疗表，无武器，便于计数）
                room = eng.state["level_map"]["rooms"][eng.state["level_map"]["current"]]
                room["type"] = "loot"
                room["tpl"] = "pharmacy"
                room["searched"] = False
                room["hazmat"] = False
                eng.state["room"] = {
                    "type": "loot", "name": "药房", "tpl": "pharmacy",
                    "idx": room["idx"], "searched": False,
                }
                await eng._act_search({})
                line = next((l for l in reversed(eng._out) if l.startswith("你找到了：")), "")
                if line:
                    n = len(line.split("：", 1)[1].split("、"))
                    assert n <= 2, f"超出 max_items 封顶: {line}"
                # 没找到也算合法（45% 失败或空房），不报错即可

        asyncio.run(run())
    finally:
        if orig is None:
            search_cfg.pop("max_items", None)
        else:
            search_cfg["max_items"] = orig


def test_search_can_yield_multiple_via_decreasing_prob():
    """在足够多的样本里，应观察到"一次搜索摸到多件"的情况（概率递减续 roll 生效）。"""
    cfg = get_config()
    search_cfg = cfg.balance["loot"]["search"]
    orig = dict(search_cfg)
    search_cfg["max_items"] = 3
    search_cfg["extra_base_chance"] = 1.0   # 强制必续，便于稳定观测多件
    search_cfg["extra_decay"] = 0.6
    try:
        async def run():
            multi = 0
            for _ in range(60):
                eng = await _new_run(cfg)
                room = eng.state["level_map"]["rooms"][eng.state["level_map"]["current"]]
                room["type"] = "loot"
                room["tpl"] = "pharmacy"
                room["searched"] = False
                room["hazmat"] = False
                eng.state["room"] = {
                    "type": "loot", "name": "药房", "tpl": "pharmacy",
                    "idx": room["idx"], "searched": False,
                }
                await eng._act_search({})
                line = next((l for l in reversed(eng._out) if l.startswith("你找到了：")), "")
                if line:
                    n = len(line.split("：", 1)[1].split("、"))
                    if n >= 2:
                        multi += 1
            assert multi > 0, "强制续 roll 下应稳定出现一次多件"

        asyncio.run(run())
    finally:
        search_cfg.clear()
        search_cfg.update(orig)


if __name__ == "__main__":
    test_merchant_repair_consumes_scrap_and_restores_durability()
    print("✓ 商人修复耗废铁并按 tier 缩水耐久上限")
    test_merchant_repair_insufficient_scrap_is_noop()
    print("✓ 废铁不足不修复")
    test_merchant_buy_spends_cash()
    print("✓ 商人买卖耗现金")
    test_merchant_panel_state_exposed()
    print("✓ 商人面板状态（merchant/repair_options）正确暴露")
    test_merchant_sell_grants_cash()
    print("✓ 出售道具换现金")
    test_merchant_plagued_min_hp_and_hp_cost()
    print("✓ 感染商人半血可交易且抽走缺失血量")
    test_merchant_plagued_rejects_below_half_hp()
    print("✓ 感染商人血量不过半拒绝交易")
    test_grave_pick_ranged_weapon()
    print("✓ 墓碑可拾取远程武器")
    test_grave_pool_eligibility()
    print("✓ 墓碑池 tier 约束正确")
    test_search_respects_max_items_cap()
    print("✓ 搜索封顶 max_items")
    test_search_can_yield_multiple_via_decreasing_prob()
    print("✓ 搜索概率递减多件生效")
    print("\n商人/搜索/墓碑远程 回归测试全部通过")
