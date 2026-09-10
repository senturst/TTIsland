"""世界频道消息。

首批（单机闭环）暂不启用 SSE，但表结构与写入口先备好——
P3 接实时聊天时不需要改数据层。
"""
from __future__ import annotations

import time
from typing import Any

from ..pool import connect, transaction

MAX_ROWS = 5000


def add(
    player_id: str,
    player_name: str,
    body: str,
    channel: str = "world",
    kind: str = "chat",
) -> int:
    with connect() as conn:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO chat_messages (player_id, player_name, channel, kind, body, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (player_id, player_name, channel, kind, body, int(time.time())),
            )
            return int(cur.lastrowid)


def since(last_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """取 last_id 之后的消息，用于 SSE 断线重连补发。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def prune() -> int:
    """只保留最近 MAX_ROWS 条，避免长期运行后表无限增长。"""
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                "DELETE FROM chat_messages WHERE id NOT IN "
                f"(SELECT id FROM chat_messages ORDER BY id DESC LIMIT {MAX_ROWS})"
            )
            return conn.total_changes


__all__ = ["add", "since", "recent", "prune"]
