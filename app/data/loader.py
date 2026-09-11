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

        # 4. 尸潮怪物必须存在
        horde_m = self.monsters_cfg["horde"]["monster"]
        if horde_m not in self.monsters:
            errs.append(f"horde.monster 引用不存在的怪物: {horde_m}")

        # 5. Boss 必须存在
        boss_id = self.levels_cfg["boss"]["id"]
        if boss_id not in self.monsters:
            errs.append(f"boss.id 引用不存在的怪物: {boss_id}")

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

        # 8. 感染区间必须递增且有 100 的尸化档
        bands = self.balance["infection"]["bands"]
        mins = [b["min"] for b in bands]
        if mins != sorted(mins):
            errs.append("infection.bands 的 min 必须递增")
        if mins[-1] != 100:
            errs.append("infection.bands 最后一档 min 必须为 100（尸化）")

        # 9. 噪音来源键要能被引用
        noise_keys = set(self.balance["noise"]["sources"])
        for w in self.items_cfg["weapons"]:
            nk = w.get("noise_key")
            if nk and nk not in noise_keys:
                errs.append(f"武器 {w['id']} 的 noise_key 不存在: {nk}")

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
