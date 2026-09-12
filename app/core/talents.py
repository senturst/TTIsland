"""天赋：三选一的局内加成。

两个来源（P7 升级系统后）：
    * 开局（复活进场）：死亡补偿；上一局撤离成功则不触发
    * 局内升级（击杀精英/XP 攒条）：本局成长，战斗结束后结算

设计边界（重要）：
    * 只在本局生效，不写回玩家档案——死亡清零，不进遗物通道
    * mods 是纯数据，各系统自己消费，新增天赋只改 YAML 不动代码
    * 多天赋叠加：数值型（int/float）求和，比例型（*_mult）连乘。
      唯一需要玩家知道的规则：「再拿一次同名的，效果会叠上去」
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


def draw(cfg: GameConfig, rng: RNG, n: int | None = None, exclude: list[str] | None = None) -> list[dict]:
    """不重复地抽 n 个天赋；exclude 里的不参与（如已拥有的）。"""
    candidates = [t for t in pool(cfg) if t["id"] not in (exclude or [])]
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


def owned_ids(state: dict) -> list[str]:
    """当前已拥有的全部天赋 ID（含旧格式单天赋）。"""
    st = state
    ids = [t["id"] for t in st.get("talents") or []]
    if st.get("talent"):
        ids.append(st["talent"]["id"])
    return ids


def apply(cfg: GameConfig, state: dict, talent: dict) -> None:
    """把天赋追加进 state（多天赋叠加）。即时生效部分当场处理。"""
    entry = {
        "id": talent["id"],
        "name": talent["name"],
        "desc": talent["desc"],
        "mods": dict(talent.get("mods") or {}),
    }
    # 旧格式（单天赋）迁移到列表
    if state.get("talent") and not state.get("talents"):
        state["talents"] = [state.pop("talent")]
    state.setdefault("talents", [])
    state["talents"].append(entry)

    mods = entry["mods"]
    # 生命上限：直接加在基础值上，之后感染削减也按新上限算
    if mods.get("hp_max"):
        state["hp_max"] += int(mods["hp_max"])
        state["hp"] += int(mods["hp_max"])
    # 开局携带类：当场发物品（绕过 _acquire 的溢出暂停——开局在地下室，
    # 溢出由 bag_overflow 正常兜底即可；这里用 loot.grant 直塞）
    # 修复：此前 start_items 只有开局基线物资消费这个键，天赋里的
    # start_items（如快速凝血的 2 绷带）从未发放过。
    from . import loot as _loot
    for iid, qty in mods.get("start_items") or []:
        _loot.grant(cfg, state, iid, int(qty))


# --- 聚合读取 ------------------------------------------------------------

_MODS_KEY = "mods"


def _all_mods(state: dict) -> list[dict]:
    """收集全部天赋的 mods（兼容旧格式单天赋字段）。"""
    out: list[dict] = []
    for t in state.get("talents") or []:
        out.append(t.get(_MODS_KEY) or {})
    if state.get("talent"):  # 旧存档兼容
        out.append(state["talent"].get(_MODS_KEY) or {})
    return out


def mod(state: dict, key: str, default: Any = 0) -> Any:
    """聚合读取某修正值：数值型求和；*_mult / *_pct 之外的加法键同规则。

    聚合规则：
      - 值为数字：求和（+5 与 +5 → +10）
      - 值以 _mult 结尾或为乘法系数（如 infection_taken_mult 0.7）：
        连乘（0.7 × 0.7 → 0.49）
      - 没有任何天赋拥有该键：返回 default
    """
    mods = _all_mods(state)
    values = [m[key] for m in mods if key in m]
    if not values:
        return default
    if key.endswith("_mult"):
        result = 1.0
        for v in values:
            result *= float(v)
        return result
    # 其余按求和（含 start_items 这类非数值键：返回第一个）
    if isinstance(values[0], (int, float)) and not isinstance(values[0], bool):
        return sum(values)
    return values[0]


def has(state: dict, key: str) -> bool:
    return any(key in m for m in _all_mods(state))


def summary(state: dict) -> list[dict]:
    """前端展示用：全部已拥有天赋的短摘要。"""
    out = []
    for t in state.get("talents") or []:
        out.append({"id": t["id"], "name": t["name"], "desc": t["desc"]})
    if state.get("talent"):
        t = state["talent"]
        out.append({"id": t["id"], "name": t["name"], "desc": t["desc"]})
    return out


__all__ = ["pool", "draw", "apply", "mod", "has", "summary", "get_by_id",
           "draw_count", "owned_ids"]
