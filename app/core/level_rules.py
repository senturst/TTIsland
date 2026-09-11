"""五层主题钩子。

每层靠一个**唯一机制**区分，而不是把数值线性放大——
数值只有小幅增长（8%/层），改变的是玩家的决策结构：
    1 层日光：教你囤货        2 层黑暗：逼你管理光源
    3 层污染：赌不赌抗生素    4 层警报：噪音几乎无法平息
    5 层倒计时：时间本身就是敌人
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig
from . import talents


def theme(cfg: GameConfig, level: int) -> dict:
    return cfg.level_theme(level)


def modifiers(cfg: GameConfig, level: int) -> dict:
    return theme(cfg, level).get("modifiers", {})


def on_enter_level(cfg: GameConfig, state: dict, level: int) -> list[str]:
    """进入新层时执行一次。返回日志行。"""
    t = theme(cfg, level)
    lines: list[str] = []

    state["depth"] = level
    state["noise"] = float(t.get("on_enter_level", {}).get("noise", 0))
    state["horde"] = False
    state["campfire_used"] = False

    m = modifiers(cfg, level)
    if m.get("flashlight_enabled"):
        bonus = int(talents.mod(state, "flashlight_bonus", 0))
        state["flashlight"] = int(m["flashlight_start"]) + bonus
    else:
        # 离开黑暗层后手电不再是枷锁，但保留数值以免状态缺失
        state.setdefault("flashlight", 100)

    if m.get("evac_countdown"):
        state["evac_countdown"] = int(m["evac_countdown"])

    if t.get("entry_text"):
        lines.append(t["entry_text"])
    lines.append(f"【{t['name']}】{t['subtitle']} — {t['mechanism_desc']}")
    return lines


def before_room(cfg: GameConfig, state: dict, room: dict) -> list[str]:
    """每次进入房间前执行。返回日志行（可能为空）。"""
    lines: list[str] = []
    level = state["depth"]
    m = modifiers(cfg, level)

    # 第 2 层：手电耗电
    if m.get("flashlight_enabled") and state.get("flashlight") is not None:
        cost = int(room.get("light_cost", 0) or 0) + int(m["flashlight_cost_per_room"])
        state["flashlight"] = max(0, state["flashlight"] - cost)
        if state["flashlight"] == 0 and not state.get("_flash_warned"):
            state["_flash_warned"] = True
            lines.append("手电闪了两下，灭了。你什么都看不清。")
        elif 0 < state["flashlight"] <= 20 and state["flashlight"] % 10 == 0:
            lines.append(f"手电的光开始发红。还剩 {state['flashlight']}%。")

    # 第 3 层：高危污染区
    if room.get("hazmat"):
        amount = int(m.get("hazmat_infection", 0))
        if amount:
            from .infection import add as inf_add

            inf_add(cfg, state, amount)
            lines.append(
                f"空气里悬浮着带血的飞沫。你屏住呼吸，但还是吸进去了一点。（感染 +{amount}）"
            )

    return lines


def on_combat_turn(cfg: GameConfig, state: dict) -> list[str]:
    """战斗每回合执行。"""
    lines: list[str] = []
    level = state["depth"]
    m = modifiers(cfg, level)

    if m.get("flashlight_enabled") and state.get("flashlight"):
        state["flashlight"] = max(0, state["flashlight"] - int(m["flashlight_cost_per_turn"]))
        if state["flashlight"] == 0 and not state.get("_flash_warned"):
            state["_flash_warned"] = True
            lines.append("手电灭了。")

    return lines


def tick_turn(cfg: GameConfig, state: dict) -> list[str]:
    """每个玩家行动推进一格。处理倒计时与持续伤害。"""
    lines: list[str] = []
    state["turn"] = state.get("turn", 0) + 1

    # 感染持续伤害
    from .infection import modifiers as inf_mods

    inf = inf_mods(cfg, state.get("infection", 0))
    if inf.get("dot_per_turn"):
        state["hp"] -= int(inf["dot_per_turn"])
        lines.append(f"高烧灼烧着你的身体。（HP −{int(inf['dot_per_turn'])}）")

    # 撤离倒计时
    if state.get("evac_countdown") is not None:
        state["evac_countdown"] -= 1
        left = state["evac_countdown"]
        if left == 10:
            lines.append("直升机在头顶盘旋。你还有十步。")
        elif left == 5:
            lines.append("螺旋桨的声音开始变远。它在准备离开。")
        elif left <= 0:
            lines.append("直升机拉高，转向，消失在楼群之间。你没能赶上。")

    # buff 倒计时
    buffs = state.get("buffs") or []
    if buffs:
        for b in buffs:
            b["turns"] = int(b.get("turns", 0)) - 1
        state["buffs"] = [b for b in buffs if b.get("turns", 0) > 0]

    return lines


def can_search(cfg: GameConfig, state: dict) -> tuple[bool, str]:
    """黑暗中（手电耗尽）无法搜刮。"""
    m = modifiers(cfg, state["depth"])
    if m.get("flashlight_dead_no_search") and state.get("flashlight", 1) <= 0:
        return False, "太黑了，你什么都摸不到。"
    return True, ""


def encounter_rate_mult(cfg: GameConfig, state: dict) -> float:
    m = modifiers(cfg, state["depth"])
    if m.get("flashlight_dead_encounter_mult") and state.get("flashlight", 1) <= 0:
        return float(m["flashlight_dead_encounter_mult"])
    return 1.0


def loot_quality_bonus(cfg: GameConfig, state: dict) -> int:
    return int(modifiers(cfg, state["depth"]).get("loot_quality_bonus", 0))


def loot_rolls_mult(cfg: GameConfig, state: dict) -> float:
    return float(modifiers(cfg, state["depth"]).get("loot_rolls_mult", 1.0))


def search_extra_chance(cfg: GameConfig, state: dict) -> float:
    """搜索续 roll 的额外基础概率（新手层更友好）。默认 0。"""
    return float(modifiers(cfg, state["depth"]).get("search_extra_flat", 0.0))


def medical_bonus(cfg: GameConfig, state: dict) -> int:
    """第 3 层污染房的医疗掉落倍数。"""
    return int(modifiers(cfg, state["depth"]).get("hazmat_medical_bonus", 1))


def zombie_density(cfg: GameConfig, level: int) -> float:
    return float(theme(cfg, level).get("zombie_density", 0.5))


def level_brief(cfg: GameConfig, level: int) -> dict[str, Any]:
    t = theme(cfg, level)
    return {
        "level": level,
        "name": t["name"],
        "subtitle": t["subtitle"],
        "mechanism": t["mechanism"],
        "mechanism_desc": t["mechanism_desc"],
        "brief": t.get("brief", ""),
    }
