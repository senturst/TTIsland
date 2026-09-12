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
import math
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
    # 隔离随机开局天赋：快速凝血（+2 绷带）等会打破「裸开局基线」断言
    # （曾致 test_merchant_plagued_full_price_when_low 1/20 偶发失败）
    eng.state["talents"] = []
    # 进地牢第一步可能随机遇敌：战斗中 bag_overflow 按设计延后，
    # 会打破「拾取即弹决策」类断言——商人/背包测试默认非战斗基线
    eng.state["in_combat"] = False
    eng.state.setdefault("combat", {})
    eng.state["combat"]["enemies"] = []
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
        m = eng.state["room"]["merchant"]
        m["type"] = "plagued"
        m["toll_hp"] = 20
        m["discount"] = 0.5
        m["toll_armed"] = False
    return eng.state["room"]["merchant"]


# ----------------------------------------------------------------------
def test_merchant_repair_consumes_scrap_and_restores_durability():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 把开局近战武器弄残，再给足废料与现金
        eng.state["weapon"]["durability"] = 5
        loot.grant(cfg, eng.state, "scrap", 20)
        loot.grant(cfg, eng.state, "cash", 20)
        _enter_merchant_room(eng)

        before = loot.count(eng.state, "scrap")
        await eng._act_merchant({"choice": "repair", "pay": "scrap"})

        after = loot.count(eng.state, "scrap")
        assert after < before, "修复应当消耗废料"
        # 每次消耗 1 废料修 floor(1/0.3)=3 点耐久（余数舍弃）
        per = float(cfg.balance["merchant"]["repair"]["scrap_per_point"])
        pts = int(1 / per) if per > 0 else 1
        assert eng.state["weapon"]["durability"] == 5 + pts, \
            f"一次修理应修 {pts} 点耐久（5→{5 + pts}），实际 {eng.state['weapon']['durability']}"
        assert after == before - 1, "应只消耗 1 个废料"
        assert any("废料" in line for line in eng._out), eng._out

        # 连续修可以逐步修满，不再降低耐久上限
        maxd = int(cfg.item(eng.state["weapon"]["id"]).get("durability", 0) or 0)
        for _ in range(maxd + 5):
            await eng._act_merchant({"choice": "repair", "pay": "scrap"})
            if eng.state["weapon"]["durability"] >= maxd:
                break
        assert eng.state["weapon"]["durability"] == maxd, "可逐次把武器修满"

    asyncio.run(run())


def test_merchant_repair_insufficient_scrap_is_noop():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["weapon"]["durability"] = 5
        loot.grant(cfg, eng.state, "scrap", 0)  # 没废料
        _enter_merchant_room(eng)

        await eng._act_merchant({"choice": "repair", "pay": "scrap"})
        assert eng.state["weapon"]["durability"] == 5, "废料不足时不应修复"
        assert any("不够" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_merchant_buy_spends_cash():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        loot.grant(cfg, eng.state, "cash", 10)
        m = _enter_merchant_room(eng)
        # 注入一个已知价货架条目（新机制：按 value 结算），避免依赖随机铺货
        m["shop"] = [{"id": "bandage", "cost": 3, "value": 3, "kind": "consumable"}]
        before = loot.count(eng.state, "cash")

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        after = loot.count(eng.state, "cash")
        assert after == before - 3, f"买绷带应耗 3 现金，实际 {before}→{after}"
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
        value = int(cfg.item("dog_tag").get("value", 1))
        ratio = float(cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
        price = max(1, int(round(value * ratio)))
        assert after == before + price, f"出售应换得单件回收价 {price}，实际 {before}→{after}"
        assert loot.count(eng.state, "dog_tag") == 2, "单件出售：3 件应剩 2 件"

    asyncio.run(run())


def test_plagued_toll_pays_hp_and_half_price_once():
    """感染商人血税：上交 20 生命 → 下一件半价（一次性），第二件回原价。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["hp"] = 30
        st["hp_max"] = 40
        m = _enter_merchant_room(eng, plagued=True)
        m["shop"] = [
            {"id": "bandage", "cost": 3, "value": 3, "kind": "consumable"},
            {"id": "canned", "cost": 3, "value": 3, "kind": "consumable"},
        ]
        loot.grant(cfg, st, "cash", 10)

        # 上交血税
        await eng._act_merchant({"choice": "toll"})
        assert st["hp"] == 10, f"应上交 20 点（30→10），实际 {st['hp']}"
        assert m["toll_armed"] is True, "上交后应武装半价"
        # 出售不经手（没有血液消耗的断言点在 full_price 旧用例位置）
        # 半价买第一件
        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        assert loot.count(st, "cash") == 8, "半价应扣 ceil(3×0.5)=2"
        assert m["toll_armed"] is False, "半价一次性，买完即失效"
        assert loot.count(st, "bandage") == 2, "应买到 1 个 bandage"
        # 第二件回原价
        await eng._act_merchant({"choice": "buy", "item": "canned"})
        assert loot.count(st, "cash") == 5, "无血税应按原价 3 扣款"

    asyncio.run(run())


def test_plagued_no_toll_full_price_and_no_blood_loss():
    """不上交血税：按原价购买，生命值纹丝不动（旧的成交抽血机制已废弃）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["hp"] = 25
        st["hp_max"] = 40
        m = _enter_merchant_room(eng, plagued=True)
        m["shop"] = [{"id": "bandage", "cost": 3, "value": 3, "kind": "consumable"}]
        loot.grant(cfg, st, "cash", 10)

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        assert st["hp"] == 25, "无血税不应扣血"
        assert loot.count(st, "cash") == 7, "应按原价 3 扣款"
        assert loot.count(st, "bandage") == 2

        # 出售也不抽血（活扳手 t1 近战，可卖）
        loot.grant(cfg, st, "wrench", 1)
        await eng._act_merchant({"choice": "sell", "item": "wrench"})
        assert st["hp"] == 25, "出售不应扣血"

    asyncio.run(run())


def test_plagued_toll_insufficient_hp():
    """血量不超过 toll_hp 时拒绝上交（付完至少留 1 点）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["hp"] = 20
        st["hp_max"] = 40
        m = _enter_merchant_room(eng, plagued=True)

        await eng._act_merchant({"choice": "toll"})
        assert st["hp"] == 20, "血不够不应扣"
        assert m["toll_armed"] is False, "不应武装半价"
        assert any("不够" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_plagued_toll_low_hp_still_trades_full_price():
    """低血量玩家不上交也能正常原价交易（旧『血量不过半按原价』的门槛机制废弃）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["hp"] = 5
        st["hp_max"] = 40  # 12.5%——旧机制此处会转"原价+不抽血"，新机制无门槛概念
        m = _enter_merchant_room(eng, plagued=True)
        m["shop"] = [{"id": "bandage", "cost": 3, "value": 3, "kind": "consumable"}]
        loot.grant(cfg, st, "cash", 10)

        await eng._act_merchant({"choice": "buy", "item": "bandage"})
        assert st["hp"] == 5, "不应扣血"
        assert loot.count(st, "cash") == 7, "原价 3 扣款"
        assert loot.count(st, "bandage") == 2

    asyncio.run(run())


def test_merchant_sell_rejects_after_same_item_sold():
    """商人名下同款商品槽已成交后，不再收购同款（防买折价→卖回收价套利）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        m = _enter_merchant_room(eng)
        m["shop"] = [{"id": "dog_tag", "cost": 2, "value": 5, "kind": "trinket"}]
        ratio = float(cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
        loot.grant(cfg, eng.state, "dog_tag", 2)
        loot.grant(cfg, eng.state, "cash", 10)
        before = loot.count(eng.state, "cash")

        # 先卖一件：成交，货架同款标记 sold
        await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
        assert loot.count(eng.state, "cash") > before, "第一件应正常成交"

        # 再卖第二件：同款槽已 sold，拒收
        await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
        assert any("不收第二件" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "dog_tag") == 1, "第二件应拒收"
        price = max(1, int(round(int(cfg.item("dog_tag")["value"]) * ratio)))
        assert loot.count(eng.state, "cash") == before + price, "拒收后现金只含第一件的回收价"

    asyncio.run(run())


def test_merchant_sell_rejects_low_durability_gear():
    """商人耐久门槛：武器/护甲耐久低于总耐久 50%（可配）拒收，0 耐久必拒。

    边界：恰好等于 50% 可收（"< 50% 才拒"）；无耐久概念的物品不受约束。
    """
    cfg = get_config()
    ratio_cfg = float(
        cfg.balance.get("merchant", {}).get("min_durability_ratio", 0.5)
    )
    assert ratio_cfg == 0.5, "配置默认门槛应为 0.5"

    async def run():
        eng = await _new_run(cfg)
        m = _enter_merchant_room(eng)
        m["shop"] = [{"id": "crowbar", "cost": 5, "value": 8, "kind": "weapon"}]
        maxd = int(cfg.item("crowbar").get("durability", 0) or 0)
        assert maxd == 20, "测试基线：crowbar 总耐久 20"

        # ---- 0 耐久：必拒 ----
        loot.grant(cfg, eng.state, "crowbar", 1, durability=0)
        loot.grant(cfg, eng.state, "cash", 0)
        await eng._act_merchant({"choice": "sell", "item": "crowbar"})
        assert any("磨损太厉害" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "crowbar") == 1, "0 耐久拒收，物品应还在包里"
        assert loot.count(eng.state, "cash") == 0, "拒收不应给钱"

        # ---- 49%（9/20）：拒 ----
        eng._out.clear()
        e = next(e for e in eng.state["inventory"] if e["id"] == "crowbar")
        e["durability"] = 9
        await eng._act_merchant({"choice": "sell", "item": "crowbar"})
        assert any("磨损太厉害" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "crowbar") == 1, "49% 拒收"

        # ---- 50%（10/20）：可收（边界：低于才拒）----
        eng._out.clear()
        e["durability"] = 10
        await eng._act_merchant({"choice": "sell", "item": "crowbar"})
        assert any("卖了" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "crowbar") == 0, "50% 应正常成交"
        assert loot.count(eng.state, "cash") > 0, "50% 成交应换得现金"

        # ---- 护甲同理：riot_gear 45 耐久，22/45≈48.9% 拒 ----
        eng._out.clear()
        m2 = _enter_merchant_room(eng)
        m2["shop"] = [{"id": "riot_gear", "cost": 5, "value": 10, "kind": "armor"}]
        loot.grant(cfg, eng.state, "riot_gear", 1, durability=22)
        await eng._act_merchant({"choice": "sell", "item": "riot_gear"})
        assert any("磨损太厉害" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "riot_gear") == 1, "护甲低耐久也应拒收"

        # ---- 无耐久概念的纪念品不受门槛约束 ----
        eng._out.clear()
        loot.grant(cfg, eng.state, "dog_tag", 1)
        await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
        assert any("卖了" in line for line in eng._out), eng._out
        assert loot.count(eng.state, "dog_tag") == 0, "纪念品无耐久概念，应正常成交"

    asyncio.run(run())


def test_merchant_sell_rejected_flag_exposed():
    """_response 应为低耐久装备标注 sell_rejected，前端据此禁用出售按钮。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        _enter_merchant_room(eng)
        loot.grant(cfg, eng.state, "crowbar", 1, durability=5)  # 25% < 50%
        loot.grant(cfg, eng.state, "dog_tag", 1)

        resp = eng._response()
        inv = {e["id"]: e for e in resp["state"]["inventory"]}
        assert inv["crowbar"]["sell_rejected"] is True, "低耐久武器应标拒收"
        assert inv["crowbar"]["sell"] is not None, "预估价保留（展示用）"
        assert inv["dog_tag"]["sell_rejected"] is False, "纪念品不受门槛约束"

    asyncio.run(run())


def test_backpack_auto_equip_on_acquire():
    """捡到背包自动装备：无背包直接装；有背包且更大自动换（旧的回包）；更小不动。

    修复前：捡到背包先占一格，常常恰好把背包顶过上限、被迫先丢东西——
    而丢的最佳选项往往就是刚捡的背包本身。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["backpack"] = None

        # 无背包 → 捡到自动装备
        eng._acquire("small_pack", 1)
        assert st["backpack"] and st["backpack"]["id"] == "small_pack", "无背包应自动装备"
        assert loot.count(st, "small_pack") == 0, "装备后不应留在包里"

        # 更大的 → 自动换，旧的回背包
        eng._acquire("large_pack", 1)
        assert st["backpack"]["id"] == "large_pack", "更大应自动换装"
        assert loot.count(st, "small_pack") == 1, "旧背包应回到背包里"
        assert eng._bag_cap() == 8 + 8, "基础 8 + 战术包 8"

        # 更小的 → 留在包里不抢装
        eng._acquire("small_pack", 1)
        assert st["backpack"]["id"] == "large_pack", "更小的不应抢装"
        assert loot.count(st, "small_pack") == 2, "小背包应留在包里"

    asyncio.run(run())


def test_bag_overflow_use_action():
    """背包溢出决策：消耗品提供「用了」代替白扔；用完仍超载则决策继续。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["backpack"] = None
        st["hp"] = 10
        # 基础容量 8：自带 1 条目（绷带，默认弹药已取消）+ 塞 9 件武器 = 10 条目 > 8
        for _ in range(9):
            eng._acquire("crowbar", 1, durability=20)
        assert st["pending_decision"] == "bag_overflow", "应触发溢出决策"

        acts = eng._available_actions()
        use_acts = [a for a in acts if a["id"] == "use"]
        assert use_acts and any(a["item"] == "bandage" for a in use_acts), \
            "溢出界面应有绷带的「用了」按钮"

        n_before = loot.count(st, "bandage")
        hp_before = st["hp"]
        await eng.act("use", {"item": "bandage"})
        assert loot.count(st, "bandage") == n_before - 1, "使用应消耗 1 绷带"
        assert st["hp"] > hp_before, "绷带应回血"
        assert st["pending_decision"] == "bag_overflow", "仍超载时决策应保持"

        # 丢到容量内 → 决策解除
        while st["pending_decision"] == "bag_overflow":
            await eng.act("discard", {"choice": "drop", "item": "crowbar"})
        assert st["pending_decision"] is None
        assert len(st["inventory"]) == eng._bag_cap()

    asyncio.run(run())


def test_armor_durability_exposed():
    """佩戴中的护甲耐久应下发前端（此前只有名字，玩家看不到磨损）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        eng._acquire("bike_helmet", 1)
        await eng.act("equip", {"item": "bike_helmet"})
        st["armor"]["durability"] = 13  # 模拟磨损 13/25

        resp = eng._response()["state"]
        assert resp["armor"]["name"] == "自行车头盔"
        assert resp["armor"]["durability"] == 13, "应下发剩余耐久"
        assert resp["armor"]["max_durability"] == 25, "应下发总耐久"
        assert any(
            r["id"] == "bike_helmet" and r["cur"] == 13
            for r in resp["repair_options"]
        ), "残血护甲应进入修理选项"

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
    # 消音冲锋枪提为 tier3 后不再进池——消音+连射若可跨局继承会滚雪球
    assert not cfg.legacy_allowed("silenced_smg"), "消音冲锋枪(tier3) 不应进池——防滚雪球"
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


def test_legacy_unknown_item_entries_are_cleaned():
    """旧局遗留的未知物品条目（如已删除的 cash 物品定义）在引擎加载时被清掉，
    不得让 _response 序列化炸 500。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["inventory"].append({"id": "cash", "qty": 3, "durability": None})
        eng.state["inventory"].append({"id": "bandage", "qty": 1, "durability": None})
        # 模拟 API 层 _load_engine 的加载路径
        eng2 = RunEngine(cfg, eng.state)
        assert all(e["id"] != "cash" for e in eng2.state["inventory"]), \
            "未知 ID 条目应在加载时清除"
        assert any(e["id"] == "bandage" for e in eng2.state["inventory"]), \
            "正常物品不受影响"
        resp = eng2._response()  # 曾在此处抛 ConfigError: 未知物品 ID: cash
        assert all(e["id"] != "cash" for e in resp["state"]["inventory"])

    asyncio.run(run())


def test_merchant_closes_after_trade_on_reentry():
    """商人在本房有过成交后玩家离开 → 房间 resolved，回来看到收摊。
    防双向边"成交→出门→再进来"无限刷（卖出换现金本可无限循环）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        lmap = st["level_map"]
        room = lmap["rooms"][lmap["current"]]
        room["type"] = "merchant"
        room["resolved"] = False
        st["room"] = {"type": "merchant", "name": "流浪商人", "idx": room["idx"]}
        st["in_combat"] = False
        st["combat"] = {}
        eng._enter_merchant(room)
        st["room"]["merchant"]["shop"] = [
            {"id": "bandage", "cost": 2, "value": 3, "kind": "consumable"}
        ]
        loot.grant(cfg, st, "dog_tag", 1)
        await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
        assert st["room"].get("merchant_traded"), "成交应打标记"

        # 找一条出口走出去再回来（测试图若无回路则跳过回程，只验 resolved）
        exits = [e["to"] for e in room.get("exits") or []]
        if exits:
            await eng._act_move({"to": exits[0]})
            assert room.get("resolved"), "成交后离开房间，商人应收摊"
            # 走回来（若有回程边）
            back = next(
                (e["to"] for e in lmap["rooms"][exits[0]]["exits"] if e["to"] == room["idx"]),
                None,
            )
            if back is not None:
                await eng._act_move({"to": back})
                n_before = loot.count(st, "dog_tag")
                loot.grant(cfg, st, "dog_tag", 1)
                await eng._act_merchant({"choice": "sell", "item": "dog_tag"})
                assert any("不在了" in line for line in eng._out), eng._out
                assert loot.count(st, "dog_tag") == n_before + 1, "收摊后不能再卖"

    asyncio.run(run())


def test_cash_is_independent_counter():
    """现金是独立计数资源：grant 不进背包、不占格；count/remove 走独立通道。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        n0 = len(eng.state["inventory"])
        loot.grant(cfg, eng.state, "cash", 7)
        loot.grant(cfg, eng.state, "cash", 3)
        # 背包条目数不变（新局；旧局遗留的现金条目也能被 count 兼容读取）
        assert len(eng.state["inventory"]) == n0, "现金不应进背包"
        assert eng.state["cash"] == 10, "现金应累加到独立计数"
        assert loot.count(eng.state, "cash") == 10
        assert loot.remove(eng.state, "cash", 4)
        assert loot.count(eng.state, "cash") == 6
        assert not loot.remove(eng.state, "cash", 99), "余额不足时移除应失败"

    asyncio.run(run())


def test_field_repair_outside_merchant():
    """非商人区域可用废料逐点修武器（战斗中被拒绝）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["weapon"]["durability"] = 5
        loot.grant(cfg, eng.state, "scrap", 10)
        # 开局可能随机刷进遭遇战——修理只应在非战斗状态可用
        eng.state["in_combat"] = False

        await eng.act("repair", {"item": eng.state["weapon"]["id"], "pay": "scrap"})
        per = float(cfg.balance["merchant"]["repair"]["scrap_per_point"])
        pts = int(1 / per) if per > 0 else 1
        assert eng.state["weapon"]["durability"] == 5 + pts, "非商人区修理应消耗 1 废料修 3 点"

        # 战斗中拒绝
        eng.state["in_combat"] = True
        n = eng.state["weapon"]["durability"]
        await eng.act("repair", {"item": eng.state["weapon"]["id"], "pay": "scrap"})
        assert eng.state["weapon"]["durability"] == n, "战斗中不应能修"

    asyncio.run(run())


def test_fists_unobtainable():
    """拳头任何渠道不可获得：商店与武器掉落池全部过滤 weight≤0 的武器。"""
    cfg = get_config()
    fists = next(w for w in cfg.items_cfg["weapons"] if w["id"] == "fists")
    assert int(fists.get("weight", 0) or 0) == 0, "拳头配置 weight 必须为 0"

    async def run():
        eng = await _new_run(cfg)
        mcfg = cfg.balance.get("merchant", {})
        for _ in range(200):
            entries = eng._roll_shop(mcfg, 1.0)
            assert all(e["id"] != "fists" for e in entries), "商店不得出现拳头"
        # 武器掉落池：_roll_weapon 直接采集入包，采 100 次不应获得拳头
        from app.core import loot as _loot
        for _ in range(100):
            eng._roll_weapon([])
        assert _loot.count(eng.state, "fists") == 0, "武器掉落不得出现拳头"

    asyncio.run(run())


def test_merchant_ammo_slot_by_region():
    """P9 弹药槽：商人固定卖一叠当前地区最低档弹药（地区1=T1×8，地区2=T4×8）。

    一叠一次性购买（sold 标记），整叠价 = value × 叠数。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        loot.grant(cfg, st, "cash", 100)
        _enter_merchant_room(eng)  # 地区 1（depth 1）

        shop = st["room"]["merchant"]["shop"]
        # 专属弹药槽 = qty 为一叠（8 发）的条目；other_pool 随机弹（单发）不算
        ammo_entry = next(
            (s2 for s2 in shop if s2.get("kind") == "ammo" and s2.get("qty") == 8), None
        )
        assert ammo_entry and ammo_entry["id"] == "ammo_t1", f"地区 1 商人应卖 T1 弹药: {shop}"
        assert ammo_entry["qty"] == 8 and ammo_entry["cost"] == 24,             f"一叠应为 8 发 / 24 现金，实际 {ammo_entry}"

        before = loot.count(st, "ammo_t1")
        cash_before = loot.count(st, "cash")
        await eng._act_merchant({"choice": "buy", "item": "ammo_t1"})
        assert loot.count(st, "ammo_t1") == before + 8, "应买到一叠 8 发"
        assert loot.count(st, "cash") == cash_before - 24, "应扣整叠价 24"
        assert ammo_entry["sold"] is True, "售罄标记（一叠一次性）"
        # 二次购买同款 → 已易主
        await eng._act_merchant({"choice": "buy", "item": "ammo_t1"})
        assert loot.count(st, "ammo_t1") == before + 8, "已售出不可回购"

    asyncio.run(run())

    async def run2():
        eng = await _new_run(cfg)
        st = eng.state
        st["depth"] = 6
        loot.grant(cfg, st, "cash", 100)
        _enter_merchant_room(eng)  # 地区 2

        shop = st["room"]["merchant"]["shop"]
        ammo_entry = next(
            (s2 for s2 in shop if s2.get("kind") == "ammo" and s2.get("qty") == 8), None
        )
        assert ammo_entry and ammo_entry["id"] == "ammo_t4",             f"地区 2 商人应卖 T4 军用弹药: {shop}"
        assert ammo_entry["qty"] == 8 and ammo_entry["cost"] == 64,             f"一叠应为 8 发 / 64 现金，实际 {ammo_entry}"

    asyncio.run(run2())


def test_armor_repair_reduces_max_durability():
    """修甲每修一次耐久上限 −1；修满那一刀的溢出被钳掉；修武器不影响上限。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["armor"] = {"id": "bike_helmet", "durability": 10}  # 出厂 25
        loot.grant(cfg, st, "scrap", 10)
        st["in_combat"] = False

        await eng.act("repair", {"item": "bike_helmet", "pay": "scrap"})
        a = st["armor"]
        per = float(cfg.balance["merchant"]["repair"]["scrap_per_point"])
        pts = int(1 / per) if per > 0 else 1
        assert a["durability"] == 10 + pts, f"应修 {pts} 点，实际 {a['durability']}"
        assert a["max_durability"] == 24, "修一次耐久上限应 −1（25→24）"

        # 只差 1 点满时修：+3 点被上限回缩钳成 +1（23/24 → 24/23 后钳 23/23）
        st["armor"]["durability"] = 23
        await eng.act("repair", {"item": "bike_helmet", "pay": "scrap"})
        a = st["armor"]
        assert a["max_durability"] == 23
        assert a["durability"] == 23, "修满那一刀溢出应被钳到新上限"
        # 修满后不再出现在修理选项
        assert not any(
            r["id"] == "bike_helmet" for r in eng._repair_options()
        ), "实例上限内无可修"

        # 武器修理不引入实例上限
        st["weapon"]["durability"] = 5
        await eng.act("repair", {"item": st["weapon"]["id"], "pay": "scrap"})
        w = st["weapon"]
        assert w["durability"] == 5 + pts
        assert "max_durability" not in w, "武器没有实例上限概念"

    asyncio.run(run())


if __name__ == "__main__":
    test_fists_unobtainable()
    print("✓ 拳头任何渠道不可获得")
    test_merchant_repair_consumes_scrap_and_restores_durability()
    print("✓ 商人修复逐点耗废料且可修满不降上限")
    test_merchant_repair_insufficient_scrap_is_noop()
    print("✓ 废铁不足不修复")
    test_merchant_buy_spends_cash()
    print("✓ 商人买卖耗现金")
    test_merchant_panel_state_exposed()
    print("✓ 商人面板状态（merchant/repair_options）正确暴露")
    test_merchant_sell_grants_cash()
    print("✓ 出售道具换现金")
    test_plagued_toll_pays_hp_and_half_price_once()
    print("✓ 感染商人血税：上交20生命→下一件半价（一次性）")
    test_plagued_no_toll_full_price_and_no_blood_loss()
    print("✓ 感染商人不上交：原价交易零扣血（旧抽血机制废弃）")
    test_plagued_toll_insufficient_hp()
    print("✓ 血量不足拒绝上交")
    test_plagued_toll_low_hp_still_trades_full_price()
    print("✓ 低血量原价交易（门槛机制废弃）")
    test_merchant_sell_rejects_after_same_item_sold()
    print("✓ 同款商品槽成交后拒收第二件")
    test_merchant_sell_rejects_low_durability_gear()
    print("✓ 低耐久武器/护甲商人拒收（0/49%/50% 边界 + 纪念品豁免）")
    test_merchant_sell_rejected_flag_exposed()
    print("✓ sell_rejected 标记暴露给前端")
    test_backpack_auto_equip_on_acquire()
    print("✓ 捡到背包自动装备/自动换更大")
    test_bag_overflow_use_action()
    print("✓ 溢出决策消耗品可「用了」+ 丢弃解除")
    test_armor_durability_exposed()
    print("✓ 佩戴中护甲耐久下发（自行车头盔问题）")
    test_merchant_closes_after_trade_on_reentry()
    print("✓ 成交后离开房间商人收摊（防双向边回刷）")
    test_grave_pick_ranged_weapon()
    print("✓ 墓碑可拾取远程武器")
    test_grave_pool_eligibility()
    print("✓ 墓碑池 tier 约束正确")
    test_search_respects_max_items_cap()
    print("✓ 搜索封顶 max_items")
    test_search_can_yield_multiple_via_decreasing_prob()
    print("✓ 搜索概率递减多件生效")
    test_cash_is_independent_counter()
    print("✓ 现金独立计数（不进背包、不占格）")
    test_legacy_unknown_item_entries_are_cleaned()
    print("✓ 旧局遗留未知物品条目加载时清理，不炸响应")
    test_field_repair_outside_merchant()
    test_armor_repair_reduces_max_durability()
    print("✓ 非商人区域废料逐点修理")
    test_merchant_ammo_slot_by_region()
    print("✓ 弹药槽：地区最低档一叠（T1/T4）一次性购买")
    print("\n商人/搜索/墓碑远程 回归测试全部通过")
