"""墓碑数据访问。

首批（单机闭环）阶段：死亡即写碑，但尚不注入他人地图。
P4 会在此之上加 pick_candidate() 做加权抽取。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..pool import connect, transaction


def create(
    *,
    run_id: str,
    player_id: str,
    player_name: str,
    level: int,
    killer_id: str | None = None,
    gear: list[dict] | None = None,
    infection: int = 0,
    epitaph: str | None = None,
    is_plagued: bool = False,
) -> str:
    grave_id = uuid.uuid4().hex[:16]
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO graves (id, run_id, player_id, player_name, level, killer_id,
                                    gear_json, infection, epitaph, is_plagued, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    grave_id, run_id, player_id, player_name, level, killer_id,
                    json.dumps(gear or [], ensure_ascii=False),
                    infection, epitaph, 1 if is_plagued else 0,
                    int(time.time()),
                ),
            )
    return grave_id


def set_epitaph(grave_id: str, epitaph: str) -> None:
    with connect() as conn:
        with transaction(conn):
            conn.execute("UPDATE graves SET epitaph = ? WHERE id = ?", (epitaph, grave_id))


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT player_name, level, infection, epitaph, is_plagued, created_at "
            "FROM graves ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def of_player(player_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graves WHERE player_id = ? ORDER BY created_at DESC LIMIT ?",
            (player_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_all() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM graves").fetchone()[0])


__all__ = ["create", "set_epitaph", "recent", "of_player", "count_all"]
