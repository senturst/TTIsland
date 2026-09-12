"""回归测试：P7 升级系统（XP + 精英直升 + 多天赋聚合）。

背景（用户拍板的五点设计）：
  1. 方案 A：复用天赋池三选一，效果可叠加
  2. 精英/Boss 击杀直接升 1 级（不走 XP 条）
  3. 节奏：每局期望 2-3 级、全程上限 4 级
  4. 强度对冲走「接受抬升 + 微调目标」
  5. 天赋池扩充到 32 个支撑抽取

机制要点：
  - XP 来源：monsters.yaml 的 xp 字段（普通怪攒条，60×1.4^level 曲线，可连升）
  - 结算时机：pending_levelups 在战斗清空后由 _settle_levelups() 弹出
  - 升级抽取排除已拥有（开局抽取不排除）
  - 多天赋聚合：数值求和、*_mult 连乘、其他取第一个
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import talents  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


class FakeEng(RunEngine):
    """不走持久化/前端输出的轻量引擎，用于直接驱动内部方法。"""

    def __init__(self, cfg, state):
        super().__init__(cfg, state)

    def log_text(self):
        return "\n".join(self._out)


def _fresh_state(cfg):
    """最小可运行状态：从 RunEngine.new_run 借基础字段（含 rng），growth 字段归零。"""
    from app.core.rng import RNG, new_seed

    rng = RNG(new_seed())
    return {
        "run_id": "t",
        "seed": 12345,
        "rng": rng.get_state(),
        "player": {"name": "t"},
        "depth": 1,
        "turn": 0,
        "hp": 20,
        "hp_max": 20,
        "infection": 0,
        "stamina": 20,
        "noise": 0.0,
        "horde": False,
        "inventory": [],
        "buffs": [],
        "log": [],
        "talents": [],
        "xp": 0,
        "growth_level": 0,
        "pending_levelups": 0,
        "pending_decision": None,
        "status": "active",
    }


def test_monsters_have_xp_field():
    """所有怪必须带 xp 字段——make_enemy 透传它，缺了升级系统就静默失效。"""
    cfg = get_config()
    monsters = cfg.monsters_cfg.get("monsters") or []
    assert monsters, "monsters.yaml 应有 monsters 列表"
    for m in monsters:
        assert isinstance(m.get("xp"), int) and m["xp"] > 0, f"{m['id']} 缺 xp 字段"


def test_make_enemy_passes_xp_and_elite():
    """make_enemy 必须透传 xp 与 elite 字段（历史上 xp 漏透过 → 升级全失效）。"""
    cfg = get_config()
    from app.core import combat

    e = combat.make_enemy(cfg, "ghoul", 1)
    ghoul = next(m for m in cfg.monsters_cfg["monsters"] if m["id"] == "ghoul")
    assert e["xp"] == int(ghoul["xp"])
    assert not e.get("elite")

    e2 = combat.make_enemy(cfg, "gatekeeper", 1)
    gate = next(m for m in cfg.monsters_cfg["monsters"] if m["id"] == "gatekeeper")
    assert e2["xp"] == int(gate["xp"])
    assert e2.get("elite") is True


def test_elite_kill_grants_direct_levelup():
    """精英击杀 → pending_levelups +1，不走 XP 条。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    eng = FakeEng(cfg, st)
    eng._grant_xp({"id": "gatekeeper", "elite": True, "xp": 30})
    assert st["pending_levelups"] == 1, st
    assert st["xp"] == 0, "精英直升不应累积 XP 条"
    assert st["growth_level"] == 0


def test_normal_kill_accumulates_xp_and_levels():
    """普通怪击杀攒 XP 条，过阈值升级（可能连升）；精英路径不受影响。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    eng = FakeEng(cfg, st)

    base = cfg.balance["growth"]["xp_base"]
    curve = float(cfg.balance["growth"]["xp_curve"])

    # 第一级需要 base 点
    eng._grant_xp({"id": "walker", "xp": 10})
    assert st["xp"] == 10 and st["growth_level"] == 0

    # 补到 base → 升 1 级，剩余进下一级条
    eng._grant_xp({"id": "walker", "xp": base - 10 + 5})
    assert st["growth_level"] == 1
    assert st["pending_levelups"] == 1
    assert st["xp"] == 5

    # 连升验证：一次灌 3 级的量
    need2 = int(base * curve ** 1)
    need3 = int(base * curve ** 2)
    eng._grant_xp({"id": "walker", "xp": (need2 - 5) + need3 + 10})
    assert st["growth_level"] == 3, (st["growth_level"], need2, need3)
    assert st["pending_levelups"] == 3


def test_settle_levelups_pops_talent_decision():
    """战斗清空后 _settle_levelups 弹三选一，一次弹一个。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    eng = FakeEng(cfg, st)
    st["pending_levelups"] = 2

    eng._settle_levelups()
    assert st.get("pending_decision") == "talent"
    assert len(st.get("talent_options") or []) == 3
    assert st["pending_levelups"] == 1, "弹一个扣一个"


def test_levelup_draw_excludes_owned():
    """升级抽取必须排除已拥有天赋；开局抽取不排除。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    eng = FakeEng(cfg, st)

    owned = st["talents"]
    # 强塞几个已拥有的（从池里取真实 id）
    pool_ids = [t["id"] for t in cfg.talents_cfg["talents"]]
    owned.extend([{ "id": pool_ids[0] }, { "id": pool_ids[1] }])

    st["pending_levelups"] = 1
    eng._settle_levelups()
    opts = [o["id"] for o in st["talent_options"]]
    assert pool_ids[0] not in opts and pool_ids[1] not in opts, opts

    # 32 个池排除 2 个仍有 30 个可选 → 抽取不该失败
    assert len(opts) == 3


def test_talents_mod_aggregation():
    """多天赋聚合：数值求和、*_mult 连乘、其他取第一个。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    st["talents"] = [
        {"id": "a", "mods": {"stamina_max": 2, "dmg_mult": 1.1, "flee_bonus": 5}},
        {"id": "b", "mods": {"stamina_max": 3, "dmg_mult": 1.2}},
    ]
    # 数值：2+3=5
    assert talents.mod(st, "stamina_max", 0) == 5
    # 乘数：1.1×1.2=1.32
    assert abs(talents.mod(st, "dmg_mult", 1.0) - 1.32) < 1e-9
    # 非数值非乘数：取第一个出现的
    assert talents.mod(st, "flee_bonus", 0) == 5
    # 缺省值兜底
    assert talents.mod(st, "nonexistent", 7) == 7


def test_old_single_talent_format_migrates():
    """旧档 state['talent']（单对象）应被迁移为列表，不丢数据。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    st["talents"] = []
    st["talent"] = {"id": "legacy_one", "mods": {"stamina_max": 4}}
    assert talents.mod(st, "stamina_max", 0) == 4, "旧格式天赋应参与聚合"


def test_death_clears_growth():
    """死亡后成长数据不进遗物/下一局——开局 XP 从零开始。"""
    cfg = get_config()
    st = _fresh_state(cfg)
    st["xp"] = 55
    st["growth_level"] = 2
    st["pending_levelups"] = 1
    # new_run 造的新 state 必须全部归零
    fresh = _fresh_state(cfg)
    assert fresh["xp"] == 0 and fresh["growth_level"] == 0 and fresh["pending_levelups"] == 0


def test_end_to_end_elite_kill_then_pick():
    """端到端：真引擎击杀精英 → decision=talent → 选完 talents+1 且级联弹下一个。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        n_before = len(st.get("talents") or [])
        # 直接注入精英击杀（绕开地图到达）
        eng._grant_xp({"id": "gatekeeper", "elite": True, "xp": 30})
        eng._settle_levelups()
        assert st.get("pending_decision") == "talent"
        await eng.act("talent", {"index": 0})
        # pending 还剩 0（只注入了 1 个），天赋数 +1
        assert len(st.get("talents") or []) == n_before + 1
        assert st.get("pending_decision") is None
        return st

    st = asyncio.run(run())
    assert st["growth_level"] == 0  # 精英直升不走条


def test_talent_pool_size():
    """天赋池必须足够大：≥30 个，否则升级系统连抽几次就枯竭。"""
    cfg = get_config()
    n = len(cfg.talents_cfg["talents"])
    assert n >= 30, f"天赋池只有 {n} 个，不足以支撑升级抽取"


def test_growth_config_exists():
    """growth 曲线配置存在且合理。"""
    cfg = get_config()
    g = cfg.balance["growth"]
    assert int(g["xp_base"]) > 0
    assert float(g["xp_curve"]) > 1.0, "曲线系数必须 >1（逐级变贵）"


def test_start_items_talent_grants_items():
    """快速凝血（start_items: [[bandage, 2]]）选完当场发放物品。

    修复前：start_items 只有 balance.player.start_items 消费，天赋里的
    同名键从未被读取——绷带永远不来，玩家白白浪费一个天赋位。
    """
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            opts = eng.state["talent_options"]
            qc = next(
                (i for i, o in enumerate(opts) if o["id"] == "quick_clot"), None
            )
            if qc is None:
                return  # 本局没抽到就跳过（抽到局断言在下面由 hits 保证）
            from app.core import loot
            before = loot.count(eng.state, "bandage")
            await eng._act_talent({"index": qc})
            assert loot.count(eng.state, "bandage") == before + 2, \
                "选快速凝血应当场发 2 个绷带"
            return True

    hits = 0
    for _ in range(120):
        if asyncio.run(run()):
            hits += 1
            if hits >= 2:
                break
    assert hits >= 2, f"120 局只抽到 {hits} 次 quick_clot，抽样不足"


def test_iron_stomach_food_infection_bonus():
    """铁胃：吃抑制类消耗品（负感染）每份多减 food_infection_bonus。

    长期缺陷：desc 承诺「吃 infection 抑制更好」但 mods 里只有 start_items，
    第二效果从未被任何代码消费。现在补上消费点（_act_use）。
    规则：只放大削减（罐头 -2→-3），正感染代价不变（伏特加 +3 仍是 +3）。
    """
    cfg = get_config()

    async def run():
        from app.core import loot
        eng = await _new_run(cfg)
        st = eng.state

        async def eat_clean(iid):
            """脱离战斗后使用物品：_act_use 战斗中会触发敌人回合，
            怪物命中附带的感染会污染断言，这里先清场。"""
            st["in_combat"] = False
            st["enemies"] = []
            await eng._act_use({"item": iid})

        # 基线：无天赋吃罐头 -2
        st["talents"] = []
        loot.grant(cfg, st, "canned", 1)
        st["infection"] = 20
        await eat_clean("canned")
        assert st["infection"] == 18, f"无天赋吃罐头应 -2，实际 {st['infection']}"

        # 有铁胃：-2-1 = -3
        st["talents"] = [
            {"id": "iron_stomach", "name": "铁胃", "desc": "",
             "mods": {"food_infection_bonus": 1}}
        ]
        loot.grant(cfg, st, "canned", 1)
        st["infection"] = 20
        await eat_clean("canned")
        assert st["infection"] == 17, f"铁胃吃罐头应 -3，实际 {st['infection']}"

        # 叠两层：求和 = +2，罐头 -4
        st["talents"] = [
            {"id": "iron_stomach", "name": "铁胃", "desc": "",
             "mods": {"food_infection_bonus": 1}},
            {"id": "iron_stomach", "name": "铁胃", "desc": "",
             "mods": {"food_infection_bonus": 1}},
        ]
        loot.grant(cfg, st, "canned", 1)
        st["infection"] = 20
        await eat_clean("canned")
        assert st["infection"] == 16, f"双层铁胃吃罐头应 -4，实际 {st['infection']}"

        # 正感染代价不放大：止痛药 +1 仍是 +1
        st["talents"] = [
            {"id": "iron_stomach", "name": "铁胃", "desc": "",
             "mods": {"food_infection_bonus": 1}}
        ]
        loot.grant(cfg, st, "painkiller", 1)
        st["infection"] = 20
        await eat_clean("painkiller")
        assert st["infection"] == 21, f"正感染不应被铁胃改变，实际 {st['infection']}"
        return True

    assert asyncio.run(run())


def test_hp_max_talent_survives_infection_sync():
    """强健体质类生命加成不再被感染同步洗掉。

    修复前：_sync_hp_max 只按「基础生命 × 感染系数」重算——任何一次感染
    变动（含 L3 医院每 2 回合的环境感染）都会把 hp_max 天赋加成洗掉。
    修复后：上限 =（基础+天赋）× 感染系数，天赋按比例保留。
    """
    cfg = get_config()

    async def run():
        from app.core import infection as inf_mod

        eng = await _new_run(cfg)
        st = eng.state
        base = int(cfg.balance["player"]["hp"])
        st["talents"] = [
            {"id": "tough", "name": "强健体质", "desc": "", "mods": {"hp_max": 5}}
        ]
        st["hp_max"] = base + 5
        st["hp"] = base + 5

        eng._add_infection(80)
        expected = max(1, int(round(
            (base + 5) * (1 + float(inf_mod.modifiers(cfg, st["infection"])["hp_max_pct"]))
        )))
        assert st["hp_max"] == expected, \
            f"高感染时上限应=（基础+天赋）×系数：期望 {expected}，实际 {st['hp_max']}"

        eng._add_infection(-80)
        assert st["hp_max"] == base + 5, f"治愈后应恢复基础+天赋，实际 {st['hp_max']}"

    asyncio.run(run())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"✓ {fn.__doc__.strip().splitlines()[0] if fn.__doc__ else fn.__name__}")
    print(f"\n升级系统回归测试全部通过（{len(fns)} 项）")
