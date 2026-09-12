"""回归测试：远程武器重做（弹药按 tier 统合 + 冲锋枪连射）。

背景（用户需求）：远程武器偏弱——噪音大、伤害小、费子弹、子弹难获得。
  1. 弹药按 tier 统合：t1 枪用 ammo_t1，t2 用 ammo_t2，t3 用 ammo_t3，
     取代旧的 ammo_pistol / ammo_shotgun / ammo_smg 三套弹种
  2. 冲锋枪（smg）一次攻击随机射出 3-5 发，每发独立 roll 命中与伤害；
     目标倒下后剩余发数转向下一个敌人；噪音按一次攻击算一次
  3. burst 弹药不足时有多少打多少（至少 1 发）；单发武器不足则打不出
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


def _combat_with(cfg, enemy_ids):
    """构造一场战斗：按怪物 id 生成完整敌人档案。"""
    return {
        "enemies": [C.make_enemy(cfg, mid, 1) for mid in enemy_ids],
    }


def _equip(cfg, eng, weapon_id):
    """把武器塞进玩家手里（绕过装备流程，直接指定）。"""
    eng.state["weapon"] = {"id": weapon_id}


def _no_drops(eng):
    """屏蔽一切获得物品的途径（击杀掉落会污染弹药计数断言）。"""
    eng._acquire = lambda *a, **k: None


# ---------------------------------------------------------------------------
# 1. 弹药统合：全武器按 tier 引用对应弹药
# ---------------------------------------------------------------------------

def test_all_ranged_weapons_use_tier_ammo():
    """每把远程武器的 ammo_type 必须存在、是弹药、且与其 tier 对应。"""
    cfg = get_config()
    tier2ammo = {1: "ammo_t1", 2: "ammo_t2", 3: "ammo_t3"}
    ranged = [w for w in cfg.items_cfg["weapons"] if w.get("kind") == "ranged"]
    assert ranged, "items.yaml 里应有远程武器"
    for w in ranged:
        at = w.get("ammo_type")
        assert at in cfg.items, f"{w['id']} 的弹药 {at} 不存在"
        assert cfg.item_kind(at) == "ammo", f"{w['id']} 的 {at} 不是弹药"
        expect = tier2ammo[int(w["tier"])]
        assert at == expect, f"{w['id']}(t{w['tier']}) 应用 {expect}，实际 {at}"


def test_old_ammo_ids_gone():
    """旧三弹种应彻底移除——残留在掉落表/商店池里会在校验期就该被发现。"""
    cfg = get_config()
    for old in ("ammo_pistol", "ammo_shotgun", "ammo_smg"):
        assert old not in cfg.items, f"旧弹种 {old} 仍注册在 items 里"
    tables = cfg.balance["loot"]["category_tables"]["ammo"]
    assert set(tables) == {"ammo_t1", "ammo_t2", "ammo_t3"}, tables
    assert "ammo_t1" in cfg.balance["merchant"]["other_pool"]


# ---------------------------------------------------------------------------
# 2. burst：随机 3-5 发、每发独立结算、噪音按一次算
# ---------------------------------------------------------------------------

def test_smg_burst_exists():
    """冲锋枪应有 burst [3,5] 配置。"""
    cfg = get_config()
    smg = cfg.item("smg")
    assert smg.get("burst") == [3, 5], smg.get("burst")
    # 消音冲锋枪同样有 burst，但代价是 tier 抬到 3——改烧稀缺的重弹药（t3）
    # 是它的主要代价，稀有度也控制在霰弹枪一档
    silenced = cfg.item("silenced_smg")
    assert silenced.get("burst") == [3, 5], silenced.get("burst")
    assert silenced.get("tier") == 3, silenced.get("tier")
    assert silenced.get("ammo_type") == "ammo_t3", silenced.get("ammo_type")


def test_burst_shoots_multiple_shots():
    """连射一次应消耗随机 3-5 发，且每个目标各受多次独立判定。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        # 血牛敌人：保证 3-5 发打不死，判定数与发数一致（清场 break 会吞判定）
        e1 = C.make_enemy(cfg, "walker", 1)
        e1["hp"] = e1["hp_max"] = 200
        e2 = C.make_enemy(cfg, "runner", 1)
        e2["hp"] = e2["hp_max"] = 200
        eng.state["combat"] = {"enemies": [e1, e2]}
        eng.state["noise"] = 0
        _equip(cfg, eng, "smg")
        before = loot.count(eng.state, "ammo_t2")  # 开局自带弹药，不能硬编码
        loot.grant(cfg, eng.state, "ammo_t2", 10)
        before += 10
        _no_drops(eng)

        await eng._act_shoot({})

        # 每只敌人至少挨了一次有效判定（日志里应有命中或落空的记录）
        hit_lines = [l for l in eng._out if "开枪命中" in l or "扑了个空" in l]
        assert len(hit_lines) >= 3, f"连射至少 3 发判定，实际 {len(hit_lines)} 条: {hit_lines}"
        # 弹药被消耗：打出去的数量在 3-5 之间
        spent = before - loot.count(eng.state, "ammo_t2")
        assert 3 <= spent <= 5, f"应消耗 3-5 发，实际 {spent}"

    asyncio.run(run())


def test_burst_noise_added_once():
    """扫射的攻击噪音只算一次——不论打了几发（击杀噪音另算，不在此列）。"""
    cfg = get_config()
    from app.core import talents

    async def run():
        eng = await _new_run(cfg)
        src = int(cfg.balance["noise"]["sources"]["gunshot"])
        # 随机天赋可能抽到「轻步」（主动噪音 -1）：期望值要跟着算
        expect = max(0.0, src + float(talents.mod(eng.state, "noise_add_delta", 0)))
        eng.state["in_combat"] = True
        # 两只高血量敌人：保证不会全被打死，把 gun_kill 击杀噪音排除在外
        e1 = C.make_enemy(cfg, "walker", 1)
        e1["hp"] = e1["hp_max"] = 200
        e2 = C.make_enemy(cfg, "runner", 1)
        e2["hp"] = e2["hp_max"] = 200
        eng.state["combat"] = {"enemies": [e1, e2]}
        eng.state["in_combat"] = True
        eng.state["noise"] = 0
        _equip(cfg, eng, "smg")
        loot.grant(cfg, eng.state, "ammo_t2", 30)
        _no_drops(eng)

        await eng._act_shoot({})

        # 噪音恰好加了一次来源值（而非每发一次）；击杀噪音未触发
        assert eng.state["noise"] == expect, (
            f"噪音应恰好 +{expect}（一次攻击一次动静），实际 +{eng.state['noise']}"
        )

    asyncio.run(run())


def test_burst_shifts_target_after_kill():
    """当前目标倒下后，剩余发数应转向下一个敌人（不等敌人回合）。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        # 第一只只剩 1 血：第一发必死，后续发数应转向第二只
        e1 = C.make_enemy(cfg, "walker", 1)
        e1["hp"] = 1
        # 第二只用血牛：smg 暴击(1.8x)一发可秒 12 血的普通 walker，
        # 秒杀后 alive 为空 break 会吞掉剩余发数的判定，断言就会偶发失败
        e2 = C.make_enemy(cfg, "walker", 1)
        e2["hp"] = e2["hp_max"] = 200
        eng.state["in_combat"] = True
        eng.state["combat"] = {"enemies": [e1, e2]}
        eng.state["noise"] = 0
        _equip(cfg, eng, "smg")
        loot.grant(cfg, eng.state, "ammo_t2", 10)
        _no_drops(eng)

        await eng._act_shoot({})

        # 两只敌人都该被判定过（除非全部落空——日志验证判定总数 ≥3）
        judged = [l for l in eng._out if "开枪命中" in l or "扑了个空" in l]
        assert len(judged) >= 3, f"应至少 3 次判定，实际 {len(judged)}"
        # 第二只至少被点名过一次（日志含其名字；血牛打不死，但必然掉血）
        assert e2["hp"] < e2["hp_max"] or any("行尸" in l for l in judged[1:]), (
            "第二只敌人应承接转向的发数"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. 弹药不足：burst 部分射击 vs 单发打不出
# ---------------------------------------------------------------------------

def test_burst_partial_shots_when_low_ammo():
    """弹药只剩 2 发时，连射应打出去 2 发而不是拒绝开火。

    用「先清空再给 2 发」控制存量——开局自带 12 发，不能硬编码计数。
    """
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        # 血牛：2 发打不死，判定数与发数一致（暴击秒杀会吞判定）
        e = C.make_enemy(cfg, "walker", 1)
        e["hp"] = e["hp_max"] = 200
        eng.state["combat"] = {"enemies": [e]}
        eng.state["noise"] = 0
        _equip(cfg, eng, "smg")
        # 清空开局弹药，精确控制只剩 2 发
        eng.state["inventory"] = [
            e for e in eng.state["inventory"] if e["id"] != "ammo_t2"
        ]
        loot.grant(cfg, eng.state, "ammo_t2", 2)
        _no_drops(eng)

        await eng._act_shoot({})

        assert loot.count(eng.state, "ammo_t2") == 0, "仅剩的弹药应全部打出去"
        judged = [l for l in eng._out if "开枪命中" in l or "扑了个空" in l]
        assert len(judged) == 2, f"应恰好 2 次判定，实际 {len(judged)}"

    asyncio.run(run())


def test_single_shot_blocked_without_ammo():
    """非 burst 武器弹药不足时打不出，也不该有判定发生。"""
    cfg = get_config()

    async def run():
        eng = await _new_run(cfg)
        eng.state["in_combat"] = True
        eng.state["combat"] = _combat_with(cfg, ["walker"])
        eng.state["noise"] = 0
        _equip(cfg, eng, "pistol_m9")  # 单发 t2 枪
        eng.state["inventory"] = [
            e for e in eng.state["inventory"] if e["id"] != "ammo_t2"
        ]

        await eng._act_shoot({})

        assert loot.count(eng.state, "ammo_t2") == 0
        judged = [l for l in eng._out if "开枪命中" in l or "扑了个空" in l]
        assert not judged, f"没弹药不应有任何判定: {judged}"
        assert any("不够了" in l for l in eng._out), "应提示弹药不足"
        # 噪音也不该产生——没开枪哪来的动静
        assert eng.state["noise"] == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. 遗物安全：弹药不进遗物池（legacy_exclude_kinds 含 ammo）
# ---------------------------------------------------------------------------

def test_ammo_not_legacy():
    cfg = get_config()
    for a in ("ammo_t1", "ammo_t2", "ammo_t3"):
        assert not cfg.legacy_allowed(a), f"{a} 不应作为遗物继承"


if __name__ == "__main__":
    test_all_ranged_weapons_use_tier_ammo()
    print("✓ 全武器按 tier 引用弹药")
    test_old_ammo_ids_gone()
    print("✓ 旧弹种已清除")
    test_smg_burst_exists()
    print("✓ 冲锋枪 burst 配置")
    test_burst_shoots_multiple_shots()
    print("✓ 连射多发独立判定")
    test_burst_noise_added_once()
    print("✓ 扫射噪音只算一次")
    test_burst_shifts_target_after_kill()
    print("✓ 目标倒下发数转向")
    test_burst_partial_shots_when_low_ammo()
    print("✓ 弹药不足部分射击")
    test_single_shot_blocked_without_ammo()
    print("✓ 单发武器没弹药打不出")
    test_ammo_not_legacy()
    print("✓ 弹药不进遗物池")
    print("\n远程武器重做回归测试全部通过")
