"""感染度系统 0-100。

设计意图：制造"安全 vs 贪心"的持续张力。
关键在于 75+ 档给的是**增益**而不是纯惩罚——高感染是一种可玩的策略位置，
玩家会主动在高位玩火，而不是一超标就想办法压回去。
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig

# 累加型字段：命中的每一档都叠加
_ADDITIVE = ("acc", "dmg_pct", "dot_per_room", "dot_per_turn")
# 层级型字段：只取"定义了该字段的最高档"，不叠加
_TIERED = ("hp_max_pct",)


def clamp(value: float) -> int:
    return max(0, min(100, int(value)))


def band_name(cfg: GameConfig, value: int) -> str:
    name = ""
    for b in cfg.balance["infection"]["bands"]:
        if value >= b["min"]:
            name = b["name"]
    return name


def modifiers(cfg: GameConfig, value: int) -> dict[str, Any]:
    """返回当前感染度下的全部修正。

    累加型：acc / dmg_pct / dot_per_room / dot_per_turn
    层级型：hp_max_pct（只取最高档定义的值，不叠加）
    """
    out: dict[str, Any] = {"band": "", "zombify": False, "npc_hostile": False}
    for f in _ADDITIVE:
        out[f] = 0
    out["hp_max_pct"] = 0.0

    for b in cfg.balance["infection"]["bands"]:
        if value < b["min"]:
            continue
        out["band"] = b["name"]
        for f in _ADDITIVE:
            out[f] = out.get(f, 0) + b.get(f, 0)
        if "hp_max_pct" in b:
            out["hp_max_pct"] = b["hp_max_pct"]
        if b.get("npc_hostile"):
            out["npc_hostile"] = True
        if b.get("zombify"):
            out["zombify"] = True
    return out


def add(cfg: GameConfig, state: dict, amount: float) -> tuple[int, int]:
    """增减感染度，返回 (旧值, 新值)。"""
    old = state.get("infection", 0)
    new = clamp(old + amount)
    state["infection"] = new
    return old, new


def describe_change(cfg: GameConfig, old: int, new: int) -> str | None:
    """跨越档位时返回提示文本，否则 None。用于日志。"""
    if old == new:
        return None
    o, n = band_name(cfg, old), band_name(cfg, new)
    if o == n:
        return None
    if not n:
        return "感染退了下去，你感觉脑子清醒了些。"
    names = {
        "低烧": "你开始发烧，额头烫得厉害。",
        "溃烂": "伤口发黑溃烂，每走一步都在抽痛。",
        "狂躁": "视野边缘泛红。你忽然觉得，这样好像也不错。",
        "尸化": "你感觉不到自己的心跳了。",
    }
    return names.get(n, f"感染进入 {n} 阶段。")
