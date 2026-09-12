"""回归测试：避战流削弱（P6.2.5）。

背景（用户需求）：避战流太强——全程绕着战斗走也能过关。
  1. 每层楼梯口（1-4 层）必刷守门精英，必须消灭才能下楼，不可逃跑
  2. 消灭一波尸潮后噪音下降 clear_noise_cut（默认 30%，可配置）
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import loot, noise  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402

import app.core.combat as C  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


# ---------------------------------------------------------------------------
# 1. 守门精英
# ---------------------------------------------------------------------------

def test_gatekeeper_config_exists():
    """配置链完整：elite.monster 指向存在的怪物，且带 elite 标记。"""
    cfg = get_config()
    ecfg = cfg.balance["noise"]["horde"].get("elite") or {}
    mid = ecfg.get("monster")
    assert mid, "noise.horde.elite.monster 未配置"
    m = cfg.monster(mid)
    assert m.get("elite"), f"{mid} 缺少 elite: true 标记"
    assert ecfg.get("no_flee"), "no_flee 未配置（守门精英必须不可逃）"


def test_stairs_spawns_elite():
    """非 skip 层进楼梯房必刷守门精英；skip 层刷普通怪；cleared 重进不重复刷。"""
    cfg = get_config()
    ecfg = cfg.balance["noise"]["horde"]["elite"]
    skip = ecfg.get("skip_levels") or []

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        room = {"cleared": False}
        # 用 skip 之外的层验证精英（第 2 层）
        eng.state["depth"] = next(l for l in range(2, cfg.max_level + 1) if l not in skip)
        eng._enter_stairs_elite(room)
        assert st["in_combat"], "进楼梯房应立即进入精英战"
        assert eng._elite_guard_active(), "战斗中应有活着的守门精英"
        assert room.get("elite_guard"), "房间应标记 elite_guard"
        assert any(e.get("elite") for e in st["combat"]["enemies"]), \
            "敌人应带 elite 标记"
        assert any("守门" in l or "堵" in l for l in st["log"]), \
            "应有精英登场提示"

        # cleared 房重进不重复刷
        st["in_combat"] = False
        eng._enter_stairs_elite({"cleared": True})
        assert not st["in_combat"], "已清过的楼梯房不应重复刷精英"

    asyncio.run(run())


def test_skip_level_gets_normal_monster():
    """skip_levels 层（如新手第 1 层）刷普通怪：有威慑但可逃跑。"""
    cfg = get_config()
    ecfg = cfg.balance["noise"]["horde"]["elite"]
    skip = ecfg.get("skip_levels") or []
    if not skip:
        return

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        eng.state["depth"] = skip[0]
        eng._enter_stairs_elite({"cleared": False})
        assert st["in_combat"], "skip 层楼梯房应有普通战斗"
        assert not any(e.get("elite") for e in st["combat"]["enemies"]), \
            "skip 层不应出现精英标记"
        # 普通怪可逃跑（try_flee 正常走）
        st["stamina"] = 20
        st["hp"] = 40
        old = C.try_flee
        C.try_flee = lambda *a, **k: True
        try:
            await eng._act_flee({})
        finally:
            C.try_flee = old
        assert not st["in_combat"], "skip 层楼梯房普通怪应可逃跑"

    asyncio.run(run())


def test_elite_cannot_be_fled():
    """守门精英不可逃跑：逃跑动作被拒绝且战斗继续。"""
    cfg = get_config()
    ecfg = cfg.balance["noise"]["horde"]["elite"]
    skip = ecfg.get("skip_levels") or []

    async def run():
        eng = await _new_run(cfg)
        # 用 skip 之外的层（第 2 层起）确保面对的是真精英
        eng.state["depth"] = next(l for l in range(2, cfg.max_level + 1) if l not in skip)
        eng._enter_stairs_elite({"cleared": False})
        st = eng.state
        stam_before = st["stamina"]

        await eng._act_flee({})

        assert st["in_combat"], "对守门精英逃跑应被拒绝"
        assert st["stamina"] == stam_before, "被拒绝的逃跑不应扣体力"
        assert any("没地方可退" in l or "堵" in l for l in st["log"])

    asyncio.run(run())


def test_elite_must_die_before_descend():
    """精英存活时没有下楼按钮；击杀后 descend 恢复。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        # 把玩家放到楼梯房（current_room 读 level_map["current"]，两者都要设）
        stairs_idx = next(
            i for i, r in enumerate(st["level_map"]["rooms"])
            if r.get("special_kind") == "stairs"
        )
        st["level_map"]["current"] = stairs_idx
        st["room"] = {"idx": stairs_idx, "type": "special", "tpl": None,
                      "name": "楼梯", "kind": "special", "cleared": False,
                      "searched": False}
        st["combat"] = {"enemies": [C.make_enemy(cfg, "gatekeeper", 1)], "round": 0}
        st["in_combat"] = True

        # 精英活着：无 descend 按钮
        acts = eng._available_actions()
        assert not any(a["id"] == "descend" for a in acts), \
            "守门精英活着不应出现下楼按钮"
        assert any(a["id"] == "attack" for a in acts), "应有攻击选项"

        # 击杀：descend 恢复
        st["in_combat"] = False
        st["combat"]["enemies"][0]["hp"] = 0
        acts = eng._available_actions()
        assert any(a["id"] == "descend" for a in acts), "精英死后应恢复下楼按钮"

    asyncio.run(run())


def test_normal_flee_still_works():
    """普通战斗的逃跑不受影响（封锁只针对守门精英）。"""
    cfg = get_config()
    import app.core.combat as CC

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        st["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)]}
        st["stamina"] = 20
        st["hp"] = 40

        old = CC.try_flee
        CC.try_flee = lambda *a, **k: True
        try:
            await eng._act_flee({})
        finally:
            CC.try_flee = old

        assert not st["in_combat"], "普通敌人仍应可正常逃跑"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. 尸潮清波噪音削减
# ---------------------------------------------------------------------------

def test_wave_clear_cuts_noise():
    """消灭一波尸潮：噪音 ×(1−clear_noise_cut) 且尸潮平息。"""
    cfg = get_config()
    cut = float(cfg.balance["noise"]["horde"]["clear_noise_cut"])
    assert 0 < cut < 1, f"clear_noise_cut 应在 (0,1) 区间，实际 {cut}"

    st = {"noise": 9.0, "horde": True}
    noise.cut_after_wave_clear(cfg, st)
    assert st["horde"] is False, "清波应平息尸潮"
    assert abs(st["noise"] - 9.0 * (1 - cut)) < 1e-9, (
        f"噪音应削减 {cut*100}%，实际 {st['noise']}"
    )
    assert st["noise"] > 0, "削减后不应归零（要有残余威胁）"


def test_wave_clear_only_when_horde_active():
    """非尸潮期间清场不应触发削减，也不应误平息。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        st["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)], "round": 0}
        st["noise"] = 5.0
        st["horde"] = False
        st["weapon"] = {"id": "crowbar"}
        st["combat"]["enemies"][0]["hp"] = 1
        eng._acquire = lambda *a, **k: None  # 屏蔽掉落噪音污染

        await eng._act_attack({})

        assert st["noise"] == 5.0, "非尸潮清场不应动噪音"
        assert not any("潮水" in l for l in st["log"]), "不应有尸潮平息提示"

    asyncio.run(run())


def test_horde_flag_without_engagement_no_cut():
    """尸潮标记在普通战斗中途置位：清普通战斗不削减、不平息（潮还在路上）。

    修复前：清场分支只看 st["horde"] 标记——普通战斗期间枪声把标记打响，
    清掉普通敌人也白得 30% 削噪 + 平息，潮根本还没接战。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["in_combat"] = True
        st["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)], "round": 0}
        st["noise"] = 11.0  # 平息线（9）与触发线（12.75）之间的滞后带
        st["horde"] = True  # 潮在路上，但这场是普通战斗（敌人无潮兵标记）
        st["weapon"] = {"id": "crowbar"}
        st["combat"]["enemies"][0]["hp"] = 1
        eng._acquire = lambda *a, **k: None  # 屏蔽掉落噪音污染

        await eng._act_attack({})

        assert st["horde"] is True, "没打退潮兵不应平息尸潮"
        assert st["noise"] == 11.0, "清普通战斗不应削减噪音"
        assert not any("潮水" in l for l in st["log"]), "不应有尸潮平息提示"

    asyncio.run(run())


def test_clearing_real_horde_cuts_noise():
    """清掉**含潮兵**的战斗：削减噪音 + 平息尸潮（走 _enemy_round 真实出口）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["talents"] = []  # 隔离随机天赋（破潮者的额外削噪会改变期望值）
        st["in_combat"] = True
        horde_enemy = C.make_enemy(cfg, "walker", 1)
        horde_enemy["horde"] = True  # spawn_horde 打的同一标记
        horde_enemy["hp"] = 1
        st["combat"] = {"enemies": [horde_enemy], "round": 0}
        st["noise"] = 11.0  # 滞后带内（平息 9 < 11 < 触发 12.75）
        st["horde"] = True
        st["weapon"] = {"id": "crowbar"}
        eng._acquire = lambda *a, **k: None

        await eng._act_attack({})
        # 17% 未命中偶发：没打死就再打（清场条件是潮兵全灭）
        for _ in range(4):
            if all(e["hp"] <= 0 for e in st["combat"]["enemies"]):
                break
            st["in_combat"] = True
            await eng._act_attack({})

        cut = float(cfg.balance["noise"]["horde"]["clear_noise_cut"])
        assert st["horde"] is False, "清掉潮兵应平息尸潮"
        assert abs(st["noise"] - 11.0 * (1 - cut)) < 1e-9, "清掉潮兵应削减噪音"

    asyncio.run(run())


def test_region2_noise_cap_30():
    """P8 地区 2 远程主场：噪音上限 30（用户拍板）。

    add 封顶 30；尸潮触发线/平息线/衰减按上限同比例放大（×3）——
    改的是"可以更吵"的预算，不是尸潮频率。地区 1 行为不变。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["depth"] = 6
        st["noise"] = 28.0
        noise.add(cfg, st, 5)
        assert st["noise"] == 30.0, f"地区 2 噪音上限应 30，实际 {st['noise']}"
        st["noise"] = 5.0
        noise.add(cfg, st, 3)
        assert st["noise"] == 8.0, "普通叠加不受地区影响"

    asyncio.run(run())

    # 触发线 = 地区噪音上限 × 85%：地区 1 = 15×0.85 = 12.75，地区 2 = 30×0.85 = 25.5
    thr2 = noise.noise_max(cfg, 6) * float(cfg.balance["noise"]["horde"]["threshold_pct"])
    st2 = {"depth": 6, "noise": thr2, "horde": False}
    assert noise.check_horde(cfg, st2) is True, "地区 2 触发线应为上限 ×85%"
    assert st2["horde"] is True
    st3 = {"depth": 6, "noise": thr2 - 0.1, "horde": False}
    assert noise.check_horde(cfg, st3) is False, "地区 2 触发线下不应触发"
    thr1 = noise.noise_max(cfg, 1) * float(cfg.balance["noise"]["horde"]["threshold_pct"])
    st4 = {"depth": 1, "noise": thr1, "horde": False}
    assert noise.check_horde(cfg, st4) is True, "地区 1 触发线应为 15×85% = 12.75"

    # 上限/缩放查询：地区 1 = 15（P8 后上调）、地区 2 = 30
    assert noise.noise_max(cfg, 6) == 30.0
    assert noise.noise_max(cfg, 1) == 15.0
    assert abs(noise.region_scale(cfg, 6) - 3.0) < 1e-9
    assert abs(noise.region_scale(cfg, 1) - 1.5) < 1e-9


def test_soldier_burst_volley():
    """P8 变异士兵扫射：多发独立命中，汇总日志带命中数；玩家倒下即停。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        st["depth"] = 6
        st["in_combat"] = True
        soldier = C.make_enemy(cfg, "soldier", 6)
        soldier["hp"] = 999  # 打不死，专注看 volley
        st["combat"] = {"enemies": [soldier], "round": 0}
        st["weapon"] = {"id": "crowbar"}
        st["armor"] = {"id": "military_vest", "durability": 55}
        st["hp"] = 200
        st["hp_max"] = 200
        eng._acquire = lambda *a, **k: None

        await eng._enemy_round()

        assert any("扫射" in l and "发命中" in l for l in st["log"]), \
            f"应有扫射汇总日志：{st['log'][-3:]}"
        assert st["hp"] < 200 or "0/2 发命中" in st["log"][-1] or "0/3 发命中" in st["log"][-1]

    asyncio.run(run())


if __name__ == "__main__":
    test_region2_noise_cap_30()
    print("✓ 地区 2 噪音上限 30（触发线×3）")
    test_soldier_burst_volley()
    print("✓ 变异士兵扫射 volley")
    test_gatekeeper_config_exists()
    print("✓ 守门者配置完整")
    test_stairs_spawns_elite()
    print("✓ 楼梯必刷精英")
    test_skip_level_gets_normal_monster()
    print("✓ skip 层刷普通怪")
    test_elite_cannot_be_fled()
    print("✓ 精英不可逃跑")
    test_elite_must_die_before_descend()
    print("✓ 精英必须消灭才能下楼")
    test_normal_flee_still_works()
    print("✓ 普通逃跑不受影响")
    test_wave_clear_cuts_noise()
    print("✓ 清波削减噪音")
    test_wave_clear_only_when_horde_active()
    print("✓ 非尸潮清场不削减")
    test_horde_flag_without_engagement_no_cut()
    print("✓ 潮未接战清普通战斗不削减")
    test_clearing_real_horde_cuts_noise()
    print("✓ 清掉真潮兵削减+平息")
    print("\n避战流削弱回归测试全部通过")
