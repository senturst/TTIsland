"""run 数据访问。

state_json 整存整取：一次 action 的所有状态变更在一个事务里写完，
事务内不做任何网络/AI 调用。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..pool import connect, transaction


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def create(player_id: str, seed: int, state: dict) -> str:
    run_id = new_run_id()
    now = int(time.time())
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO runs (id, player_id, seed, status, depth, turn, score, kills,
                                  hp, hp_max, infection, noise, ammo, flashlight,
                                  state_json, started_at)
                VALUES (?,?,?, 'active',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    player_id,
                    seed,
                    state.get("depth", 1),
                    state.get("turn", 0),
                    state.get("score", 0),
                    state.get("kills", 0),
                    state.get("hp", 0),
                    state.get("hp_max", 0),
                    state.get("infection", 0),
                    float(state.get("noise", 0.0)),
                    state.get("ammo_total", 0),
                    state.get("flashlight"),
                    json.dumps(state, ensure_ascii=False),
                    now,
                ),
            )
    return run_id


def get_active(player_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE player_id = ? AND status = 'active' "
            "ORDER BY started_at DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        return dict(row) if row else None


def list_active() -> list[dict[str, Any]]:
    """所有存活在玩的 run（status='active'），用于管理后台查看在线玩家。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE status = 'active' ORDER BY started_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def load_state(row: dict) -> dict:
    return json.loads(row["state_json"])


def save(run_id: str, state: dict, **extra: Any) -> None:
    """写回状态。extra 可覆盖 runs 表的镜像列（depth/score/hp 等）。"""
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                """
                UPDATE runs SET
                    depth = ?, turn = ?, score = ?, kills = ?,
                    hp = ?, hp_max = ?, infection = ?, noise = ?,
                    ammo = ?, flashlight = ?, state_json = ?
                WHERE id = ?
                """,
                (
                    state.get("depth", 1),
                    state.get("turn", 0),
                    state.get("score", 0),
                    state.get("kills", 0),
                    state.get("hp", 0),
                    state.get("hp_max", 0),
                    state.get("infection", 0),
                    float(state.get("noise", 0.0)),
                    state.get("ammo_total", 0),
                    state.get("flashlight"),
                    json.dumps(state, ensure_ascii=False),
                    run_id,
                ),
            )
            if extra:
                cols = ", ".join(f"{k} = ?" for k in extra)
                conn.execute(
                    f"UPDATE runs SET {cols} WHERE id = ?", (*extra.values(), run_id)
                )


def finish(run_id: str, status: str, score: int, death_cause: str | None = None) -> None:
    now = int(time.time())
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = ?, score = MAX(score, ?), ended_at = ?, "
                "death_cause = ? WHERE id = ?",
                (status, score, now, death_cause, run_id),
            )


def leaderboard(by: str = "score", limit: int = 20) -> list[dict[str, Any]]:
    """三榜通用：by ∈ {score, depth, humanity}。

    直接读 players 表（规模 <1000 人时全表排序 <5ms，无需独立榜单表）。
    每榜只取在该维度上有成绩、且来过至少一局的玩家。
    """
    col = {"score": "best_score", "depth": "best_depth", "humanity": "humanity"}.get(by, "best_score")
    with connect() as conn:
        rows = conn.execute(
            f"SELECT name, best_score, best_depth, total_runs, escapes, humanity "
            f"FROM players WHERE total_runs > 0 AND {col} > 0 "
            f"ORDER BY {col} DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["_by"] = by
    return out


__all__ = [
    "create", "get_active", "get", "load_state", "save", "finish",
    "leaderboard", "new_run_id",
]
