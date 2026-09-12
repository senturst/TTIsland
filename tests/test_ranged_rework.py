"""回归测试：P9 弹匣系统（任意枪装任意弹 + 弹夹装填 + 弹药伤害加成）。

背景（用户拍板）：
  1. 弹药 T1-T6（劣质/制式/精工/军用/实验/原型），伤害加成
     90%/100%/105%/115%/125%/150%（远程每发子弹伤害百分比）
  2. **所有远程武器可装填任意弹种**——不再限制 t1 枪只能用 t1 弹；
     武器差异靠 弹匣容量/burst/命中/暴击，伤害成长靠弹药品质
  3. 弹匣：容量 mag_size 可配置；战斗中装填耗 1 回合（触发敌人回合），
     非战斗装填不推进任何计时；换弹种时旧弹退回背包
  4. 射击只消耗弹匣内子弹；burst 弹匣不足时有多少打多少
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

import app.core.combat as C  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


def _equip(cfg, eng, weapon_id):
    """把武器塞进玩家手里（绕过装备流程，直接指定；空弹匣）。"""
    eng.state["weapon"] = {"id": weapon_id}


def _no_drops(eng):
    """屏蔽一切获得物品的途径（击杀掉落会污染弹药计数断言）。"""
    eng._acquire = lambda *a, **k: None


async def _load(eng, ammo_id: str, n: int) -> int:
    """给当前武器装填：先塞 n 发进背包再走真实装填动作，返回弹匣内发数。"""
    cfg = get_config()
    loot.grant(cfg, eng.state, ammo_id, n)
    await eng._act_reload({"ammo": ammo_id})
    return int((eng.state.get("weapon") or {}).get("clip_count") or 0)


# ---------------------------------------------------------------------------
# 1. 弹药 T1-T6：全枪通用 + mag_size 配置
# ---------------------------------------------------------------------------

def test_all_ranged_weapons_have_mag():
    """每把远程武器都配置了 mag_size（>0），且任意枪可装任意弹种。"""
    cfg = get_config()
    ranged = [w for w in cfg.items_cfg["weapons"] if w.get("kind") == "ranged"]
    assert ranged, "items.yaml 里应有远程武器"
    for w in ranged:
        assert int(w.get("mag_size", 0) or 0) > 0, f"{w['id']} 缺 mag_size"

    async def run():
        eng = await _new_run(cfg)
        # 每把枪各装填一次 t6（最高档）——通用性验证
        for w in ranged:
            eng.state["weapon"] = {"id": w["id"]}
            eng.state["in_combat"] = False
            loot.grant(cfg, eng.state, "ammo_t6", 3)
            await eng._act_reload({"ammo": "ammo_t6"})
            assert (eng.state["weapon"] or {}).get("clip_ammo") == "ammo_t6", \
                f"{w['id']} 应可装填 t6"
            eng.state["inventory"] = [
                e for e in eng.state["inventory"] if e["id"] != "ammo_t6"
            ]

    asyncio.run(run())


def test_ammo_t1_t6_defined():
    """弹药 T1-T6 齐备：名称、伤害加成、T4 起地区 2 产出、T5/T6 缺省。"""
    cfg = get_config()
    expect = {
        "ammo_t1": ("劣质弹药", 0.90),
        "ammo_t2": ("制式弹药", 1.00),
        "ammo_t3": ("精工弹药", 1.05),
        "ammo_t4": ("军用弹药", 1.15),
        "ammo_t5": ("实验弹药", 1.25),
        "ammo_t6": ("原型弹药", 1.50),
    }
    for aid, (name, mult) in expect.items():
        a = cfg.items.get(aid)
        assert a, f"{aid} 应存在"
        assert a["name"] == name, f"{aid} 应名 {name}，实际 {a['name']}"
        assert abs(float(a.get("dmg_mult", 0)) - mult) < 1e-9, f"{aid} dmg_mult 应 {mult}"
    assert int(cfg.items["ammo_t4"].get("min_region", 0) or 0) == 2, "T4 应地区 2 产出"
    assert int(cfg.items["ammo_t5"].get("weight", 1) or 0) == 0, "T5 应缺省"
    assert int(cfg.items["ammo_t6"].get("weight", 1) or 0) == 0, "T6 应缺省"


def test_ammo_tables_region_bound():
    """弹药掉落表：T1-T3 在通用表，T4 只进军事补给表（地区 2），T5/T6 不进表。"""
    cfg = get_config()
    tables = cfg.balance["loot"]["category_tables"]
    assert set(tables["ammo"]) == {"ammo_t1", "ammo_t2", "ammo_t3"}, tables["ammo"]
    mil = tables.get("military_supplies") or []
    assert "ammo_t4" in mil, "T4 军用弹药应进军事补给表"
    for reserved in ("ammo_t5", "ammo_t6"):
        assert reserved not in mil and reserved not in tables["ammo"], \
            f"{reserved} 应为缺省不可获得"


# ---------------------------------------------------------------------------
# 2. 装填：容量钳制 / 换弹种退旧弹 / 通用性
# ---------------------------------------------------------------------------

def test_reload_clamps_to_mag_size():
    """装填量 = min(容量−现有, 背包存量)；背包只有 10 发也照装（显示 10/30）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        _equip(cfg, eng, "smg")  # mag 30
        clip = await _load(eng, "ammo_t2", 10)
        assert clip == 10, f"背包只有 10 发应全装进（10/30），实际 {clip}"
        assert st["weapon"]["clip_ammo"] == "ammo_t2"
        # 背包弹药被压进弹匣
        assert loot.count(st, "ammo_t2") == 0

    asyncio.run(run())


def test_reload_switch_type_returns_old():
    """换弹种：弹匣里旧弹退回背包，新弹装到上限。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        _equip(cfg, eng, "smg")  # mag 30
        await _load(eng, "ammo_t2", 5)
        loot.grant(cfg, st, "ammo_t3", 8)

        await eng._act_reload({"ammo": "ammo_t3"})

        assert st["weapon"]["clip_ammo"] == "ammo_t3"
        assert st["weapon"]["clip_count"] == 8
        assert loot.count(st, "ammo_t2") == 5, "旧弹应退回背包"

    asyncio.run(run())


def test_reload_full_is_noop():
    """同类补满：弹匣未满时再装填会补到上限；已满则不消耗背包弹药。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        _equip(cfg, eng, "smg")  # mag 30
        clip = await _load(eng, "ammo_t2", 10)
        assert clip == 10
        loot.grant(cfg, st, "ammo_t2", 20)

        await eng._act_reload({"ammo": "ammo_t2"})

        # 未满 → 同类补满到 30（背包 20 发全部压入）
        assert st["weapon"]["clip_count"] == 30, f"应补满到 30，实际 {st['weapon']['clip_count']}"
        assert loot.count(st, "ammo_t2") == 0, "补满消耗背包弹药"

        # 已满再装填 → no-op
        loot.grant(cfg, st, "ammo_t2", 5)
        await eng._act_reload({"ammo": "ammo_t2"})
        assert st["weapon"]["clip_count"] == 30, "已满不应再装"
        assert loot.count(st, "ammo_t2") == 5, "满弹匣不应消耗背包弹药"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. 射击：只消耗弹匣；burst 部分射击；空弹匣打不出
# ---------------------------------------------------------------------------

def test_shoot_consumes_clip_not_inventory():
    """射击只消耗弹匣内子弹；背包储备不自动进弹匣。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        e = C.make_enemy(cfg, "walker", 1)
        e["hp"] = e["hp_max"] = 200
        st["combat"] = {"enemies": [e]}
        st["noise"] = 0
        _equip(cfg, eng, "smg")
        clip = await _load(eng, "ammo_t2", 10)
        # 背包里另有 20 发储备——不应被射击直接消耗
        loot.grant(cfg, st, "ammo_t2", 20)
        _no_drops(eng)

        await eng._act_shoot({})

        after = int(st["weapon"].get("clip_count") or 0)
        spent = clip - after
        assert 3 <= spent <= 5, f"burst 应消耗 3-5 发弹匣，实际 {spent}"
        assert loot.count(st, "ammo_t2") == 20, "背包储备不应被射击消耗"

    asyncio.run(run())


def test_empty_clip_blocks_shoot():
    """空弹匣打不出，也不消耗背包弹药——提示需要装填。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        st["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)]}
        st["noise"] = 0
        _equip(cfg, eng, "pistol_m9")
        # 背包有弹药但弹匣是空的——不再自动从背包供给
        loot.grant(cfg, st, "ammo_t2", 12)

        await eng._act_shoot({})

        assert loot.count(st, "ammo_t2") == 12, "空弹匣不应消耗背包弹药"
        judged = [l for l in eng._out if "开枪命中" in l or "扑了个空" in l]
        assert not judged, f"空弹匣不应有判定: {judged}"
        assert any("弹夹空了" in l for l in eng._out), "应提示需要装填"

    asyncio.run(run())


def test_reload_in_combat_costs_a_turn():
    """战斗中装填消耗 1 回合：触发敌人反击 + 推进回合计数。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        e = C.make_enemy(cfg, "walker", 1)
        e["hp"] = e["hp_max"] = 200
        st["combat"] = {"enemies": [e]}
        st["noise"] = 0
        _equip(cfg, eng, "smg")
        loot.grant(cfg, st, "ammo_t2", 10)
        turns = st["turn"]
        st["hp"] = 100
        st["hp_max"] = 100

        await eng.act("reload", {"ammo": "ammo_t2"})

        assert int((st["weapon"]).get("clip_count") or 0) > 0, "装填应成功"
        assert st["turn"] > turns, "战斗中装填应推进回合（耗 1 回合）"

    asyncio.run(run())


def test_reload_out_of_combat_is_free():
    """非战斗装填是整理动作：不推进回合/倒计时。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = False
        st["evac_countdown"] = 30
        turns = st["turn"]
        _equip(cfg, eng, "smg")

        await eng.act("reload", {"ammo": "ammo_t2"}) if False else None
        loot.grant(cfg, st, "ammo_t2", 10)
        await eng.act("reload", {"ammo": "ammo_t2"})

        assert st["turn"] == turns, "非战斗装填不应推进回合"
        assert int(st["weapon"].get("clip_count") or 0) > 0, "装填应成功"
        assert st["evac_countdown"] == 30, "非战斗装填不应推进倒计时"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. 弹药品质：dmg_mult 按弹种生效（四舍五入）
# ---------------------------------------------------------------------------

def test_ammo_dmg_mult_scaling():
    """弹药 dmg_mult 乘在每发伤害上：同一把枪，T6 比 T2 打得更疼（四舍五入）。"""
    cfg = get_config()

    def shoot_dmg(mult: float) -> int:
        attacker = {"acc": 100, "eva": 0, "crit": 0.0, "dmg": [10, 10],
                    "strength": 0, "dmg_pct": 0.0}
        defender = {"armor": 0, "eva": 0, "taken_dmg": 0}
        rng = C.RNG(12345)
        total = 0
        for _ in range(50):
            res = C.resolve_attack(
                cfg, rng, attacker, defender,
                attacker_meta={"dmg_mult": mult},
            )
            total += res["dmg"]
        return total

    t2 = shoot_dmg(1.00)
    t6 = shoot_dmg(1.50)
    assert t6 > t2 * 1.3, f"T6(150%) 应显著高于 T2(100%)：{t6} vs {t2}"

    from app.core.rng import RNG

    r = RNG(7)
    for _ in range(20):
        res = C.resolve_attack(
            cfg, r,
            {"acc": 100, "eva": 0, "crit": 0.0, "dmg": [5, 5], "strength": 0, "dmg_pct": 0.0},
            {"armor": 0, "eva": 0, "taken_dmg": 0},
            attacker_meta={"dmg_mult": 1.05},
        )
        # 5 × 1.05 = 5.25 → 四舍五入 5；不存在小数残留
        assert res["dmg"] == int(res["dmg"]), "伤害应为整数"


def test_extended_mag_talent():
    """扩容弹匣：所有枪械弹匣容量 ×1.5（30 → 45）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["talents"] = [
            {"id": "extended_mag", "name": "扩容弹匣", "desc": "",
             "mods": {"mag_size_mult": 1.5}}
        ]
        _equip(cfg, eng, "smg")
        loot.grant(cfg, st, "ammo_t2", 45)

        await eng._act_reload({"ammo": "ammo_t2"})

        assert st["weapon"]["clip_count"] == 45, \
            f"扩容后应可装 45 发，实际 {st['weapon']['clip_count']}"

    asyncio.run(run())


def test_speed_loader_talent():
    """快速装填：战斗中装填不再触发敌人回合。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        e = C.make_enemy(cfg, "walker", 1)
        e["hp"] = e["hp_max"] = 200
        st["combat"] = {"enemies": [e]}
        st["noise"] = 0
        _equip(cfg, eng, "smg")
        st["talents"] = [
            {"id": "speed_loader", "name": "快速装填", "desc": "",
             "mods": {"reload_free": 1}}
        ]
        loot.grant(cfg, st, "ammo_t2", 10)
        # hp_max 与同步口径一致（每个行动后 _sync_hp_max 会校回基础值）
        st["hp_max"] = cfg.balance["player"]["hp"]
        st["hp"] = st["hp_max"]
        hp_before = st["hp"]
        log_before = len(st["log"])

        await eng.act("reload", {"ammo": "ammo_t2"})

        assert int(st["weapon"].get("clip_count") or 0) > 0, "装填应成功"
        assert st["hp"] == hp_before, "快速装填不应挨敌人反击"
        new_lines = st["log"][log_before:]
        assert not any("击中你" in l or "扑空了" in l for l in new_lines),             f"不应有敌人反击判定: {new_lines}"

    asyncio.run(run())


def test_ammo_not_legacy():
    """弹药 T1-T6 都不进遗物池（legacy_exclude_kinds 含 ammo 类）。"""
    cfg = get_config()
    for a in ("ammo_t1", "ammo_t2", "ammo_t3", "ammo_t4", "ammo_t5", "ammo_t6"):
        assert not cfg.legacy_allowed(a), f"{a} 不应作为遗物继承"


if __name__ == "__main__":
    test_all_ranged_weapons_have_mag()
    print("✓ 全远程武器配置弹匣容量（任意枪装任意弹）")
    test_ammo_t1_t6_defined()
    print("✓ 弹药 T1-T6 名称/伤害加成/地区绑定")
    test_ammo_tables_region_bound()
    print("✓ 弹药掉落表地区绑定（T4 军事表，T5/T6 缺省）")
    test_reload_clamps_to_mag_size()
    print("✓ 装填钳制到弹匣容量（10/30）")
    test_reload_switch_type_returns_old()
    print("✓ 换弹种旧弹退回背包")
    test_reload_full_is_noop()
    print("✓ 满弹匣装填不消耗")
    test_shoot_consumes_clip_not_inventory()
    print("✓ 射击只消耗弹匣（背包储备不自动进弹）")
    test_empty_clip_blocks_shoot()
    print("✓ 空弹匣打不出（提示装填）")
    test_reload_in_combat_costs_a_turn()
    print("✓ 战斗中装填耗 1 回合")
    test_reload_out_of_combat_is_free()
    print("✓ 非战斗装填不推进计时")
    test_extended_mag_talent()
    print("✓ 扩容弹匣天赋（容量 ×1.5）")
    test_speed_loader_talent()
    print("✓ 快速装填天赋（不触发敌人回合）")
    test_ammo_dmg_mult_scaling()
    print("✓ 弹药伤害加成按弹种生效（四舍五入）")
    test_ammo_not_legacy()
    print("✓ 弹药不进遗物池")
    print("\nP9 弹匣系统回归测试全部通过")
