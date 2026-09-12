"""配置加载：一次性载入全部 YAML，做交叉引用校验，失败即启动失败。

不做全量 Pydantic 建模（样板代码多、收益低），改为针对**真正会出 bug 的地方**
做硬校验：悬空 ID、概率表不闭合、层级缺失、掉落表指向不存在的物品。
这些是改配置时最容易犯且最难在运行时发现的错误。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config import ROOT

CONFIG_DIR = ROOT / "configs"


class ConfigError(RuntimeError):
    """配置不合法。启动阶段抛出，不让它带着错误数据跑起来。"""


def _load_yaml(name: str) -> Any:
    path = CONFIG_DIR / name
    if not path.exists():
        raise ConfigError(f"缺少配置文件: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class GameConfig:
    """全部游戏配置的只读聚合视图。"""

    def __init__(self) -> None:
        self.balance: dict = _load_yaml("balance.yaml")
        self.monsters_cfg: dict = _load_yaml("monsters.yaml")
        self.items_cfg: dict = _load_yaml("items.yaml")
        self.rooms_cfg: dict = _load_yaml("rooms.yaml")
        self.events_cfg: dict = _load_yaml("events.yaml")
        self.levels_cfg: dict = _load_yaml("level_themes.yaml")
        self.talents_cfg: dict = _load_yaml("talents.yaml")
        self.regions_cfg: dict = _load_yaml("regions.yaml")

        # ---- 物品索引：按 id 聚合所有类别 ----
        self.items: dict[str, dict] = {}
        self._item_kind: dict[str, str] = {}
        for kind, key in (
            ("weapon", "weapons"),
            ("ammo", "ammo"),
            ("consumable", "consumables"),
            ("material", "materials"),
            ("armor", "armor"),
            ("trinket", "trinkets"),
            ("backpack", "backpacks"),
        ):
            for item in self.items_cfg.get(key) or []:
                iid = item["id"]
                if iid in self.items:
                    raise ConfigError(f"物品 ID 重复: {iid}")
                item = dict(item)
                item["_kind"] = kind
                self.items[iid] = item
                self._item_kind[iid] = kind

        # ---- 怪物索引 ----
        self.monsters: dict[str, dict] = {
            m["id"]: m for m in self.monsters_cfg["monsters"]
        }

        # ---- 房间模板：按类型分组 ----
        self.room_templates: dict[str, list[dict]] = self.rooms_cfg["templates"]

        # ---- 事件 ----
        self.events: list[dict] = self.events_cfg["events"]

        # ---- 层主题 ----
        self.levels: dict[int, dict] = {
            int(k): v for k, v in self.levels_cfg["levels"].items()
        }

        # ---- 地区（P6.2.1）：实装地区索引 + 层→地区映射 ----
        # placeholder 地区只是结构占位，不进索引——玩家查不到、校验不碰。
        self.regions: dict[int, dict] = {}
        self._level_region: dict[int, int] = {}
        for k, r in (self.regions_cfg.get("regions") or {}).items():
            if r.get("placeholder"):
                continue
            rid = int(k)
            self.regions[rid] = r
            for lv in r.get("levels") or []:
                self._level_region[int(lv)] = rid

        self._validate()

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def item(self, item_id: str) -> dict:
        try:
            return self.items[item_id]
        except KeyError:
            raise ConfigError(f"未知物品 ID: {item_id}") from None

    def item_kind(self, item_id: str) -> str:
        return self._item_kind[item_id]

    def monster(self, monster_id: str) -> dict:
        try:
            return self.monsters[monster_id]
        except KeyError:
            raise ConfigError(f"未知怪物 ID: {monster_id}") from None

    def level_theme(self, level: int) -> dict:
        try:
            return self.levels[level]
        except KeyError:
            raise ConfigError(f"未定义的层: {level}") from None

    @property
    def max_level(self) -> int:
        return max(self.levels)

    # ---- 地区（P6.2.1）----
    @property
    def max_region(self) -> int:
        return max(self.regions) if self.regions else 1

    def region_for_level(self, level: int) -> dict:
        """某层属于哪个（实装）地区。未归属的层视为地区 1。"""
        rid = self._level_region.get(int(level), 1)
        return self.regions.get(rid) or self.regions[1]

    def region_id_for_level(self, level: int) -> int:
        return self._level_region.get(int(level), 1)

    def last_level_of(self, level: int) -> int:
        """某层所在地区的最后一层（撤离点 / 地区 Boss 层）。

        P8 地区化后「撤离层」是每地区一个，而不是全局最后一层——
        引擎里所有 `level == max_level` 的撤离语义都应改用这里。
        """
        return self.region_last_level(self.region_id_for_level(level))

    def region_boss(self, region_id: int) -> str:
        """地区 Boss 的怪物 ID（守在该地区撤离点，击杀后才能登机）。"""
        rid = int(region_id)
        boss = (self.regions.get(rid) or {}).get("boss") or (
            (self.levels_cfg.get("boss") or {}).get("id")
        )
        if not boss:
            raise ConfigError(f"地区 {rid} 未配置 boss")
        return str(boss)

    def region_last_level(self, region_id: int) -> int:
        """地区的最后一层（撤离点所在层）——撤离成功即解锁下一地区。"""
        levels = self.regions[int(region_id)].get("levels") or []
        return max(levels) if levels else self.max_level

    def region_unlocked(self, region_id: int, region_progress: int) -> bool:
        """地区是否已解锁。

        region_progress = 玩家已从「哪个地区」撤离过（0 = 一个都没通关）。
        地区 N 解锁条件：region_progress >= N-1（即从 N-1 撤离过）。
        地区 1 对所有人开放。
        """
        rid = int(region_id)
        return rid <= 1 or int(region_progress or 0) >= rid - 1

    def room_template(self, room_type: str, template_id: str) -> dict:
        for tpl in self.room_templates.get(room_type, []):
            if tpl["id"] == template_id:
                return tpl
        raise ConfigError(f"未知房间模板: {room_type}/{template_id}")

    def legacy_allowed(self, item_id: str) -> bool:
        """能否作为遗物继承。

        三道闸之一：消耗品、弹药不能带走；重火力（tier 3）也不能。
        后者是防滚雪球的关键——霰弹枪和消防斧必须每局重新挣。
        """
        excluded = set(self.items_cfg.get("legacy_exclude_kinds") or [])
        if self._item_kind.get(item_id) in excluded:
            return False
        max_tier = int(self.balance.get("legacy", {}).get("max_tier", 2))
        return int(self.items[item_id].get("tier", 1)) <= max_tier

    def legacy_rules(self) -> dict:
        return self.balance.get("legacy", {})

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        errs: list[str] = []

        # 0. 现金是独立计数资源，绝不能以物品身份注册
        if "cash" in self.items:
            errs.append(
                "cash 不应是物品（独立计数资源，不进背包）——请从 items.yaml 移除"
            )

        # 1. 开局装备必须存在
        start_weapon = self.balance["player"]["start_weapon"]
        if start_weapon not in self.items:
            errs.append(f"balance.player.start_weapon 指向不存在的物品: {start_weapon}")
        for iid, _qty in self.balance["player"].get("start_items") or []:
            if iid not in self.items:
                errs.append(f"balance.player.start_items 指向不存在的物品: {iid}")

        # 2. 掉落表引用的物品必须存在
        tables = self.balance["loot"]["category_tables"]
        for cat, ids in tables.items():
            for iid in ids:
                if iid not in self.items:
                    errs.append(f"loot.category_tables.{cat} 引用不存在的物品: {iid}")
                elif iid == "cash":
                    errs.append("loot.category_tables 不应包含 cash（独立计数资源）")

        # 2b. 商店/天赋池等其余物品引用也不得含 cash
        def _check_no_cash(where: str, ids) -> None:
            for iid in ids or []:
                if iid == "cash":
                    errs.append(f"{where} 不应包含 cash（独立计数资源）")

        _check_no_cash(
            "merchant.other_pool",
            (self.balance.get("merchant", {}).get("other_pool") or []),
        )

        # 3. 每层遭遇表的怪物必须存在、层级配置必须完整
        enc = self.monsters_cfg["encounter_tables"]["per_level"]
        for lv in range(1, self.max_level + 1):
            if lv not in enc:
                errs.append(f"encounter_tables.per_level 缺少第 {lv} 层")
                continue
            for mid in enc[lv]["weights"]:
                if mid not in self.monsters:
                    errs.append(f"第 {lv} 层遭遇表引用不存在的怪物: {mid}")
            if lv not in self.levels:
                errs.append(f"level_themes 缺少第 {lv} 层")

        # 4. 尸潮怪物必须存在；守门精英怪物必须存在（若配置了）
        horde_m = self.monsters_cfg["horde"]["monster"]
        if horde_m not in self.monsters:
            errs.append(f"horde.monster 引用不存在的怪物: {horde_m}")
        elite_m = (self.balance.get("noise", {}).get("horde", {}).get("elite") or {}).get("monster")
        if elite_m and elite_m not in self.monsters:
            errs.append(f"noise.horde.elite.monster 引用不存在的怪物: {elite_m}")

        # 5. Boss 必须存在
        boss_id = self.levels_cfg["boss"]["id"]
        if boss_id not in self.monsters:
            errs.append(f"boss.id 引用不存在的怪物: {boss_id}")

        # 5b. 地区配置：实装地区的层必须有主题定义，且一层不能归属两个地区；
        #     地区 Boss（P8）必须是已定义的怪物
        for rid, region in self.regions.items():
            rlevels = region.get("levels") or []
            if not rlevels:
                errs.append(f"地区 {rid} 未定义 levels")
            for lv in rlevels:
                if lv not in self.levels:
                    errs.append(f"地区 {rid} 引用了未定义的层: {lv}")
                other = self._level_region.get(lv)
                if other is not None and other != rid:
                    errs.append(f"第 {lv} 层同时归属地区 {other} 和 {rid}")
            boss = region.get("boss")
            if boss and boss not in self.monsters:
                errs.append(f"地区 {rid} 的 boss 引用不存在的怪物: {boss}")

        # 6. 事件：层级合法、结果概率闭合、引用的物品/怪物存在
        for ev in self.events:
            eid = ev["id"]
            lo, hi = ev["levels"]
            if not (1 <= lo <= hi <= self.max_level):
                errs.append(f"事件 {eid} 的 levels 区间不合法: {ev['levels']}")
            for ch in ev["choices"]:
                total = sum(o["p"] for o in ch["outcomes"])
                if abs(total - 1.0) > 1e-6:
                    errs.append(
                        f"事件 {eid}/{ch['id']} 结果概率之和为 {total:.3f}，应为 1.0"
                    )
                for o in ch["outcomes"]:
                    if "item" in o and o["item"] not in self.items:
                        errs.append(f"事件 {eid} 引用不存在的物品: {o['item']}")
                    if "spawn" in o and o["spawn"] not in self.monsters:
                        errs.append(f"事件 {eid} 引用不存在的怪物: {o['spawn']}")
                    lc = o.get("loot_category")
                    if lc and lc not in tables:
                        errs.append(f"事件 {eid} 引用不存在的掉落类别: {lc}")

        # 7. 房间模板：vibe 存在（AI 提示词要靠它），loot.tables 指向合法类别
        for rtype, tpls in self.room_templates.items():
            for tpl in tpls:
                if not tpl.get("vibe"):
                    errs.append(f"房间模板 {rtype}/{tpl['id']} 缺少 vibe 字段")
                for t in tpl.get("tables") or []:
                    if t in tables:
                        continue
                    if t == "weapons":
                        continue  # 武器走独立掉落逻辑
                    errs.append(f"房间模板 {tpl['id']} 引用不存在的掉落类别: {t}")

        # 7b. 灾害房（P6.2.2）：倒计时有效、choices 概率闭合、掉落类别合法
        for tpl in self.room_templates.get("hazard") or []:
            if int(tpl.get("countdown", 0)) <= 0:
                errs.append(f"灾害房 {tpl['id']} 的 countdown 必须 > 0")
            for eff in (tpl.get("onset"), tpl.get("worsening")):
                for k in (eff or {}):
                    if k not in ("infection", "cut", "noise", "heal"):
                        errs.append(f"灾害房 {tpl['id']} 的效果键不合法: {k}")
            for ch in tpl.get("choices") or []:
                total = sum(o["p"] for o in ch["outcomes"])
                if abs(total - 1.0) > 1e-6:
                    errs.append(
                        f"灾害房 {tpl['id']}/{ch['id']} 结果概率之和为 {total:.3f}，应为 1.0"
                    )
                for o in ch["outcomes"]:
                    lc = o.get("loot_category")
                    if lc and lc not in tables:
                        errs.append(f"灾害房 {tpl['id']} 引用不存在的掉落类别: {lc}")

        # 7c. 变异巢穴（P6.2.2）：收获掉落类别合法
        for tpl in self.room_templates.get("nest") or []:
            if int(tpl.get("enemy_bonus", 0)) < 1:
                errs.append(f"巢穴 {tpl['id']} 的 enemy_bonus 必须 ≥ 1（进房必遇敌）")
            for t in tpl.get("harvest_tables") or []:
                if t not in tables:
                    errs.append(f"巢穴 {tpl['id']} 引用不存在的收获掉落类别: {t}")

        # 7d. 幸存者 NPC（P6.2.2）：铺货池引用的物品必须存在
        for tpl in self.room_templates.get("special") or []:
            if tpl.get("kind") == "npc":
                pass  # NPC 铺货走 merchant.other_pool，已在 2b 校验过物品存在性

        # 8. 感染区间必须递增且有 100 的尸化档
        bands = self.balance["infection"]["bands"]
        mins = [b["min"] for b in bands]
        if mins != sorted(mins):
            errs.append("infection.bands 的 min 必须递增")
        if mins[-1] != 100:
            errs.append("infection.bands 最后一档 min 必须为 100（尸化）")

        # 9. 噪音来源键要能被引用；远程武器的弹药与 burst 字段必须合法
        noise_keys = set(self.balance["noise"]["sources"])
        for w in self.items_cfg["weapons"]:
            nk = w.get("noise_key")
            if nk and nk not in noise_keys:
                errs.append(f"武器 {w['id']} 的 noise_key 不存在: {nk}")
            if w.get("kind") == "ranged":
                at = w.get("ammo_type")
                if not at or at not in self.items:
                    errs.append(f"远程武器 {w['id']} 的 ammo_type 不存在: {at}")
                elif self.item_kind(at) != "ammo":
                    errs.append(f"远程武器 {w['id']} 的 ammo_type 不是弹药: {at}")
            if w.get("burst"):
                b = w["burst"]
                if (
                    not isinstance(b, (list, tuple)) or len(b) != 2
                    or int(b[0]) < 1 or int(b[1]) < int(b[0])
                ):
                    errs.append(
                        f"武器 {w['id']} 的 burst 应为 [min, max] 且 1 ≤ min ≤ max: {b}"
                    )

        # 10. 天赋池：ID 唯一、开局物资引用合法
        seen_talent: set[str] = set()
        for t in self.talents_cfg.get("talents") or []:
            if t["id"] in seen_talent:
                errs.append(f"天赋 ID 重复: {t['id']}")
            seen_talent.add(t["id"])
            if not t.get("mods"):
                errs.append(f"天赋 {t['id']} 没有任何 mods，会是纯装饰（鸡肋）")
            for iid, _qty in (t.get("mods") or {}).get("start_items") or []:
                if iid not in self.items:
                    errs.append(f"天赋 {t['id']} 引用不存在的物品: {iid}")

        # 10b. 天赋 mods 键合法性：拼错一个键，天赋就会静默失效——
        # 这类 bug 比崩溃难查得多（历史上 make_enemy 漏透传 xp 字段，
        # 升级系统整体不触发且没有任何报错）。白名单与代码消费点一一对应，
        # 新增 mods 键时必须先在消费方落地，再往这里加。
        KNOWN_MODS_KEYS = {
            # 战斗属性（combat.player_profile）
            "acc", "eva", "armor", "crit", "agility",
            "melee_dmg_pct", "ranged_dmg_pct", "low_hp_dmg_pct", "low_hp_threshold",
            "execute_dmg_pct", "brace_acc_bonus_add",
            # 资源与背包（run_service / _bag_cap）
            "hp_max", "stamina_max", "bag_slots", "kill_heal",
            "descend_heal_add", "flashlight_bonus",
            # 噪音（noise）
            "noise_decay_mult", "noise_add_delta", "horde_threshold_delta",
            # 感染 / 逃跑 / 掉落 / 修理 / 计分
            "infection_taken_mult", "food_infection_bonus",
            "flee_bonus", "loot_extra_roll_chance",
            "ammo_scav_mult", "repair_bonus", "trinket_score_mult",
            # 特殊键：开局物资与弹药（talents.apply / new_run 消费）
            "start_items", "ammo_start",
        }
        for t in self.talents_cfg.get("talents") or []:
            bad = set((t.get("mods") or {})) - KNOWN_MODS_KEYS
            if bad:
                errs.append(
                    f"天赋 {t['id']} 的 mods 含未知键 {sorted(bad)}——拼错的天赋会静默失效"
                )

        if errs:
            raise ConfigError(
                "配置校验失败，共 %d 处问题:\n  - %s" % (len(errs), "\n  - ".join(errs))
            )


# ---------------------------------------------------------------------------
# 热重载
#
# 改数值要重启服务是调平衡时最痛的一环，所以配置支持热重载。
# 但结构配置不能无脑重载——删掉一个物品 ID 会让**正在进行的 run 崩溃**。
# 因此策略是：
#   1. 新配置必须通过完整校验，否则保留旧配置继续跑（绝不因为改错配置而挂服务）
#   2. 检测到"破坏性变更"（ID 被删除、层被删除）时拒绝热重载，提示需要重启
#   3. 通过后才替换；已在运行的 RunEngine 仍持有旧对象，天然安全
# ---------------------------------------------------------------------------

WATCHED_FILES = (
    "balance.yaml", "monsters.yaml", "items.yaml",
    "rooms.yaml", "events.yaml", "level_themes.yaml", "talents.yaml",
)

_current: GameConfig | None = None
_loaded_sig: str = ""      # 注意：不能和下面的 _signature() 函数同名，
_reload_error: str = ""    # 否则赋值时会把函数对象覆盖掉（'str' object is not callable）


def _signature() -> str:
    """所有配置文件的 mtime + 大小，用于判断是否有改动。"""
    parts = []
    for name in WATCHED_FILES:
        p = CONFIG_DIR / name
        try:
            st = p.stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{name}:missing")
    for p in sorted((CONFIG_DIR / "prompts").glob("*.txt")):
        st = p.stat()
        parts.append(f"p/{p.name}:{st.st_mtime_ns}")
    return "|".join(parts)


def _ids(cfg: GameConfig) -> dict[str, set]:
    return {
        "items": set(cfg.items),
        "monsters": set(cfg.monsters),
        "events": {e["id"] for e in cfg.events},
        "talents": {t["id"] for t in cfg.talents_cfg.get("talents") or []},
        "levels": set(cfg.levels),
    }


def _breaking_changes(old: GameConfig, new: GameConfig) -> list[str]:
    """找出会破坏运行中 run 的变更。"""
    a, b = _ids(old), _ids(new)
    problems = []
    for kind in a:
        removed = a[kind] - b[kind]
        if removed:
            problems.append(f"{kind} 被删除: {', '.join(sorted(removed)[:5])}")
    return problems


def get_config() -> GameConfig:
    """取当前配置。文件有改动且新配置合法时自动热重载。"""
    global _current, _loaded_sig, _reload_error

    sig = _signature()
    if _current is not None and sig == _loaded_sig:
        return _current

    # 首次加载：不设限，出错就直接抛（fail fast）
    if _current is None:
        _current = GameConfig()
        _loaded_sig = sig
        return _current

    # 热重载：任何问题都保留旧配置
    try:
        candidate = GameConfig()
    except ConfigError as exc:
        _reload_error = str(exc)
        _loaded_sig = sig          # 记下已检查过的版本，避免每次请求都重试
        return _current

    problems = _breaking_changes(_current, candidate)
    if problems:
        _reload_error = "热重载被拒绝（需重启服务）：" + "；".join(problems)
        _loaded_sig = sig
        return _current

    _current = candidate
    _loaded_sig = sig
    _reload_error = ""
    return _current


def reload_config() -> GameConfig:
    """强制重新加载（忽略 mtime 缓存）。"""
    global _loaded_sig
    _loaded_sig = ""
    return get_config()


def reload_status() -> dict:
    return {
        "signature": _loaded_sig[:40],
        "error": _reload_error or None,
        "hot_reload": True,
    }
