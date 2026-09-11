"""回归测试：挂机自动清理（6 小时无活动的 run 置为 abandoned）。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
os.environ["DB_PATH"] = "data/test_stale_cleanup.db"  # 独立测试库，避免污染真实数据
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.db.repo as repo  # noqa: E402
from app.db import migrate  # noqa: E402
from app.db.pool import connect  # noqa: E402

migrate.apply_migrations()


def _mk_run(player_id: str, **kw) -> str:
    """建一个临时玩家 + 其 active run（runs.player_id 有外键约束）。"""
    repo.players.get_or_create(player_id)
    state = {"depth": kw.get("depth", 1), "log": []}
    return repo.runs.create(player_id, seed=1, state=state)


def _db_updated_at(run_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT updated_at FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row["updated_at"] if row else 0


def _set_updated_at(run_id: str, ts: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (ts, run_id))
        conn.commit()


def test_fresh_run_not_expired():
    """新 run 的 updated_at 已初始化，且不会被立即清掉。"""
    pid = f"test-stale-{time.time_ns()}"
    rid = _mk_run(pid)
    assert _db_updated_at(rid) > 0, "create 应初始化 updated_at"

    n = repo.runs.expire_stale()
    with connect() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "active", "刚创建的 run 不应被清掉"


def test_old_run_expired_after_six_hours():
    """updated_at 超过 6 小时的 active run 应被置为 abandoned。"""
    pid = f"test-stale-old-{time.time_ns()}"
    rid = _mk_run(pid)
    six_h_ago = int(time.time()) - 6 * 3600 - 60
    _set_updated_at(rid, six_h_ago)

    repo.runs.expire_stale()
    with connect() as conn:
        row = conn.execute(
            "SELECT status, death_cause, ended_at FROM runs WHERE id = ?", (rid,)
        ).fetchone()
    assert row["status"] == "abandoned", "超时 run 应被置为 abandoned"
    assert row["death_cause"] == "与应急频段失去了联系"
    assert row["ended_at"], "应写入结束时间"


def test_finished_runs_never_touched():
    """已结束（dead/escaped 等）的 run 不受清理影响。"""
    pid = f"test-stale-done-{time.time_ns()}"
    rid = _mk_run(pid)
    repo.runs.finish(rid, "dead", 123, "被撕碎")
    _set_updated_at(rid, int(time.time()) - 24 * 3600)

    repo.runs.expire_stale()
    with connect() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "dead", "已结束的 run 状态不应被覆盖"


def test_save_refreshes_updated_at():
    """save() 应刷新 updated_at——这是"活动"的定义。"""
    pid = f"test-stale-save-{time.time_ns()}"
    rid = _mk_run(pid)
    _set_updated_at(rid, int(time.time()) - 12 * 3600)

    repo.runs.save(rid, {"depth": 2, "log": []})
    assert _db_updated_at(rid) > int(time.time()) - 60, "save 后 updated_at 应是现在"


if __name__ == "__main__":
    test_fresh_run_not_expired()
    print("ok 新 run 不会被误清")
    test_old_run_expired_after_six_hours()
    print("ok 超 6 小时的 run 被清理")
    test_finished_runs_never_touched()
    print("ok 已结束 run 不受影响")
    test_save_refreshes_updated_at()
    print("ok save 刷新活动时间")
    print("\n挂机清理回归测试全部通过")
