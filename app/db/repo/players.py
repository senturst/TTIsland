"""玩家数据访问。

MVP 无密码无 JWT：client_id 即身份。
明确这是取舍——防误操作，不防恶意伪造。等真要上线再加鉴权。
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from ...core.rng import new_seed
from ..pool import connect, transaction

_SURNAMES = [
    "幸存者", "拾荒者", "逃亡者", "守夜人", "清道夫", "信使", "游魂", "断线者",
]
_TRAITS = [
    "沉默", "跛脚", "独眼", "不眠", "空手", "带伤", "哑", "健忘", "机警", "疲惫",
]


def _random_name() -> str:
    r = random.SystemRandom()
    return f"{r.choice(_SURNAMES)}-{r.randrange(1000, 9999)}"


def get_or_create(client_id: str, device_name: str | None = None) -> dict[str, Any]:
    now = int(time.time())
    with connect() as conn:
        row = conn.execute("SELECT * FROM players WHERE id = ?", (client_id,)).fetchone()
        if row:
            conn.execute("UPDATE players SET last_seen = ? WHERE id = ?", (now, client_id))
            conn.commit()
            return dict(row)

        name = _random_name()
        with transaction(conn):
            conn.execute(
                "INSERT INTO players (id, name, created_at, last_seen) VALUES (?,?,?,?)",
                (client_id, name, now, now),
            )
        row = conn.execute("SELECT * FROM players WHERE id = ?", (client_id,)).fetchone()
        return dict(row)


def get(client_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM players WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def rename(client_id: str, name: str) -> dict[str, Any] | None:
    name = name.strip()[:12]
    if not name:
        return None
    with connect() as conn:
        with transaction(conn):
            conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, client_id))
        row = conn.execute("SELECT * FROM players WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def get_legacy(client_id: str) -> dict | None:
    """取出待继承的遗物，并清空槽位（一次性）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT legacy_item FROM players WHERE id = ?", (client_id,)
        ).fetchone()
        if not row or not row["legacy_item"]:
            return None
        with transaction(conn):
            conn.execute("UPDATE players SET legacy_item = NULL WHERE id = ?", (client_id,))
        return json.loads(row["legacy_item"])


def set_legacy(client_id: str, legacy: dict | None) -> None:
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE players SET legacy_item = ? WHERE id = ?",
                (json.dumps(legacy, ensure_ascii=False) if legacy else None, client_id),
            )


def record_run_end(
    client_id: str,
    *,
    depth: int,
    score: int,
    kills: int,
    escaped: bool,
    humanity_delta: int = 0,
) -> None:
    """run 结束时更新玩家累计统计。全部走 MAX / 累加，不做读-改-写。"""
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                """
                UPDATE players SET
                    total_runs = total_runs + 1,
                    total_kills = total_kills + ?,
                    best_depth = MAX(best_depth, ?),
                    best_score = MAX(best_score, ?),
                    escapes = escapes + ?,
                    humanity = humanity + ?,
                    last_seen = ?
                WHERE id = ?
                """,
                (kills, depth, score, 1 if escaped else 0, humanity_delta,
                 int(time.time()), client_id),
            )


__all__ = [
    "get_or_create",
    "get",
    "rename",
    "get_legacy",
    "set_legacy",
    "record_run_end",
    "new_seed",
]
