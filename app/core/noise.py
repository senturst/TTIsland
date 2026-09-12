"""噪音系统 0-10（本层计数器）。

设计意图：制造"搜刮 vs 潜行"的张力。
枪 = 强但吵，近战 = 弱但安静。每一枪都是在用未来的安全换现在的省力。
"""
from __future__ import annotations

from ..core import talents
from ..data.loader import GameConfig


def value(state: dict) -> float:
    return float(state.get("noise", 0.0))


def add(cfg: GameConfig, state: dict, key_or_amount: str | float) -> float:
    """按来源键或绝对值增加噪音，返回增量。"""
    ncfg = cfg.balance["noise"]
    if isinstance(key_or_amount, str):
        amount = float(ncfg["sources"][key_or_amount])
        # 轻步：开枪与撬门的噪音各 −1（只修正"主动制造"的噪音，不影响引怪事件）
        amount += float(talents.mod(state, "noise_add_delta", 0))
    else:
        amount = float(key_or_amount)
    amount = max(0.0, amount)
    # 第 4 层：噪音有基线，且自然衰减减半（由 decay 处理基线）
    state["noise"] = min(float(ncfg["max"]), value(state) + amount)
    return amount


def decay(cfg: GameConfig, state: dict, level_theme: dict) -> float:
    """每移动一个房间的自然衰减。返回衰减后的值。"""
    ncfg = cfg.balance["noise"]
    base = float(ncfg["decay_per_room"])
    mods = level_theme.get("modifiers", {})
    base *= float(mods.get("noise_decay_mult", 1.0))
    base *= float(talents.mod(state, "noise_decay_mult", 1.0))

    floor = float(mods.get("noise_floor", 0))
    new = max(floor, value(state) - base)
    state["noise"] = new
    return new


def horde_active(state: dict) -> bool:
    return bool(state.get("horde", False))


def check_horde(cfg: GameConfig, state: dict) -> bool:
    """噪音越过阈值触发尸潮；降到解除线以下自动平息。返回本次是否**新触发**。"""
    ncfg = cfg.balance["noise"]["horde"]
    v = value(state)
    if state.get("horde"):
        if v <= float(ncfg["end"]):
            state["horde"] = False
        return False
    threshold = float(ncfg["threshold"]) + float(
        talents.mod(state, "horde_threshold_delta", 0)
    )
    if v >= threshold:
        state["horde"] = True
        return True
    return False


def cut_after_wave_clear(cfg: GameConfig, state: dict) -> float:
    """消灭一波尸潮后的噪音削减（clear_noise_cut）。返回实际削减后的值。

    同时平息尸潮——把追兵打退，潮水就算退了。
    """
    ncfg = cfg.balance["noise"]["horde"]
    state["horde"] = False
    cut = float(ncfg.get("clear_noise_cut", 0))
    if cut > 0:
        state["noise"] = max(0.0, value(state) * (1.0 - cut))
    return value(state)


def bar(value_: float, max_: int = 10) -> str:
    """HUD 用的 ASCII 条。"""
    filled = int(round(value_ / max_ * 10))
    return "▁▂▃▄▅▆▇█"[max(0, min(7, filled - 1))] if filled else "·"
