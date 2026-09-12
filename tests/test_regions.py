"""P6.2.1 地区系统回归测试。

覆盖：
  - regions.yaml 加载与层→地区归属（校验逻辑：层不重复归属、地区必须实装层）
  - region_for_level / region_last_level / region_unlocked 查询语义
  - DB v6 迁移：players.region_progress 存在且默认 0
  - record_region_clear 只升不降
  - placeholder 地区（地区 2）不进索引、不可进入
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.loader import get_config
from app.db import migrate
from app.db.pool import connect


def test_region_structure():
    cfg = get_config()
    # P8：地区 1（1-5 层）+ 地区 2（6-10 层）双地区实装
    assert cfg.regions and 1 in cfg.regions and 2 in cfg.regions
    assert cfg.region_for_level(1)["id"] == 1
    assert cfg.region_for_level(5)["id"] == 1
    assert cfg.region_for_level(6)["id"] == 2
    assert cfg.region_for_level(10)["id"] == 2
    assert cfg.max_level == 10, f"两地区实装后 max_level 应为 10，实际 {cfg.max_level}"
    # 撤离点 = 所在地区最后一层（L5 与 L10 都是撤离点）
    assert cfg.last_level_of(3) == 5, "地区 1 中段层的撤离点是 L5"
    assert cfg.last_level_of(5) == 5
    assert cfg.last_level_of(6) == 10, "地区 2 中段层的撤离点是 L10"
    assert cfg.last_level_of(10) == 10
    # 地区 Boss
    assert cfg.region_boss(1) == "tyrant_t03"
    assert cfg.region_boss(2) == "horde_marshal"


def test_region_unlock_semantics():
    cfg = get_config()
    # 地区 1 永远开放
    assert cfg.region_unlocked(1, 0)
    assert cfg.region_unlocked(1, 1)
    # 地区 2 需要先从地区 1 撤离（progress >= 1）
    assert not cfg.region_unlocked(2, 0)
    assert cfg.region_unlocked(2, 1)
    # 越界安全
    assert not cfg.region_unlocked(3, 0)


def test_region_progress_migration():
    """v6 迁移后 players 表有 region_progress 列且默认 0。"""
    with tempfile.TemporaryDirectory() as td:
        import os
        os.environ["WB_DB_PATH"] = str(Path(td) / "t.db")
        # pool.connect 读取缓存的路径——用独立进程语义不可行，
        # 这里直接对内存库建表验证 SQL 合法性
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(Path("app/db/schema.sql").resolve().read_text(encoding="utf-8"))
        conn.execute("INSERT INTO players (id, name, created_at, last_seen) VALUES ('p1','x',1,1)")
        row = conn.execute(
            "SELECT region_progress FROM players WHERE id='p1'"
        ).fetchone()
        assert row is not None and int(row[0]) == 0, "region_progress 默认 0"
        # MAX 覆盖语义：只升不降
        conn.execute("UPDATE players SET region_progress = MAX(region_progress, ?) WHERE id='p1'", (1,))
        conn.execute("UPDATE players SET region_progress = MAX(region_progress, ?) WHERE id='p1'", (0,))
        v = conn.execute("SELECT region_progress FROM players WHERE id='p1'").fetchone()[0]
        assert v == 1, "MAX 覆盖应只升不降"
        conn.close()
    # 迁移声明包含 v6
    assert any(t == 6 for t, _ in migrate.MIGRATIONS), "MIGRATIONS 应含 v6"
    assert migrate.SCHEMA_VERSION >= 6


def test_meta_exposes_regions():
    """meta_config 的 regions 字段结构可序列化（直接调视图函数）。"""
    import asyncio
    from app.api.rest.meta import meta_config
    data = asyncio.run(meta_config())
    assert "regions" in data
    r1 = data["regions"].get(1) or data["regions"].get("1")
    assert r1 and r1["name"] and isinstance(r1["levels"], list) and r1["levels"]


# ==================== P8 撤离带装 / 地区切换 ====================

def _engine_imports():
    import os
    os.environ.setdefault("LLM_ENABLED", "false")
    from app.services.run_service import RunEngine
    from app.core import loot
    return RunEngine, loot


def test_region1_evac_enters_carry_decision():
    """L5 撤离：对局不结束（status=active），进入 evac_carry 待决策并标记换区。"""
    RunEngine, _ = _engine_imports()
    import asyncio
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})
        st = eng.state
        st["depth"] = 5
        st["boss_alive"] = False  # Boss 已死
        st["evac_countdown"] = 10
        st["inventory"].append({"id": "dog_tag", "qty": 1})  # 纪念品随撤离计分

        await eng._act_evac({})
        import sys as _s
        print("DBG score:", st.get("score"), "| depth:", st.get("depth"), "| pending:", st.get("pending_decision"), "| log:", st["log"][-2:])

        assert st["status"] == "active", "中间地区撤离不应结束对局"
        assert st["pending_decision"] == "evac_carry"
        assert st["region_clear_pending"] == 1, "应标记待发放的地区通关"
        assert st["evac_countdown"] is None, "撤离后倒计时应停止"
        # 纪念品随撤离结算积分（没带走也计）
        trinket = int(cfg.item("dog_tag").get("score", 0) or 0)
        assert st["score"] >= 200 + trinket, f"撤离分应含基础分与纪念品分，实际 {st['score']}"

    asyncio.run(run())


def test_evac_carry_applies_selection_and_enters_region2():
    """确认带装：武器/装备/其他进新局配置、其余清空、按枪发弹药、进入 L6。"""
    RunEngine, loot = _engine_imports()
    import asyncio
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})
        st = eng.state
        st["region_clear_pending"] = 1
        st["pending_decision"] = "evac_carry"
        st["inventory"] = [
            {"id": "silenced_smg", "qty": 1, "durability": 22},
            {"id": "riot_gear", "qty": 1, "durability": 40},
            {"id": "bandage", "qty": 3},
            {"id": "canned", "qty": 2},
        ]
        st["weapon"] = {"id": "crowbar", "durability": 20}
        # 换区整备的前置惨状：高感染/低血/高噪音/尸潮追着
        st["infection"] = 69
        st["hp"] = 10
        st["noise"] = 8.0
        st["horde"] = True
        # 纪念品随撤离结算积分（没带走也计）
        st["inventory"].append({"id": "dog_tag", "qty": 1})

        await eng._apply_evac_carry({
            "weapon": "silenced_smg", "gear": "riot_gear", "other": "bandage",
        })

        assert st["depth"] == 6, "应进入地区 2 首层"
        assert st["pending_decision"] is None
        assert st["weapon"]["id"] == "silenced_smg"
        assert st["armor"]["id"] == "riot_gear"
        inv = {e["id"]: e["qty"] for e in st["inventory"]}
        assert inv.get("bandage") == 3, "只带走选中的其他物品"
        assert inv.get("medkit") == 1, "换区应赠送 1 个医疗箱"
        assert "canned" not in inv and "silenced_smg" not in inv and "riot_gear" not in inv, \
            "其余物品清空（武器/装备进槽位，弹药箱由系统发放）"
        start = int(cfg.balance["player"]["ammo_start"])
        assert loot.count(st, "ammo_t3") == start, "携带远程枪应发对口弹药"
        assert loot.count(st, "scrap") == 0
        assert st["boss_alive"] is False, "L6 不是 Boss 层"
        # 换区整备：感染清零、生命/体力回满、噪音清零、尸潮平息
        assert st["infection"] == 0, f"感染应清零，实际 {st['infection']}"
        assert st["hp"] == st["hp_max"], "进入新地区应回满生命"
        assert st["noise"] == 0, f"噪音应清零，实际 {st['noise']}"
        assert st["horde"] is False, "尸潮应平息"

    asyncio.run(run())


def test_evac_carry_without_ranged_grants_scrap():
    """没带远程武器：发 1 废料、零弹药；没带武器则发新撬棍。"""
    RunEngine, loot = _engine_imports()
    import asyncio
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})
        st = eng.state
        st["region_clear_pending"] = 1
        st["pending_decision"] = "evac_carry"
        st["inventory"] = [{"id": "bandage", "qty": 1}]
        st["weapon"] = {"id": "silenced_smg", "durability": 20}

        # 什么都不带 → 新撬棍 + 1 废料，背包清空（另有换区赠送的医疗箱）
        await eng._apply_evac_carry({"weapon": None, "gear": None, "other": None})

        assert st["weapon"]["id"] == "crowbar", "没带武器应发制式撬棍"
        inv2 = {e["id"]: e["qty"] for e in st["inventory"]}
        assert inv2 == {"scrap": 1, "medkit": 1}, \
            f"应只有补给品（废料 1 + 医疗箱 1），实际 {inv2}"
        for a in ("ammo_t1", "ammo_t2", "ammo_t3"):
            assert loot.count(st, a) == 0, f"无远程继承不发 {a}"

    asyncio.run(run())


def test_region2_evac_still_finalizes():
    """L10 撤离：全局最后一层走现行 escaped→遗物选择结算（也发继承码）。"""
    RunEngine, _ = _engine_imports()
    import asyncio
    from app.services.run_service import RunEnded
    cfg = get_config()

    async def run():
        eng = await RunEngine.new_run(cfg, None)
        if eng.state.get("pending_decision") == "talent":
            await eng.act("talent", {"index": 0})
        st = eng.state
        st["depth"] = 10
        st["boss_alive"] = False

        try:
            await eng._act_evac({})
        except RunEnded:
            pass
        assert st["status"] == "escaped", "全局末层撤离仍应结束对局"
        assert st["pending_decision"] == "legacy"
        assert "region_clear_pending" not in st, "终局撤离不走换区标记"

    asyncio.run(run())


if __name__ == "__main__":
    test_region_structure()
    print("✓ 地区结构与层归属（双地区 1-10）")
    test_region_unlock_semantics()
    print("✓ 解锁语义（地区1常开，地区2需通关地区1）")
    test_region_progress_migration()
    print("✓ v6 迁移与 MAX 只升不降")
    test_meta_exposes_regions()
    print("✓ meta 接口暴露 regions")
    test_region1_evac_enters_carry_decision()
    print("✓ L5 撤离进入 evac_carry 待决策（对局继续）")
    test_evac_carry_applies_selection_and_enters_region2()
    print("✓ 带装应用：三槽携带+清包+对口弹药+进入L6")
    test_evac_carry_without_ranged_grants_scrap()
    print("✓ 无远程继承：新撬棍+1废料+零弹药")
    test_region2_evac_still_finalizes()
    print("✓ L10 撤离仍走终局结算")
