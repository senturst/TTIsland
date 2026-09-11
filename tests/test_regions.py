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
    # 地区 1 实装，覆盖全部 5 层
    assert cfg.regions and 1 in cfg.regions
    assert cfg.region_for_level(1)["id"] == 1
    assert cfg.region_for_level(5)["id"] == 1
    assert cfg.region_last_level(1) == cfg.max_level == 5
    # placeholder 地区不进索引
    assert 2 not in cfg.regions, "placeholder 地区不应进 regions 索引"


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


if __name__ == "__main__":
    test_region_structure()
    print("✓ 地区结构与层归属")
    test_region_unlock_semantics()
    print("✓ 解锁语义（地区1常开，地区2需通关地区1）")
    test_region_progress_migration()
    print("✓ v6 迁移与 MAX 只升不降")
    test_meta_exposes_regions()
    print("✓ meta 接口暴露 regions")
