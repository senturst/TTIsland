"""天赋：复活进场时三选一的局内加成。

设计边界（重要）：
    * 只在本局生效，不写回玩家档案——这是死亡补偿，不是成长系统
    * 上一局撤离成功则不触发（通关已经给了满耐久遗物 + 津贴）
    * mods 是纯数据，各系统自己消费，新增天赋只改 YAML 不动代码
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig
from .rng import RNG


def pool(cfg: GameConfig) -> list[dict]:
    return cfg.talents_cfg.get("talents") or []


def draw_count(cfg: GameConfig) -> int:
    return int(cfg.talents_cfg.get("draw_count", 3))


def get_by_id(cfg: GameConfig, talent_id: str) -> dict | None:
    return next((t for t in pool(cfg) if t["id"] == talent_id), None)


def draw(cfg: GameConfig, rng: RNG, n: int | None = None) -> list[dict]:
    """不重复地抽 n 个天赋。"""
    candidates = pool(cfg)
    n = n if n is not None else draw_count(cfg)
    if not candidates:
        return []

    picked: list[dict] = []
    remaining = list(candidates)
    for _ in range(min(n, len(remaining))):
        choice = rng.weighted_choice(remaining, [c.get("weight", 10) for c in remaining])
        picked.append(choice)
        remaining = [c for c in remaining if c["id"] != choice["id"]]
    return picked


def apply(cfg: GameConfig, state: dict, talent: dict) -> None:
    """把天赋写进 state。会顺带处理即时生效的部分（开局物资、生命上限等）。"""
    state["talent"] = {
        "id": talent["id"],
        "name": talent["name"],
        "desc": talent["desc"],
        "mods": dict(talent.get("mods") or {}),
    }

    mods = state["talent"]["mods"]
    # 生命上限：直接加在基础值上，之后感染削减也按新上限算
    if mods.get("hp_max"):
        state["hp_max"] += int(mods["hp_max"])
        state["hp"] += int(mods["hp_max"])


def mod(state: dict, key: str, default: Any = 0) -> Any:
    """读取当前天赋的某个修正值。没选天赋则返回 default。"""
    talent = state.get("talent")
    if not talent:
        return default
    return (talent.get("mods") or {}).get(key, default)


def has(state: dict, key: str) -> bool:
    return key in ((state.get("talent") or {}).get("mods") or {})


def summary(state: dict) -> dict | None:
    t = state.get("talent")
    if not t:
        return None
    return {"id": t["id"], "name": t["name"], "desc": t["desc"]}


__all__ = ["pool", "draw", "apply", "mod", "has", "summary", "get_by_id", "draw_count"]
