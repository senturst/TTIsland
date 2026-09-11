"""私人回执：墓碑被别人摸走时，原主人离线也能收到。

世界播报走 SSE（公开、即时）；而"你的遗体被某人发现了"是**只给原主人的私信**，
不能广播。它走这张表：摸尸体时写一条，原主人下次上线（boot/resume）拉取并标记已读。
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..pool import connect, transaction


def add(player_id: str, kind: str, body: str, data: dict | None = None) -> int:
    with connect() as conn:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO notifications (player_id, kind, body, data_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (player_id, kind, body, json.dumps(data or {}, ensure_ascii=False),
                 int(time.time())),
            )
            return int(cur.lastrowid)


def unread(player_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE player_id = ? AND read = 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (player_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_read(ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})",
                ids,
            )
            return conn.total_changes


__all__ = ["add", "unread", "mark_read"]
