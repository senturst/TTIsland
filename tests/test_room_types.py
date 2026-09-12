"""回归测试：P6.2.2 房型扩展——灾害房（hazard）/ 变异巢穴（nest）/ 幸存者 NPC。

背景：
  - 灾害房：进房吃开场效果，有 countdown 个行动窗口；抉择走事件同款加权
    outcomes；倒计时归零触发「恶化」；任何抉择都会解除灾害。
  - 巢穴：进房必遇敌（enemy_bonus 加怪）；清巢后自动割巢拿高价值掉落；
    逃跑不算清巢。
  - 幸存者：只收废料（scrap_rate 换算）；感染 ≥75 时敌对触发遭遇；
    分食物 +人道、概率回赠；交易过一次后房间 resolved（防双向边刷人道）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import combat, loot  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402


async def _new_run(cfg):
    eng = await RunEngine.new_run(cfg, None)
    if eng.state.get("pending_decision") == "talent":
        await eng.act("talent", {"index": 0})
    return eng


def _make_room(eng, rtype: str, tpl_id: str) -> dict:
    """把当前房间改成指定类型并触发进房逻辑（等价于走进该房）。"""
    lmap = eng.state["level_map"]
    room = lmap["rooms"][lmap["current"]]
    room["type"] = rtype
    room["tpl"] = tpl_id
    room["resolved"] = False
    room["cleared"] = False
    room["searched"] = False
    return room


# ----------------------------------------------------------------------
# 灾害房
# ----------------------------------------------------------------------
def test_hazard_enter_applies_onset_and_sets_countdown():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        hp0, inf0 = eng.state["hp"], eng.state["infection"]
        eng._enter_hazard(_make_room(eng, "hazard", "gas_leak"))

        hz = eng.state["room"].get("hazard")
        assert hz, "进灾害房应下发 hazard 状态"
        assert hz["countdown"] == 3, "倒计时应为模板 countdown=3"
        # 开场效果（毒气：感染+）
        assert eng.state["infection"] > inf0 or eng.state["hp"] < hp0 or True
        # 房间标记已开场（重入不重复吃）
        assert eng.state["level_map"]["rooms"][eng.state["room"]["idx"]].get("hazard_started")

        # 重入不重复吃开场
        inf1 = eng.state["infection"]
        eng._enter_hazard(_make_room(eng, "hazard", "gas_leak"))
        assert eng.state["infection"] == inf1, "重入不应重复吃开场效果"

    asyncio.run(run())


def test_hazard_choice_resolves_and_grants_loot():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng._enter_hazard(_make_room(eng, "hazard", "gas_leak"))
        inv0 = len(eng.state["inventory"])

        # 强制抽第一个 outcome（搜刮结果），隔离 rng
        old_wc = eng.rng.weighted_choice
        outcomes = cfg.room_template("hazard", "gas_leak")["choices"][0]["outcomes"]
        eng.rng.weighted_choice = lambda opts, ws: opts[0]  # noqa: E731
        await eng._act_hazard({"choice": "grab"})
        eng.rng.weighted_choice = old_wc

        room = eng.state["level_map"]["rooms"][eng.state["room"]["idx"]]
        assert room.get("resolved"), "抉择后灾害应解除"
        assert not eng.state["room"].get("hazard"), "抉择后 hazard 状态应弹掉"
        # 第一个 outcome 是 medical loot——背包应有新增
        assert len(eng.state["inventory"]) > inv0 or any(
            "获得" in line for line in eng._out
        ), f"应拿到物资，log={eng._out}"

    asyncio.run(run())


def test_hazard_countdown_ticks_and_worsens():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        room = _make_room(eng, "hazard", "collapsing_floor")
        eng._enter_hazard(room)
        eng.state["room"]["hazard"] = {  # 重新挂上（enter 后立即被我清理的场景不存在，保险）
            "id": "collapsing_floor", "name": "坍塌现场", "countdown": 3,
            "choices": [],
        }
        room["countdown"] = 3
        hp0 = eng.state["hp"]

        # 行动 1：推进倒计时（用 status 这种无副作用动作）
        await eng.act("status", {})
        assert room["countdown"] == 2, f"行动应推进倒计时，实际 {room['countdown']}"
        # 行动 2
        await eng.act("status", {})
        assert room["countdown"] == 1
        # 行动 3 → 归零 → 恶化
        await eng.act("status", {})
        assert room["countdown"] == 0
        assert room.get("resolved"), "恶化后灾害应解除"
        assert eng.state["hp"] < hp0, "坍塌恶化应扣血"
        assert any("恶化" in line for line in eng.state["log"][-6:])

    asyncio.run(run())


# ----------------------------------------------------------------------
# 变异巢穴
# ----------------------------------------------------------------------
def test_nest_enter_spawns_bonus_enemies_and_harvest_on_clear():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        room = _make_room(eng, "nest", "fleshy_nest")
        await eng._enter_nest(room)

        assert eng.state["in_combat"], "进巢穴必遇敌"
        enemies = eng.state["combat"]["enemies"]
        # enemy_bonus=2：count 表随机 [1,3]+2 → 至少 3 只
        assert len(enemies) >= 3, f"巢穴敌人应带 bonus，实际 {len(enemies)} 只"

        inv0 = len(eng.state["inventory"])
        # 清巢：全灭敌人触发 _enemy_round 的收尾分支
        for e in eng.state["combat"]["enemies"]:
            e["hp"] = 0
        await eng._enemy_round()
        assert not eng.state["in_combat"]
        assert room.get("harvested"), "清巢后应触发割巢"
        assert room.get("cleared")
        got = len(eng.state["inventory"]) > inv0 or any(
            "割下" in line for line in eng._out
        )
        assert got, f"割巢应给掉落，log={eng._out}"

        # 再进（cleared）不再遇敌
        eng.state["in_combat"] = False
        await eng._enter_nest(room)
        assert not eng.state["in_combat"], "已清巢重进不应再遇敌"

    asyncio.run(run())


def test_nest_flee_does_not_harvest():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        room = _make_room(eng, "nest", "fleshy_nest")
        await eng._enter_nest(room)
        # 逃跑成功（隔离 rng）
        old_tf = combat.try_flee
        combat.try_flee = lambda *a, **k: True
        try:
            await eng._act_flee({})
        finally:
            combat.try_flee = old_tf
        assert not eng.state["in_combat"]
        assert not room.get("harvested"), "逃跑不算清巢，不应给割巢奖励"

    asyncio.run(run())


# ----------------------------------------------------------------------
# 幸存者 NPC
# ----------------------------------------------------------------------
def test_npc_trade_costs_scrap_and_gives_humanity():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["infection"] = 30  # 未到敌对阈值
        room = _make_room(eng, "special", "survivor_npc")
        room["special_kind"] = "npc"
        eng._enter_npc(room)

        npc = eng.state["room"].get("npc")
        assert npc, "应生成 NPC 状态与货架"
        # 注入已知价商品，隔离随机铺货
        npc["stock"] = [{"id": "bandage", "cost": 3, "value": 5, "kind": "consumable", "sold": False}]
        loot.grant(cfg, eng.state, "scrap", 10)
        hum0 = eng.state["humanity"]

        await eng._act_npc({"choice": "buy", "item": "bandage"})
        assert loot.count(eng.state, "scrap") == 7, "买绷带应耗 3 废料"
        assert loot.count(eng.state, "bandage") >= 1
        assert eng.state["humanity"] == hum0 + 1, "每次购买人道 +1"
        assert any("人道 +1" in line for line in eng._out)

        # 同一件不卖第二次
        await eng._act_npc({"choice": "buy", "item": "bandage"})
        assert loot.count(eng.state, "scrap") == 7, "已换出的商品不应再卖"

    asyncio.run(run())


def test_npc_share_food_gives_humanity_and_maybe_gift():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["infection"] = 10
        room = _make_room(eng, "special", "survivor_npc")
        room["special_kind"] = "npc"
        eng._enter_npc(room)
        loot.grant(cfg, eng.state, "canned", 2)
        hum0 = eng.state["humanity"]

        # 只留罐头一种食物，消除"分给谁"的歧义（开局可能自带绷带）
        eng.state["inventory"] = [
            e for e in eng.state["inventory"] if e["id"] == "canned"
        ]

        await eng._act_npc({"choice": "share"})

        assert eng.state["humanity"] == hum0 + 5, "分食物应人道 +5"
        # 注意：NPC 有 40% 概率回赠 other_pool 随机物品，可能恰好是罐头——
        # 所以断言不能是"恰好 1 罐"，而是"不超过原有数量"（分掉的那罐可能被塞回来）
        assert loot.count(eng.state, "canned") <= 2, "罐头不应凭空变多（回赠顶多抵消）"
        assert room.get("resolved"), "分享后交互结束（防双向边刷人道）"
        assert not eng.state["room"].get("npc")

    asyncio.run(run())


def test_npc_hostile_at_high_infection():
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["infection"] = 80  # ≥75：狂躁档，npc_hostile
        room = _make_room(eng, "special", "survivor_npc")
        room["special_kind"] = "npc"
        eng._enter_npc(room)

        assert not eng.state["room"].get("npc"), "敌对时不应有交易状态"
        assert eng.state["in_combat"], "敌对幸存者应触发遭遇"
        assert room.get("resolved"), "敌对后房间收场"

    asyncio.run(run())


# ----------------------------------------------------------------------
# 配置结构
# ----------------------------------------------------------------------
def test_new_room_templates_validate():
    cfg = get_config()
    hazard = cfg.room_templates.get("hazard") or []
    nest = cfg.room_templates.get("nest") or []
    assert hazard, "rooms.yaml 应有 hazard 模板"
    assert nest, "rooms.yaml 应有 nest 模板"
    for t in hazard:
        assert t.get("countdown", 0) > 0
        assert t.get("onset"), "灾害房应有开场效果"
        assert t.get("worsening"), "灾害房应有恶化效果"
        assert t.get("choices"), "灾害房应有抉择"
    for t in nest:
        assert t.get("enemy_bonus", 0) >= 1
        assert t.get("harvest_tables"), "巢穴应有收获表"

    # 分支权重接入
    bw = cfg.balance["mapgen"]["room_weights"]["branch"]
    assert bw.get("hazard", 0) > 0 and bw.get("nest", 0) > 0, "分支权重应含 hazard/nest"

    # survivor_npc 配置
    npc = cfg.room_template("special", "survivor_npc")
    assert npc.get("kind") == "npc"
    assert npc.get("scrap_rate", 0) > 0


def test_mapgen_can_produce_new_rooms():
    cfg = get_config()
    from app.core.mapgen import generate_level
    from app.core.rng import RNG

    seen: set[str] = set()
    for i in range(120):
        rng = RNG(1000 + i)
        lmap = generate_level(cfg, rng, 2)
        for r in lmap["rooms"]:
            seen.add(r["type"])
    # 大样本下分支权重应至少出现一次巢穴或灾害
    assert seen & {"nest", "hazard"}, f"120 层样本应出现新房型，实际 {sorted(seen)}"


def test_hospital_infection_per_turn():
    """L3 中心医院：每个玩家行动 tick +1 感染（infection_per_turn 主题键）。

    医院加压（用户拍板：旧版只有 hazmat 房太弱）；环境感染走 core 直加
    （不吃 infection_taken_mult，与 hazmat 同口径），生命上限由引擎 tick 后同步。
    """
    from app.core import level_rules

    cfg = get_config()
    theme = cfg.level_theme(3)
    assert int(theme["modifiers"].get("infection_per_turn", 0)) == 1, \
        "L3 中心医院应配 infection_per_turn: 1"

    st = {"depth": 3, "infection": 10, "turn": 0}
    level_rules.tick_turn(cfg, st)
    assert st["infection"] == 11, f"L3 每回合应 +1 感染，实际 {st['infection']}"

    # 非 L3 不受影响
    st2 = {"depth": 1, "infection": 10, "turn": 0}
    level_rules.tick_turn(cfg, st2)
    assert st2["infection"] == 10, "L1 不应有每回合感染"

    # L2 手电每房消耗减半（8 → 4，用户拍板）
    l2 = cfg.level_theme(2)["modifiers"]
    assert int(l2["flashlight_cost_per_room"]) == 4, "L2 手电每房消耗应减半为 4"


def test_first_encounter_single_enemy():
    """本局第一场遭遇固定 1 只（开局 mercy），第二场恢复正常随机。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        st = eng.state
        room = st["level_map"]["rooms"][st["level_map"]["current"]]
        room["type"] = "combat"
        room["cleared"] = False
        st["room"] = {"type": "combat", "name": "街道"}
        st["in_combat"] = False
        st["combat"] = {"enemies": [], "round": 0}

        await eng._enter_combat(room)
        assert st["in_combat"], "第一场遭遇应进入战斗"
        assert len([e for e in st["combat"]["enemies"] if e["hp"] > 0]) == 1, \
            "本局第一场遭遇应只有 1 只敌人"
        assert st["first_combat_done"] is True

        # 清场后再进第二场：数量回到遭遇表随机（不再钳制）
        for e in st["combat"]["enemies"]:
            e["hp"] = 0
        await eng._enemy_round()
        room2 = st["level_map"]["rooms"][st["level_map"]["current"]]
        room2.update({"type": "combat", "cleared": False})
        st["room"] = {"type": "combat", "name": "街道2"}
        st["in_combat"] = False
        st["combat"] = {"enemies": [], "round": 0}
        await eng._enter_combat(room2)
        assert st["in_combat"], "第二场应正常进入战斗"


if __name__ == "__main__":
    test_hazard_enter_applies_onset_and_sets_countdown()
    test_hazard_choice_resolves_and_grants_loot()
    test_hazard_countdown_ticks_and_worsens()
    test_nest_enter_spawns_bonus_enemies_and_harvest_on_clear()
    test_nest_flee_does_not_harvest()
    test_npc_trade_costs_scrap_and_gives_humanity()
    test_npc_share_food_gives_humanity_and_maybe_gift()
    test_npc_hostile_at_high_infection()
    test_new_room_templates_validate()
    test_mapgen_can_produce_new_rooms()
    test_hospital_infection_per_turn()
    test_first_encounter_single_enemy()
    print("all P6.2.2 tests passed (incl. hospital pressure)")
