"""继承码：撤离成功后发放的一次性档案恢复码。

背景：玩家身份 = 前端 localStorage 的 UUID（无注册体系），清缓存/换设备后
旧档案就接不回来了；6 小时挂机清理还会把 abandoned run 抹掉——
玩家几小时的进度可能一夜归零。

机制：
  * 撤离成功 → 生成 TT-XXXX-XXXX 继承码，绑定当时玩家档案
  * 任意（新）client_id 输入该码 → 把来源玩家的档案（名字/统计/遗物/
    地区进度）覆盖合并进当前身份 → 码立即核销
  * 一次性核销天然防分享：码被任何人用掉后失效，来源玩家档案本身不动
    （复制而非搬走，老身份继续可玩）
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ..pool import connect, transaction

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去 I/1/O/0，防口述误认


def _generate_code() -> str:
    """TT-XXXX-XXXX。冲突概率极低（32^8 ≈ 10^12），撞车重试即可。"""
    def _chunk() -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"TT-{_chunk()}-{_chunk()}"


def create_for_player(client_id: str, run_id: str | None = None) -> str:
    """撤离成功时发码。返回可展示给玩家的码。"""
    now = int(time.time())
    with connect() as conn:
        with transaction(conn):
            # 同一次撤离重复调用（理论不该发生）→ 直接复用已有未核销的码
            row = conn.execute(
                "SELECT code FROM inherit_codes WHERE from_player = ? AND used_by IS NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (client_id,),
            ).fetchone()
            if row:
                return row["code"]
            for _ in range(10):  # 撞主键重试
                code = _generate_code()
                try:
                    conn.execute(
                        "INSERT INTO inherit_codes (code, from_player, created_at, run_id)"
                        " VALUES (?,?,?,?)",
                        (code, client_id, now, run_id),
                    )
                    return code
                except Exception:
                    continue
            raise RuntimeError("继承码生成失败：连续冲突")


def redeem(code: str, to_client_id: str) -> dict[str, Any] | None:
    """核销继承码并把来源玩家档案合并进 to_client_id。

    返回合并后的档案 dict；码无效/已用/来源不存在 → None（不核销）。
    合并策略：名字与统计取来源（这才是"接回我的进度"），遗物/地区进度
    取两者更优（来源丢失时至少保留新身份已刷出来的东西）。
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    from . import players as _players
    now = int(time.time())
    with connect() as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM inherit_codes WHERE code = ?", (code,)
            ).fetchone()
            if not row or row["used_by"]:
                return None
            src = conn.execute(
                "SELECT * FROM players WHERE id = ?", (row["from_player"],)
            ).fetchone()
            if not src:
                return None
            dst = conn.execute(
                "SELECT * FROM players WHERE id = ?", (to_client_id,)
            ).fetchone()
            if not dst:
                # 新身份还不存在（玩家没 hello 过）→ 建一个空档案再合并
                conn.execute(
                    "INSERT INTO players (id, name, created_at, last_seen, named)"
                    " VALUES (?,?,?,?,0)",
                    (to_client_id, src["name"], now, now),
                )
                dst = conn.execute(
                    "SELECT * FROM players WHERE id = ?", (to_client_id,)
                ).fetchone()
            merged = _merge(dict(src), dict(dst))
            conn.execute(
                """
                UPDATE players SET
                    name = ?, named = ?, total_runs = ?, best_depth = ?,
                    best_score = ?, total_kills = ?, escapes = ?, humanity = ?,
                    legacy_item = ?, region_progress = ?, last_seen = ?
                WHERE id = ?
                """,
                (
                    merged["name"], merged["named"], merged["total_runs"],
                    merged["best_depth"], merged["best_score"],
                    merged["total_kills"], merged["escapes"],
                    merged["humanity"], merged["legacy_item"],
                    merged["region_progress"], now, to_client_id,
                ),
            )
            conn.execute(
                "UPDATE inherit_codes SET used_by = ?, used_at = ? WHERE code = ?",
                (to_client_id, now, code),
            )
    return _players.get(to_client_id)


def _merge(src: dict, dst: dict) -> dict:
    """两份档案取优合并：统计/名字跟来源走，遗物/地区进度取更优一侧。"""
    def _better_legacy(a: str | None, b: str | None) -> str | None:
        """遗物按代次少的优先（代次=磨损），都空取非空者。"""
        def _passes(raw: str | None) -> int:
            try:
                return int((json.loads(raw) or {}).get("passes", 0)) if raw else 99
            except Exception:
                return 99
        if not a:
            return b
        if not b:
            return a
        return a if _passes(a) <= _passes(b) else b

    return {
        "name": src["name"],
        "named": src["named"],
        # 累计统计取"来源 + 当前已新增"的并集思路太复杂，直接取更优（MAX）
        "total_runs": max(src["total_runs"], dst["total_runs"]),
        "best_depth": max(src["best_depth"], dst["best_depth"]),
        "best_score": max(src["best_score"], dst["best_score"]),
        "total_kills": max(src["total_kills"], dst["total_kills"]),
        "escapes": max(src["escapes"], dst["escapes"]),
        "humanity": max(src["humanity"], dst["humanity"]),
        "legacy_item": _better_legacy(src.get("legacy_item"), dst.get("legacy_item")),
        "region_progress": max(src["region_progress"], dst["region_progress"]),
    }


def list_for_player(client_id: str) -> list[dict[str, Any]]:
    """玩家名下所有继承码（含已核销）——撤离后弹窗展示 / 帮玩家找回。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT code, created_at, used_by, used_at FROM inherit_codes"
            " WHERE from_player = ? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = ["create_for_player", "redeem", "list_for_player"]
