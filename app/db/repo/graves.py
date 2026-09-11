"""墓碑数据访问。

首批（单机闭环）阶段：死亡即写碑，但尚不注入他人地图。
P4 在此之上加了：
  * pick_candidate —— 给某个玩家挑一具"别人的、还能摸"的墓碑注入地图
  * claim          —— 摸走一件道具（gear 里删掉那件、认领数 +1）
  * bury           —— 掩埋（直接令墓碑不可再被挑中，给掩埋者人道 +1）
  * get            —— 按 id 取，用于房间重入时复用同一具尸体
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
                                    gear_json, infection, epitaph, is_plagued, claim_count,
                                    claim_cap, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,3,?)
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


def get(grave_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM graves WHERE id = ?", (grave_id,)).fetchone()
    return dict(row) if row else None


def pick_candidate(player_id: str, depth: int | None = None) -> dict[str, Any] | None:
    """挑一具"别人的、还有货、没被认领满"的墓碑。

    优先选层数接近当前深度的（体验上更合理：你在 3 层遇到的多半也是 3 层附近死的），
    深度相同时在最近的 30 具里随机抽一具，避免永远只刷最新那具。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graves "
            "WHERE claim_count < claim_cap AND gear_json != '[]' AND player_id != ? "
            "ORDER BY created_at DESC LIMIT 30",
            (player_id,),
        ).fetchall()
    cands = [dict(r) for r in rows]
    if not cands:
        return None
    if depth is not None:
        cands.sort(key=lambda g: abs(g["level"] - depth))
        # 取最接近的 8 具再随机，兼顾"相关"与"不总重复"
        pool = cands[:8]
    else:
        pool = cands
    import random
    return random.choice(pool)


def claim(grave_id: str, uid: str) -> list[dict[str, Any]] | None:
    """摸走 gear 里 uid 对应的那一件，认领数 +1。

    返回摸完后剩下的 gear（供上层判断尸体是否已空）。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT gear_json, claim_count FROM graves WHERE id = ?", (grave_id,)
        ).fetchone()
        if not row:
            return None
        gear: list[dict] = json.loads(row["gear_json"])
        gear = [g for g in gear if g.get("uid") != uid]
        with transaction(conn):
            conn.execute(
                "UPDATE graves SET gear_json = ?, claim_count = claim_count + 1 WHERE id = ?",
                (json.dumps(gear, ensure_ascii=False), grave_id),
            )
    return gear


def bury(grave_id: str) -> None:
    """掩埋：让墓碑不再被挑中（claim_cap 压到已认领数）。"""
    with connect() as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE graves SET claim_cap = claim_count WHERE id = ?", (grave_id,)
            )


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


__all__ = [
    "create", "set_epitaph", "get", "pick_candidate", "claim", "bury",
    "recent", "of_player", "count_all",
]

