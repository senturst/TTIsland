"""回归测试：所有物品都必须能被玩家看见。

曾经的问题：全家福、婚戒这类**纪念品**拿到手后完全不显示。

原因有两层：
  1. `available_actions` 只给消耗品和武器/护甲生成按钮，
     纪念品 / 材料 / 弹药这三类没有任何动作，因此不会出现在命令区
  2. 界面上又没有背包面板

所以"物品可见性"不能只靠命令区的按钮来保证。
这里把两个不变量钉住：
  - 配置里每个物品/怪物都有图标（新增内容忘了配图标会直接失败）
  - run 响应里每个背包条目都能查到图标，且该物品确实在响应中
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


def test_every_configured_item_has_icon():
    """新增物品时忘了配 icon，界面上会显示成一个不明方块。"""
    cfg = get_config()
    missing = [iid for iid, it in cfg.items.items() if not it.get("icon")]
    assert not missing, f"以下物品缺少 icon，请在 configs/items.yaml 里补上: {missing}"


def test_every_configured_monster_has_icon():
    cfg = get_config()
    missing = [mid for mid, m in cfg.monsters.items() if not m.get("icon")]
    assert not missing, f"以下怪物缺少 icon，请在 configs/monsters.yaml 里补上: {missing}"


def test_every_level_has_icon():
    cfg = get_config()
    missing = [lv for lv, t in cfg.levels.items() if not t.get("icon")]
    assert not missing, f"以下层缺少 icon，请在 configs/level_themes.yaml 里补上: {missing}"


def test_non_actionable_items_are_still_reported():
    """纪念品 / 材料 / 弹药没有操作按钮，但必须出现在 state.inventory 与 icons 里，
    否则前端那个「随身」面板无从渲染——这正是全家福不显示的根因。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})

        # 每种"没有操作按钮"的类别各放一个
        probe = {"photo": 1, "scrap": 2, "ammo_pistol": 5}
        for iid, qty in probe.items():
            loot.grant(cfg, eng.state, iid, qty)

        resp = eng._response()
        inv = {e["id"]: e for e in resp["state"]["inventory"]}
        icons = resp["icons"]

        for iid in probe:
            assert iid in inv, f"{iid} 未出现在 state.inventory —— 玩家看不到它"
            assert icons.get(iid), f"{iid} 缺少图标映射 —— 前端渲染不出"
            entry = inv[iid]
            assert entry["qty"] >= 1
            # 明确断言这些类别**没有**操作能力，避免有人误以为漏了按钮
            assert not entry["usable"], f"{iid} 不该被当作可用消耗品"
            assert not entry["wearable"], f"{iid} 不该被当作可装备物品"

        # 计分道具的类别必须正确，否则前端的分类配色会错
        assert inv["photo"]["kind"] == "trinket"
        assert inv["scrap"]["kind"] == "material"
        assert inv["ammo_pistol"]["kind"] == "ammo"

    asyncio.run(run())


def test_status_action_prints_full_inventory():
    """命令行玩家靠 `状态` 看背包；文本清单必须包含纪念品。"""
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})
        loot.grant(cfg, eng.state, "photo", 1)

        eng._out = []
        await eng._act_status({})
        text = "\n".join(eng._out)
        assert "全家福" in text, f"`状态` 输出里没有纪念品:\n{text}"
        assert "随身" in text, f"缺少随身清单段落:\n{text}"

    asyncio.run(run())


def test_items_carry_numeric_desc():
    """道具必须在响应里带数值摘要（desc），玩家不用真的用一次就知道它干什么。

    这是修复"道具不显示具体数值，得使用一次才知道"的核心不变量：
    每件有数值意义的道具，其序列化结果都必须带一段人类可读的数值说明。
    """
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})

        # 开局自带武器，必须带 desc
        w = eng._response()["state"]["weapon"]
        assert w["desc"], f"已装备武器缺少数值摘要: {w}"

        # 每类各放一件，检查 desc
        probe = {
            "bandage": "consumable",      # 回复 8–13 · 感染 -5
            "leather_jacket": "armor",    # 防御 +2 · 闪避 -5
            "crowbar": "weapon",          # 伤害 6–10 · 暴击 5% · 静音 · 耐久 20
            "wedding_ring": "trinket",    # 计分 +15
            "ammo_pistol": "ammo",        # 每拾 3–6
            "scrap": "material",          # 无数值，desc 应为空串
        }
        for iid in probe:
            loot.grant(cfg, eng.state, iid, 1)

        inv = {e["id"]: e for e in eng._response()["state"]["inventory"]}
        for iid, kind in probe.items():
            assert iid in inv, f"{iid} 没出现在背包"
            d = inv[iid]["desc"]
            if kind == "material":
                assert d == "", f"{iid}(材料) 不该有数值摘要，实际: {d!r}"
            else:
                assert d, f"{iid}({kind}) 缺少数值摘要 desc"

    asyncio.run(run())


if __name__ == "__main__":
    test_every_configured_item_has_icon()
    print("✓ 所有物品都有图标")
    test_every_configured_monster_has_icon()
    print("✓ 所有怪物都有图标")
    test_every_level_has_icon()
    print("✓ 所有层都有图标")
    test_non_actionable_items_are_still_reported()
    print("✓ 纪念品/材料/弹药都会出现在响应里")
    test_status_action_prints_full_inventory()
    print("✓ `状态` 能列出完整背包")
    print("\n物品可见性回归测试全部通过")
