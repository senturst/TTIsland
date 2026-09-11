"""掉落与背包。

背包用 list[{"id","qty","durability"}] 表示：
  * 消耗品/弹药/材料/纪念品 —— qty 累加
  * 武器/护甲 —— qty 恒为 1，用 durability 追踪损耗（同名武器不合并）
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig
from .rng import RNG

STACKABLE = {"consumable", "ammo", "material", "trinket"}


def roll_category(cfg: GameConfig, rng: RNG) -> str:
    w = cfg.balance["loot"]["category_weights"]
    keys = list(w)
    return rng.weighted_choice(keys, [w[k] for k in keys])


def roll_from_category(
    cfg: GameConfig, rng: RNG, category: str, quality_bonus: int = 0
) -> str:
    table = cfg.balance["loot"]["category_tables"].get(category)
    if not table:
        raise ValueError(f"未知掉落类别: {category}")
    if category == "ammo":
        # 弹药：先选弹种，数量在 balance 里配
        item_id = rng.choice(table)
        return item_id
    if quality_bonus:
        # 品质加成：从表里排除最普通的一项，优先给稀有物
        pool = table if len(table) <= 1 else table[1:]
        return rng.choice(pool)
    return rng.choice(table)


def ammo_qty(cfg: GameConfig, rng: RNG, item_id: str) -> int:
    pair = cfg.item(item_id).get("qty", [1, 1])
    return rng.rand_range_int(pair)


def is_stackable(cfg: GameConfig, item_id: str) -> bool:
    return cfg.item_kind(item_id) in STACKABLE


def find_stackable(state: dict, item_id: str) -> dict | None:
    """找到可堆叠物品的现有条目。"""
    for e in state["inventory"]:
        if e["id"] == item_id:
            return e
    return None


def find_equipment(state: dict, item_id: str) -> dict | None:
    """找到武器/护甲类条目（不可堆叠，按 id 匹配第一个）。"""
    for e in state["inventory"]:
        if e["id"] == item_id:
            return e
    return None


def grant(
    cfg: GameConfig,
    state: dict,
    item_id: str,
    qty: int = 1,
    durability: int | None = None,
) -> dict[str, Any] | None:
    """把物品放进背包，返回该物品的背包条目。

    现金是独立计数资源（不进背包、不占格子），grant 只累加 state["cash"] 并返回 None。
    """
    if item_id == "cash":
        state["cash"] = int(state.get("cash", 0)) + int(qty)
        return None
    item = cfg.item(item_id)
    if is_stackable(cfg, item_id):
        for e in state["inventory"]:
            if e["id"] == item_id:
                e["qty"] += qty
                return e
        entry = {"id": item_id, "qty": qty, "durability": None}
    else:
        entry = {
            "id": item_id,
            "qty": 1,
            "durability": durability
            if durability is not None
            else int(item.get("durability", 0)) or None,
        }
    state["inventory"].append(entry)
    return entry


def remove(state: dict, item_id: str, qty: int = 1) -> bool:
    """移除物品。武器/护甲按 id 移除第一个匹配项。现金走独立计数。"""
    if item_id == "cash":
        have = int(state.get("cash", 0))
        if have < qty:
            return False
        state["cash"] = have - int(qty)
        return True
    for i, e in enumerate(state["inventory"]):
        if e["id"] != item_id:
            continue
        if e["qty"] <= qty:
            state["inventory"].pop(i)
        else:
            e["qty"] -= qty
        return True
    return False


def count(state: dict, item_id: str) -> int:
    """现金从独立计数读取；旧局背包里的现金条目也算数（向下兼容）。"""
    if item_id == "cash":
        inv = sum(e["qty"] for e in state["inventory"] if e["id"] == "cash")
        return int(state.get("cash", 0)) + inv
    return sum(e["qty"] for e in state["inventory"] if e["id"] == item_id)


def roll_loot(
    cfg: GameConfig,
    rng: RNG,
    state: dict,
    category: str,
    rolls: int = 1,
    quality_bonus: int = 0,
) -> list[tuple[str, int]]:
    """按类别抽若干次，返回 [(item_id, qty)]。不直接入包，交给调用方展示。"""
    out: list[tuple[str, int]] = []
    for _ in range(max(1, rolls)):
        item_id = roll_from_category(cfg, rng, category, quality_bonus)
        if cfg.item_kind(item_id) == "ammo":
            qty = ammo_qty(cfg, rng, item_id)
        else:
            qty = 1
        out.append((item_id, qty))
    return out


def describe(cfg: GameConfig, item_id: str, qty: int = 1) -> str:
    item = cfg.item(item_id)
    name = item["name"]
    if qty > 1:
        return f"{name} ×{qty}"
    return name
