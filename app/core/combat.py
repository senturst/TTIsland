"""回合制战斗结算。全部纯函数，方便被 bench.py 拿去做上千次模拟对局。

设计取舍：
  * 命中用**减法模型**而非乘法——玩家能心算，调数值时直觉不会失真
  * 护甲走**固定减伤**而非百分比——低护甲不至于鸡肋
  * 闪避不独立 roll，并入命中公式——少一次随机就少一层方差
  * 暴击**必中**——让暴击有明确的爽感，而不是"暴击了但没打中"
"""
from __future__ import annotations

from typing import Any

from ..data.loader import GameConfig
from . import talents
from .rng import RNG
from .infection import modifiers as infection_mods


def clamp_hit(cfg: GameConfig, chance: float) -> int:
    c = cfg.balance["combat"]
    return int(max(c["hit_floor"], min(c["hit_ceiling"], chance)))


# ----------------------------------------------------------------------
# 攻击方画像
# ----------------------------------------------------------------------
def player_profile(
    cfg: GameConfig, state: dict, weapon: dict | None = None
) -> dict[str, Any]:
    """从 run 状态推导玩家的战斗属性（含感染、装备、buff 修正）。

    weapon：显式覆盖手持武器——拳头兜底攻击（持枪挥拳/空手）时传拳头配置，
    让命中/暴击/伤害按拳头算而不是按枪算。缺省读当前装备。
    """
    base = cfg.balance["player"]
    inf = infection_mods(cfg, state.get("infection", 0))

    acc = float(base["acc"])
    acc += float(inf["acc"])

    weapon = weapon if weapon is not None else equipped_weapon(cfg, state)
    acc += float(weapon.get("acc_mod", 0)) if weapon else 0.0

    crit = float(weapon.get("crit", base["crit"])) if weapon else float(base["crit"])
    crit += float(talents.mod(state, "crit", 0.0))

    dmg_pct = float(inf["dmg_pct"])
    # 天赋：近战 / 枪械分流加成
    is_ranged = bool(weapon and weapon.get("kind") == "ranged")
    if is_ranged:
        dmg_pct += float(talents.mod(state, "ranged_dmg_pct", 0.0))
    else:
        dmg_pct += float(talents.mod(state, "melee_dmg_pct", 0.0))
    # 亡命之徒：残血反扑
    threshold = float(talents.mod(state, "low_hp_threshold", 0.30))
    if state.get("hp", 1) <= state.get("hp_max", 1) * threshold:
        dmg_pct += float(talents.mod(state, "low_hp_dmg_pct", 0.0))
    # 处刑人：敌人残血加伤（P7 天赋池）
    if any(e["hp"] / max(1, e["hp_max"]) < 0.3 for e in (state.get("combat") or {}).get("enemies", [])):
        dmg_pct += float(talents.mod(state, "execute_dmg_pct", 0.0))

    # 尸变模式：全能力 +30%
    if state.get("zombified"):
        mult = float(cfg.balance["infection"]["zombify"]["stat_mult"])
        dmg_pct += mult - 1.0
        acc *= mult

    # 临时 buff
    for b in state.get("buffs") or []:
        acc += float(b.get("acc", 0))
        dmg_pct += float(b.get("dmg_pct", 0))

    # 手电耗尽（第 2 层机制）
    if state.get("flashlight") is not None and state["flashlight"] <= 0:
        acc += float(
            cfg.level_theme(state["depth"])
            .get("modifiers", {})
            .get("flashlight_dead_acc_penalty", 0)
        )

    dmg_lo = dmg_hi = 0
    if weapon:
        lo, hi = weapon["dmg"]
        dmg_lo, dmg_hi = float(lo), float(hi)
        # 破损折损只对"有耐久概念"的近战武器生效；拳头 durability=null 永不破损
        w_dur = weapon.get("durability")
        if (
            weapon.get("kind") == "melee"
            and w_dur is not None
            and w_dur <= 0
        ):
            broken = float(cfg.balance["loot"]["broken_weapon_mult"])
            dmg_lo, dmg_hi = dmg_lo * broken, dmg_hi * broken

    return {
        "acc": clamp_hit(cfg, acc + float(talents.mod(state, "acc", 0))),
        "eva": float(base["eva"]) + float(talents.mod(state, "eva", 0)),
        "armor": float(base["armor"]) + _armor_value(cfg, state)
        + float(talents.mod(state, "armor", 0)),
        "crit": crit,
        "crit_mult": float(base["crit_mult"]),
        "strength": float(base["strength"]),
        "dmg": [dmg_lo, dmg_hi],
        "dmg_pct": dmg_pct,
        "taken_dmg": sum(float(b.get("taken_dmg", 0)) for b in state.get("buffs") or []),
    }


def _armor_value(cfg: GameConfig, state: dict) -> float:
    """玩家固定减伤仅来自基础值 + 天赋。

    装备（护甲）的减伤改为「按等级百分比吸伤」，由
    RunEngine._apply_armor_absorb 结算并扣除耐久，不再这里固定减伤——
    否则会同时有固定减伤 + 百分比吸伤两层，调手感时互相打架。
    """
    return 0.0


def equipped_weapon(cfg: GameConfig, state: dict) -> dict | None:
    """当前武器。

    state["weapon"] 形如 {"id","durability","dmg_mult","passes"}——
    后两个是遗物传承衰减的结果，会直接压低这件武器的伤害。
    """
    w = state.get("weapon")
    if not w:
        return None
    wid = w["id"] if isinstance(w, dict) else w
    item = dict(cfg.item(wid))
    if isinstance(w, dict):
        if w.get("durability") is not None:
            item["durability"] = w["durability"]
        mult = float(w.get("dmg_mult", 1.0))
        if mult != 1.0:
            lo, hi = item["dmg"]
            item["dmg"] = [max(1, round(float(lo) * mult)), max(1, round(float(hi) * mult))]
    return item


def enemy_profile(cfg: GameConfig, enemy: dict) -> dict[str, Any]:
    return {
        "acc": float(enemy["acc"]),
        "eva": float(enemy["eva"]),
        "armor": float(enemy["armor"]),
        "crit": float(enemy.get("crit", 0.02)),
        "crit_mult": 1.8,
        "strength": 0.0,
        "dmg": [float(enemy["dmg"][0]), float(enemy["dmg"][1])],
        "dmg_pct": 0.0,
        "taken_dmg": 0.0,
    }


# ----------------------------------------------------------------------
# 单次攻击结算
# ----------------------------------------------------------------------
def resolve_attack(
    cfg: GameConfig,
    rng: RNG,
    attacker: dict,
    defender: dict,
    attacker_meta: dict | None = None,
) -> dict[str, Any]:
    """结算一次攻击。

    attacker / defender 为 profile；attacker_meta 承载武器与怪物特性。
    返回 {"hit", "crit", "dmg", "infection", "effects"}
    """
    meta = attacker_meta or {}
    crit = rng.chance(float(attacker["crit"]))

    if not crit:
        chance = clamp_hit(cfg, float(attacker["acc"]) - float(defender["eva"]))
        if not rng.chance(chance / 100.0):
            return {"hit": False, "crit": False, "dmg": 0, "infection": 0, "effects": []}

    lo, hi = attacker["dmg"]
    raw = float(rng.randint(int(round(lo)), int(round(hi))))
    if crit:
        raw *= float(attacker["crit_mult"])
    # 力量加成
    raw += attacker["strength"] / float(cfg.balance["combat"]["str_divisor"])
    # 百分比增益（感染狂躁 / 尸变 / buff）
    raw *= 1.0 + float(attacker["dmg_pct"])
    # 护甲固定减伤
    raw -= float(defender["armor"])
    # 减伤类 buff
    raw -= float(defender.get("taken_dmg", 0))

    dmg = max(1, int(round(raw)))

    infection = 0
    effects: list[str] = []
    if crit:
        effects.append("crit")

    bite_chance = float(meta.get("bite_chance", 0))
    if bite_chance and rng.chance(bite_chance):
        pair = meta.get("bite_infection", cfg.balance["infection"]["sources"]["bite"])
        infection = rng.rand_range_int(pair)
        effects.append("bite")

    return {
        "hit": True,
        "crit": crit,
        "dmg": dmg,
        "infection": infection,
        "effects": effects,
    }


def try_flee(
    cfg: GameConfig,
    rng: RNG,
    player_agi: int,
    enemy_agi: int,
    stamina: int = 0,
    enemy_count: int = 1,
) -> bool:
    """逃跑判定：敏捷差决定基础概率，当前体力每点额外 +0.5%（可配）。

    stamina 传扣减前的当前体力——"拼了命地跑"：体力越满越容易逃掉。
    敌人越多越难脱身：每只额外敌人 −2%（flee_per_enemy，可配），
    被 flee_clamp 钳制。以 1 只为基准，0 只不会出现（逃跑前提是有敌人）。
    """
    c = cfg.balance["combat"]
    chance = c["flee_base"] + c["flee_per_agi"] * (player_agi - enemy_agi)
    stamina_bonus_pct = float(c.get("flee_stamina_bonus_pct", 0))
    chance += stamina * stamina_bonus_pct
    per_enemy = float(c.get("flee_per_enemy", 0))
    chance -= per_enemy * max(0, enemy_count - 1)
    chance = max(c["flee_clamp"][0], min(c["flee_clamp"][1], chance))
    return rng.chance(chance / 100.0)


def player_agility(state: dict) -> int:
    """玩家敏捷（含天赋）。影响先手与逃跑。"""
    from ..data.loader import get_config

    base = int(get_config().balance["player"]["agility"])
    return base + int(talents.mod(state, "agility", 0))


# ----------------------------------------------------------------------
# 怪物实例化与遭遇生成
# ----------------------------------------------------------------------
def make_enemy(cfg: GameConfig, monster_id: str, level: int) -> dict[str, Any]:
    m = cfg.monster(monster_id)
    # 深层成长只做血量 +8%/层，**不加伤害**。
    # 难度靠"新怪种 + 更多数量 + 层主题机制"来推，
    # 而不是让同一只行尸到 5 层就能一巴掌拍死你——那样调不出手感，只会劝退。
    scale = 1.0 + 0.08 * (level - 1)
    hp = max(1, int(round(float(m["hp"]) * scale)))
    dmg = [int(m["dmg"][0]), int(m["dmg"][1])]
    e = {
        "id": m["id"],
        "name": m["name"],
        "hp": hp,
        "hp_max": hp,
        "acc": m["acc"],
        "eva": m["eva"],
        "crit": m.get("crit", 0.02),
        "armor": m.get("armor", 0),
        "speed": m.get("speed", 5),
        "dmg": dmg,
        "archetype": m.get("archetype", "shambler"),
        "bite_chance": m.get("bite_chance", 0),
        "bite_infection": m.get("bite_infection", [3, 6]),
        "on_death": m.get("on_death"),
        "on_hit": m.get("on_hit"),
        "ambush": m.get("ambush", False),
        "boss": m.get("boss", False),
        "elite": m.get("elite", False),
        # P8 扫射（地区 2 远程怪）：[min, max] 发/回合，单发伤害同步下调
        "burst": m.get("burst"),
        "score": m.get("score", 8),
        "xp": m.get("xp", 10),
    }
    if m.get("abilities"):
        e["abilities"] = [
            {**a, "cd_left": int(a.get("cooldown", 4))} for a in m["abilities"]
        ]
    return e


def spawn_encounter(
    cfg: GameConfig, rng: RNG, level: int, bonus: int = 0, boss: bool = False
) -> list[dict[str, Any]]:
    """按层遭遇表生成敌人列表。"""
    if boss:
        # P8 地区化：Boss 按层所属地区取（regions.yaml 的 boss 字段）
        return [make_enemy(cfg, cfg.region_boss(cfg.region_id_for_level(level)), level)]

    table = cfg.monsters_cfg["encounter_tables"]["per_level"][level]
    count = rng.rand_range_int(table["count"]) + bonus

    weights = table["weights"]
    ids = list(weights)
    ws = [weights[i] for i in ids]

    out = [make_enemy(cfg, rng.weighted_choice(ids, ws), level) for _ in range(max(1, count))]

    # 尸潮：本层剩余房间密度翻倍时追加追击者
    return out


def spawn_horde(cfg: GameConfig, rng: RNG, level: int) -> list[dict[str, Any]]:
    h = cfg.monsters_cfg["horde"]
    n = rng.rand_range_int(h["count"])
    out = [make_enemy(cfg, h["monster"], level) for _ in range(n)]
    for m in out:
        # 潮兵标记：清场分支据此判断"清掉的这场是不是尸潮"——
        # 尸潮标记在普通战斗期间置位时，清掉普通战斗不该削减噪音
        m["horde"] = True
    return out
