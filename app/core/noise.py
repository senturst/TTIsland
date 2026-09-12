"""噪音系统 0-10（本层计数器）。

设计意图：制造"搜刮 vs 潜行"的张力。
枪 = 强但吵，近战 = 弱但安静。每一枪都是在用未来的安全换现在的省力。
"""
from __future__ import annotations

from ..core import talents
from ..data.loader import GameConfig


def value(state: dict) -> float:
    return float(state.get("noise", 0.0))


def noise_max(cfg: GameConfig, depth: int) -> float:
    """该层的噪音上限：按地区配置（regions.yaml 的 noise_max），缺省用全局 max。

    P8：地区 2「军事检疫营地」是远程枪械的主场，noise_max = 30（用户拍板）。
    """
    ncfg = cfg.balance["noise"]
    base = float(ncfg.get("max", 10) or 10)
    region = cfg.region_for_level(depth)
    rm = region.get("noise_max")
    return float(rm) if rm else base


def region_scale(cfg: GameConfig, depth: int) -> float:
    """地区噪音缩放系数（上限 / 全局基线）。

    上限放大时，尸潮触发线、平息线、自然衰减同比例放大——
    改的是"远程可以更吵"的预算，不是尸潮出现的频率。
    """
    base = float(cfg.balance["noise"].get("max", 10) or 10)
    return noise_max(cfg, depth) / base if base else 1.0


def add(cfg: GameConfig, state: dict, key_or_amount: float | str) -> float:
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
    cap = noise_max(cfg, int(state.get("depth", 1)))
    state["noise"] = min(cap, value(state) + amount)
    return amount


def decay(cfg: GameConfig, state: dict, level_theme: dict) -> float:
    """每移动一个房间的自然衰减。返回衰减后的值。"""
    ncfg = cfg.balance["noise"]
    base = float(ncfg["decay_per_room"])
    mods = level_theme.get("modifiers", {})
    base *= float(mods.get("noise_decay_mult", 1.0))
    base *= float(talents.mod(state, "noise_decay_mult", 1.0))
    # 地区缩放：上限 ×3 的地区，衰减也 ×3（预算变大，节奏不变）
    base *= region_scale(cfg, int(state.get("depth", 1)))

    floor = float(mods.get("noise_floor", 0)) * region_scale(cfg, int(state.get("depth", 1)))
    new = max(floor, value(state) - base)
    state["noise"] = new
    return new


def horde_active(state: dict) -> bool:
    return bool(state.get("horde", False))


def check_horde(cfg: GameConfig, state: dict) -> bool:
    """噪音越过阈值触发尸潮；降到解除线以下自动平息。返回本次是否**新触发**。

    触发/平息线按地区缩放（上限 30 的地区，触发线 = 8 × 3 = 24）。
    """
    ncfg = cfg.balance["noise"]["horde"]
    scale = region_scale(cfg, int(state.get("depth", 1)))
    v = value(state)
    if state.get("horde"):
        if v <= float(ncfg["end"]) * scale:
            state["horde"] = False
        return False
    threshold = (
        float(ncfg["threshold"])
        + float(talents.mod(state, "horde_threshold_delta", 0))
    ) * scale
    if v >= threshold:
        state["horde"] = True
        return True
    return False


def cut_after_wave_clear(cfg: GameConfig, state: dict, extra_cut: float = 0.0) -> float:
    """消灭一波尸潮后的噪音削减（clear_noise_cut）。返回实际削减后的值。

    同时平息尸潮——把追兵打退，潮水就算退了。
    extra_cut：破潮者天赋的追加削减（在基础削减上叠加，总削减封顶 90%）。
    """
    ncfg = cfg.balance["noise"]["horde"]
    state["horde"] = False
    cut = min(0.9, float(ncfg.get("clear_noise_cut", 0)) + max(0.0, float(extra_cut)))
    if cut > 0:
        state["noise"] = max(0.0, value(state) * (1.0 - cut))
    return value(state)


def bar(value_: float, max_: int = 10) -> str:
    """HUD 用的 ASCII 条。"""
    filled = int(round(value_ / max_ * 10))
    return "▁▂▃▄▅▆▇█"[max(0, min(7, filled - 1))] if filled else "·"
