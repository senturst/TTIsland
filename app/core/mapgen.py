"""地牢地图生成：房间有向图 + 模板拼装。

为什么不是网格 BSP/CA：
    文字游戏没有空间可视化，玩家感知的是"第几个房间、下一步去哪"。
    有向图最贴合叙事节奏，连通性天然有保障，也便于后续注入墓碑节点。

结构：
    主干路径线性串联（入口 → 楼梯），主干节点上挂 1-2 房的死胡同分支放高价值奖励。
    分支占比随层数上升，越深越值得绕路。
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig
from .rng import RNG


def _weighted_room_type(rng: RNG, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.weighted_choice(keys, [weights[k] for k in keys])


def _pick_template(cfg: GameConfig, rng: RNG, room_type: str, level: int) -> dict:
    """按层区间筛选可用模板后随机取一个。"""
    pool = [
        t
        for t in cfg.room_templates.get(room_type, [])
        if t.get("level_min", 1) <= level <= t.get("level_max", 99)
    ]
    if not pool:
        pool = cfg.room_templates.get(room_type) or []
    if not pool:
        raise RuntimeError(f"没有可用的房间模板: {room_type}")
    return rng.choice(pool)


def generate_level(cfg: GameConfig, rng: RNG, level: int) -> dict[str, Any]:
    """生成一层的房间图。返回可 JSON 序列化的 dict。"""
    mg = cfg.balance["mapgen"]
    themes = cfg.levels_cfg["levels"]

    total = mg["rooms_base"]
    if level != cfg.max_level:
        total += rng.randint(0, mg["rooms_rand"])
    else:
        total = 8  # 撤离层固定，倒计时压着，不宜太大

    branch_ratio = mg["branch_ratio_by_level"].get(level, 0.3)
    main_len = max(3, round(total * (1.0 - branch_ratio)))
    branch_budget = max(0, total - main_len)

    rooms: list[dict[str, Any]] = []

    # ---- 1. 主干：线性串联 ----
    for i in range(main_len):
        rooms.append(
            {
                "idx": i,
                "kind": "main",
                "type": "empty",  # 稍后按权重分配
                "tpl": None,
                "name": "",
                "vibe": "",
                "exits": [],
                "visited": False,
                "cleared": False,
                "searched": False,
                "hazmat": False,
            }
        )
    for i in range(main_len - 1):
        rooms[i]["exits"].append({"to": i + 1, "label": "继续深入"})

    # ---- 2. 分支：死胡同，挂在主干节点上 ----
    #     不挂在最后一个节点（那里是楼梯），也不挂两个在同一个节点
    anchor_pool = list(range(main_len - 1))
    rng.shuffle(anchor_pool)
    branch_roots: list[int] = []
    idx = main_len
    while branch_budget > 0 and anchor_pool:
        anchor = anchor_pool.pop()
        size = min(branch_budget, rng.randint(mg["branch_min"], mg["branch_max"]))
        if size <= 0:
            break
        branch_roots.append(anchor)
        prev = anchor
        for b in range(size):
            rooms.append(
                {
                    "idx": idx,
                    "kind": "branch",
                    "type": "empty",
                    "tpl": None,
                    "name": "",
                    "vibe": "",
                    "exits": [],
                    "visited": False,
                    "cleared": False,
                    "searched": False,
                    "hazmat": False,
                }
            )
            if b == 0:
                rooms[anchor]["exits"].append(
                    {"to": idx, "label": rng.choice(["旁边的门", "一条侧道", "通往别处的口子"])}
                )
            # 分支房间可返回主干
            rooms[idx]["exits"].append({"to": anchor, "label": "原路返回"})
            if b > 0:
                rooms[idx - 1]["exits"].append({"to": idx, "label": "再往里走"})
            prev = idx
            idx += 1
        branch_budget -= size

    # ---- 3. 分配房间类型与模板 ----
    main_weights = dict(mg["room_weights"]["main"])
    branch_weights = dict(mg["room_weights"]["branch"])
    main_weights.pop("special", None)
    branch_weights.pop("special", None)

    # 楼梯固定在主干末端
    stairs_idx = main_len - 1
    stair_tpl = cfg.room_template("special", "stairs_down")
    rooms[stairs_idx]["type"] = "special"
    rooms[stairs_idx]["tpl"] = stair_tpl["id"]
    rooms[stairs_idx]["name"] = stair_tpl["name"]
    rooms[stairs_idx]["vibe"] = stair_tpl.get("vibe", "")
    rooms[stairs_idx]["special_kind"] = "stairs"

    # 篝火：主干中段，60% 概率出现，每层至多一个
    campfire_idx = None
    if main_len >= 3 and rng.chance(0.6):
        cand = [i for i in range(1, main_len - 1)]
        if cand:
            campfire_idx = rng.choice(cand)
            cf = cfg.room_template("special", "campfire")
            r = rooms[campfire_idx]
            r["type"] = "special"
            r["tpl"] = cf["id"]
            r["name"] = cf["name"]
            r["vibe"] = cf.get("vibe", "")
            r["special_kind"] = "campfire"

    # 其余房间按权重分配
    for r in rooms:
        if r["type"] == "special":
            continue
        weights = main_weights if r["kind"] == "main" else branch_weights
        r["type"] = _weighted_room_type(rng, weights)
        tpl = _pick_template(cfg, rng, r["type"], level)
        r["tpl"] = tpl["id"]
        r["name"] = tpl["name"]
        r["vibe"] = tpl.get("vibe", "")

    # ---- 4. 商人保底：1-4 层每层必出 2 个商人房，且每层不重复 ----
    #     按权重随机刷出来的商人房 + 强制补足配额（从非商人房里随机改铸）。
    #     配额只对地区 1 的 1-4 层生效；撤离层（最后一层）节奏特殊，不加保底。
    #     注意：这里只保证"房间是商人房"；每个商人房的货/类型进房时才生成，
    #     所以同一层天然不会有重复的商人实体。
    # YAML 数字键解析为 int，这里同时兼容 int / str 两种 key
    quota_cfg = mg.get("merchant_guarantee", {}) or {}
    merchant_quota = int(quota_cfg.get(level, quota_cfg.get(str(level), 0)))
    if merchant_quota > 0:
        # 已经按权重刷出来的商人房计入配额
        cur = [r for r in rooms if r["type"] == "merchant"]
        # 可改铸池：非 special、非商人房（楼梯/篝火不动，入口房不动）
        pool = [
            r for r in rooms
            if r["type"] not in ("merchant", "special") and r["idx"] != 0
        ]
        rng.shuffle(pool)
        while len(cur) < merchant_quota and pool:
            r = pool.pop()
            tpl = _pick_template(cfg, rng, "merchant", level)
            r["type"] = "merchant"
            r["tpl"] = tpl["id"]
            r["name"] = tpl["name"]
            r["vibe"] = tpl.get("vibe", "")
            cur.append(r)

    # ---- 5. 层主题钩子：第 3 层的高危污染区 ----
    theme = themes[level]
    hazmat_chance = theme.get("modifiers", {}).get("hazmat_room_chance")
    if hazmat_chance:
        for r in rooms:
            if r["type"] == "special":
                continue
            if rng.chance(hazmat_chance):
                r["hazmat"] = True

    return {
        "level": level,
        "rooms": rooms,
        "entry": 0,
        "current": 0,
        "stairs": stairs_idx,
        "campfire": campfire_idx,
        "main_len": main_len,
        "total": len(rooms),
    }


def current_room(level_map: dict) -> dict:
    return level_map["rooms"][level_map["current"]]


def exits_of(level_map: dict) -> list[dict]:
    return current_room(level_map)["exits"]


def move_to(level_map: dict, target_idx: int) -> dict:
    level_map["current"] = target_idx
    r = current_room(level_map)
    r["visited"] = True
    return r
