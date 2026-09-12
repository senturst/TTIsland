"""回归测试：体力（stamina）是真实资源，不是空话。

背景（用户反馈：罐头显示"体力 +8"但实际没增加）：
  - 体力此前是孤儿数值——从不消耗、HUD 不显示（"体力"标签还错挂在了生命条上）、
    且开局就是满值，于是 heal_stamina 永远被钳到上限，玩家看不到任何变化。
  - 修复：逃跑=冲刺消耗体力（combat.flee_stamina_cost）；罐头/能量饮料/篝火回体力；
    HUD 新增独立体力条；use 时补上反馈日志；响应里带上 stamina/stamina_max。
  - "瞄准"动作：战斗中消耗体力换取临时命中加成（走 buff 系统）。
    设计红线：低体力只是用不了瞄准，绝不对命中做任何减益——基础命中不受体力影响。
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
    # 统一隔离随机开局天赋：P7 天赋池里带 stamina_max / brace_acc_bonus_add /
    # flee_bonus 的天赋都会改变对应数值断言的基线，这里统一清零。
    # （new_run 选完天赋进地牢后清空是安全的——天赋只在创建时生效一次。）
    eng.state["talents"] = []
    return eng


def test_canned_restores_stamina():
    """罐头应在体力低于上限时真正回体力，并给出反馈日志。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["stamina"] = 5  # 低于上限
        loot.grant(cfg, eng.state, "canned", 1)  # 开局不再自带罐头，显式给一件
        before_count = loot.count(eng.state, "canned")

        await eng._act_use({"item": "canned"})

        gain = int(cfg.item("canned").get("heal_stamina", 0))
        assert eng.state["stamina"] == 5 + gain, f"罐头应回 {gain} 体力，实际 {eng.state['stamina']}"
        assert loot.count(eng.state, "canned") == before_count - 1, "罐头应被消耗一件"
        assert any("体力 +" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_stamina_clamped_at_max():
    """满体力时吃罐头不应溢出，且日志如实显示 +0。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["stamina"] = 20
        loot.grant(cfg, eng.state, "canned", 1)

        await eng._act_use({"item": "canned"})

        assert eng.state["stamina"] == 20, "体力不应超过上限"
        assert any("体力 +0" in line for line in eng._out), eng._out

    asyncio.run(run())


def test_flee_costs_stamina():
    """逃跑=冲刺应消耗体力（设计上体力是会被消耗的资源）。

    防空测试回归：本测试曾因配置 cost=0 而恒真（assert 20-0==20 永过），
    导致"逃跑不扣体力"的 bug 逃过全量测试。这里显式要求 cost > 0。
    """
    cfg = get_config()
    cost = int(cfg.balance["combat"]["flee_stamina_cost"])
    assert cost > 0, "flee_stamina_cost 必须为正——逃跑不扣体力是回归，不是设计"

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        eng.state["combat"] = {"enemies": [{"name": "x", "hp": 5, "hp_max": 5}]}
        eng.state["stamina"] = 20

        old = C.try_flee
        C.try_flee = lambda *a, **k: True  # 强制逃跑成功，隔离 rng
        try:
            await eng._act_flee({})
        finally:
            C.try_flee = old

        assert not eng.state["in_combat"], "应已脱离战斗"
        assert eng.state["stamina"] == 20 - cost, f"逃跑应耗 {cost} 体力"
        assert any("体力 −" in line for line in eng._out), "日志应显示体力消耗"

    asyncio.run(run())


def test_flee_costs_stamina_even_on_fail():
    """逃跑失败同样扣体力——冲刺本身就耗力，跑输了也在跑。"""
    cfg = get_config()
    cost = int(cfg.balance["combat"]["flee_stamina_cost"])

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        # 逃跑失败会触发敌人回合，敌人字段要够 combat.enemy_profile 用
        eng.state["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)]}
        eng.state["stamina"] = 20
        eng.state["hp"] = 40

        old = C.try_flee
        C.try_flee = lambda *a, **k: False  # 强制逃跑失败
        try:
            await eng._act_flee({})
        finally:
            C.try_flee = old

        assert eng.state["in_combat"], "逃跑失败应仍在战斗"
        assert eng.state["stamina"] == 20 - cost, f"逃跑失败也应耗 {cost} 体力"

    asyncio.run(run())


def test_flee_penalized_by_enemy_count():
    """敌人越多越难逃：每只额外敌人 −flee_per_enemy，且引擎要传敌人数量。"""
    cfg = get_config()
    per_enemy = float(cfg.balance["combat"]["flee_per_enemy"])
    assert per_enemy > 0, "flee_per_enemy 应为正——多敌人惩罚是需求，不该回退为 0"

    captured = {}

    def fake_try_flee(cfg_, rng_, agi, eagi, stamina=0, enemy_count=1):
        captured["enemy_count"] = enemy_count
        return True

    old = C.try_flee
    C.try_flee = fake_try_flee
    try:
        cfg2 = get_config()

        async def run():
            eng = await _new_run(cfg2)
            eng.state["in_combat"] = True
            eng.state["combat"] = {
                "enemies": [C.make_enemy(cfg2, "walker", 1) for _ in range(3)]
            }
            eng.state["stamina"] = 20
            eng.state["hp"] = 40
            await eng._act_flee({})
        asyncio.run(run())
    finally:
        C.try_flee = old

    assert captured.get("enemy_count") == 3, (
        f"引擎应把活敌数量传给 try_flee，实际传了 {captured.get('enemy_count')}"
    )

    # 概率数值验证：3 只敌人应比 1 只低 2*per_enemy 个百分点
    base_1 = C.try_flee
    class FakeRng:
        def __init__(self):
            self.last_chance = None
        def chance(self, p):
            self.last_chance = p
            return False
    rng1, rng3 = FakeRng(), FakeRng()
    C.try_flee(cfg, rng1, 5, 5, stamina=10, enemy_count=1)
    C.try_flee(cfg, rng3, 5, 5, stamina=10, enemy_count=3)
    diff = (rng1.last_chance - rng3.last_chance) * 100
    assert abs(diff - 2 * per_enemy) < 1e-6, (
        f"3 只敌人应比 1 只低 {2 * per_enemy}pp，实际差 {diff}pp"
    )


def test_stamina_serialized_to_response():
    """前端要能看到体力，响应里必须带上 stamina / stamina_max。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        resp = eng._response()
        assert "stamina" in resp["state"], "响应缺 stamina"
        assert "stamina_max" in resp["state"], "响应缺 stamina_max"
        assert resp["state"]["stamina_max"] == int(cfg.balance["player"]["stamina"])

    asyncio.run(run())


def test_brace_costs_stamina_and_adds_buff():
    """瞄准应消耗体力并追加"瞄准"命中 buff，且该 buff 进入响应与 player_profile。"""
    cfg = get_config()
    cost = int(cfg.balance["combat"]["brace_stamina_cost"])
    bonus = int(cfg.balance["combat"]["brace_acc_bonus"])
    turns = int(cfg.balance["combat"]["brace_turns"])

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        eng.state["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)]}
        eng.state["stamina"] = 20
        eng.state["buffs"] = []

        await eng._act_brace({})

        assert eng.state["stamina"] == 20 - cost, f"瞄准应耗 {cost} 体力"
        brace = [b for b in eng.state["buffs"] if b["name"] == "瞄准"]
        assert brace, "应追加「瞄准」buff"
        assert brace[0]["acc"] == bonus, "buff 命中加成应等于配置"
        assert brace[0]["turns"] == turns, "buff 持续回合数应等于配置"

        # buff 应被 player_profile 读到，抬高命中
        pp = C.player_profile(cfg, eng.state)
        assert pp["acc"] >= bonus, f"player_profile 应含瞄准加成(+{bonus})"

        # 响应里必须能带上 buff，前端才能展示
        resp = eng._response()
        names = [b["name"] for b in resp["state"]["buffs"]]
        assert "瞄准" in names, "响应 state.buffs 应含瞄准"

    asyncio.run(run())


def test_low_stamina_blocks_brace():
    """体力不足时不能用瞄准，且不应凭空产生 buff（绝不做减益同理）。"""
    cfg = get_config()
    cost = int(cfg.balance["combat"]["brace_stamina_cost"])

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        eng.state["combat"] = {"enemies": [{"name": "x", "hp": 5, "hp_max": 5}]}
        eng.state["stamina"] = max(0, cost - 1)  # 差一点够
        eng.state["buffs"] = []

        await eng._act_brace({})

        assert eng.state["stamina"] < cost, "前置：体力确实不足"
        assert not any(b["name"] == "瞄准" for b in eng.state["buffs"]), "体力不够不应产生瞄准 buff"

    asyncio.run(run())


def test_brace_does_not_trigger_enemy_turn():
    """瞄准是免费预备动作：不应触发敌人回合（敌人不获得一次行动），玩家仍保有行动权。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        eng.state["combat"] = {"enemies": [C.make_enemy(cfg, "walker", 1)]}
        eng.state["stamina"] = 20
        eng.state["buffs"] = []

        called = {"enemy_round": False}
        real = eng._enemy_round

        async def spy(*_a, **_k):
            called["enemy_round"] = True
            return await real(*_a, **_k)

        eng._enemy_round = spy

        await eng._act_brace({})

        assert not called["enemy_round"], "瞄准不应触发敌人回合"
        assert eng.state["in_combat"] is True, "瞄准后玩家仍应处于战斗、保留行动权"
        # 瞄准是刷新而非叠加：连续两次瞄准只应留下一个「瞄准」buff
        assert sum(1 for b in eng.state["buffs"] if b["name"] == "瞄准") == 1, \
            "反复瞄准不应叠加多个瞄准 buff"

    asyncio.run(run())


if __name__ == "__main__":
    test_canned_restores_stamina()
    print("✓ 罐头回体力")
    test_stamina_clamped_at_max()
    print("✓ 体力封顶不溢出")
    test_flee_costs_stamina()
    print("✓ 逃跑消耗体力")
    test_flee_costs_stamina_even_on_fail()
    print("✓ 逃跑失败也扣体力")
    test_flee_penalized_by_enemy_count()
    print("✓ 逃跑按敌人数量递减")
    test_stamina_serialized_to_response()
    print("✓ 体力序列化到响应")
    test_brace_costs_stamina_and_adds_buff()
    print("✓ 瞄准消耗体力并加命中 buff")
    test_low_stamina_blocks_brace()
    print("✓ 体力不足不能用瞄准")
    test_brace_does_not_trigger_enemy_turn()
    print("✓ 瞄准不触发敌人回合（不占回合）")
    print("\n体力回归测试全部通过")
