"""run 状态机——游戏的全部规则都在这里。

一个 run 的完整生命周期：
    起局 → 进入房间（氛围/遭遇/搜刮/事件）→ 战斗 → 找楼梯下行
    → 第 5 层撤离倒计时 → 撤离成功 / 死亡（留遗物）→ 结算

所有随机都走 RNG 实例（状态随存档持久化），
避免玩家靠刷新页面反复抽同一个掉落。
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from ..ai.service import flavor
from ..core import combat, infection as inf_mod, level_rules, loot, mapgen, noise, talents
from ..core.rng import RNG, new_seed
from ..data.loader import GameConfig

ROOM_TARGET = "room:{depth}:{idx}"


class RunEnded(Exception):
    def __init__(self, status: str, cause: str | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.cause = cause


def _item_desc(item: dict, kind: str, absorb_pct: float | None = None) -> str:
    """给前端用的道具数值摘要（中文短句，用「·」连接）。

    玩家不用真的用一次道具，也能在「随身」面板直接看到它到底干什么、
    数值多少。空字符串表示该类道具没有可展示的数值（如废铁）。
    """
    p: list[str] = []

    if kind == "weapon":
        dmg = item.get("dmg")
        if dmg:
            p.append(f"伤害 {dmg[0]}–{dmg[1]}")
        if item.get("crit"):
            p.append(f"暴击 {round(item['crit'] * 100)}%")
        if item.get("acc_mod"):
            p.append(f"命中 {item['acc_mod']:+d}")
        if item.get("kind") == "ranged":
            if item.get("burst"):
                p.append(f"连射 {item['burst'][0]}–{item['burst'][1]} 发")
            else:
                p.append(f"每发 {item.get('ammo_per_shot', 1)} 弹")
            # P9 弹匣：容量可配（mag_size），任意弹种可装填（伤害按弹种 dmg_mult）
            if item.get("mag_size"):
                p.append(f"弹匣 {item['mag_size']} 发")
        else:
            p.append("静音")
        if item.get("durability"):
            p.append(f"耐久 {item['durability']}")

    elif kind == "armor":
        # 防具按等级百分比吸伤（取代旧的固定减伤）；absorb_pct 由调用方算好传入
        if absorb_pct is not None:
            p.append(f"吸伤 {round(absorb_pct * 100)}%")
        if item.get("armor"):
            p.append(f"防御 +{item['armor']}")
        if item.get("eva"):
            p.append(f"闪避 {item['eva']:+d}")
        if item.get("noise_mod"):
            p.append(f"噪音 +{item['noise_mod']}")

    elif kind == "consumable":
        heal = item.get("heal")
        if heal:
            p.append(f"回复 {heal[0]}–{heal[1]}")
        if item.get("heal_stamina"):
            p.append(f"体力 +{item['heal_stamina']}")
        if item.get("infection"):
            p.append(f"感染 {item['infection']:+d}")
        if item.get("flashlight"):
            p.append(f"手电 +{item['flashlight']}")
        buff = item.get("buff")
        if buff:
            b: list[str] = []
            if buff.get("acc"):
                b.append(f"命中 {buff['acc']:+d}")
            if buff.get("taken_dmg"):
                b.append(f"受伤 {buff['taken_dmg']:+d}")
            if buff.get("turns"):
                b.append(f"{buff['turns']}回合")
            if b:
                p.append("·".join(b))

    elif kind == "trinket":
        if item.get("score"):
            p.append(f"计分 +{item['score']}")

    elif kind == "ammo":
        qty = item.get("qty")
        if isinstance(qty, list):
            p.append(f"每拾 {qty[0]}–{qty[1]}")

    elif kind == "backpack":
        if item.get("slots"):
            p.append(f"容量 +{item['slots']}")

    # materials: 没有可展示数值
    return " · ".join(p)


class _NullWorld:
    """无数据库时的世界数据桩：单机闭环 / 模拟器用，墓碑永远返回占位。"""

    def pick_grave(self, depth: int) -> dict | None:
        return None


class RunEngine:
    # 供 scripts/sim.py 在测量基线时关掉天赋（不然测不出天赋本身的贡献）
    talents_enabled: bool = True

    def __init__(self, cfg: GameConfig, state: dict, world: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.rng = RNG(state=state["rng"])
        # world 是跨局世界数据的注入点（墓碑等）。默认空实现，保证 sim.py 直接驱动时不依赖数据库。
        self.world = world or _NullWorld()
        self._out: list[str] = []
        # 旧局遗留条目清理：物品定义可能已从配置删除（如 cash 曾是 material 实体物品），
        # 残留条目会让 cfg.item() 在响应序列化时炸 500。加载时把未知 ID 条目就地清掉。
        known = cfg.items
        state["inventory"] = [
            e for e in state.get("inventory", []) if e.get("id") in known
        ]

    # ------------------------------------------------------------------
    # 存档
    # ------------------------------------------------------------------
    def persist_rng(self) -> None:
        self.state["rng"] = self.rng.get_state()

    # ------------------------------------------------------------------
    # 感染 → 最大生命
    # ------------------------------------------------------------------
    def _sync_hp_max(self) -> None:
        """感染会压低最大生命，改完感染度必须同步一次，否则数值是死的。

        修复：同步必须**保留天赋的生命加成**（强健体质 +5 之类）——
        旧实现只按基础生命 × 感染系数重算，任何一次感染变动都会把
        天赋加成洗掉（用户报告：感染度高/感冒后加成消失）。
        感染惩罚按比例作用在「基础+天赋」的总和上。
        """
        st = self.state
        inf = inf_mod.modifiers(self.cfg, st["infection"])
        base = int(self.cfg.balance["player"]["hp"])
        base += int(talents.mod(st, "hp_max", 0))
        new_max = max(1, int(round(base * (1.0 + float(inf["hp_max_pct"])))))
        if new_max != st["hp_max"]:
            st["hp_max"] = new_max
            st["hp"] = min(st["hp"], new_max)

    def _add_infection(self, amount: float) -> tuple[int, int]:
        """改感染度并同步最大生命。所有感染变动都必须走这里。"""
        # 抗性体质：只减免**增加**的感染，不影响治疗类减免
        if amount > 0:
            amount *= float(talents.mod(self.state, "infection_taken_mult", 1.0))
        old, new = inf_mod.add(self.cfg, self.state, amount)
        self._sync_hp_max()
        return old, new

    # ------------------------------------------------------------------
    # 防具吸伤（按等级百分比）
    # ------------------------------------------------------------------
    def _armor_absorb_pct(self) -> float:
        """当前装备防甲的吸伤比例（0 表示未装备或无耐久）。"""
        st = self.state
        ar = st.get("armor")
        if not ar or not ar.get("id"):
            return 0.0
        if int(ar.get("durability", 0) or 0) <= 0:
            return 0.0
        tier = int(self.cfg.item(ar["id"]).get("tier", 1))
        tiers = self.cfg.balance["combat"].get("armor_absorb", {}).get("tiers", {1: 0.10})
        return float(tiers.get(tier, tiers.get(1, 0.10)))

    def _apply_armor_absorb(self, dmg: int) -> int:
        """对玩家受到的伤害应用防具百分比吸伤，返回实际「吸收」掉的量。

        吸掉的量转为防具耐久损耗；耐久耗尽后不再吸伤。
        """
        if dmg <= 0:
            return 0
        pct = self._armor_absorb_pct()
        if pct <= 0:
            return 0
        ar = self.state["armor"]
        min_abs = int(self.cfg.balance["combat"].get("armor_absorb", {}).get("min_absorb", 1))
        absorbed = max(min_abs, int(round(dmg * pct)))
        absorbed = min(absorbed, dmg)
        cur = int(ar.get("durability", 0) or 0)
        ar["durability"] = max(0, cur - absorbed)
        return absorbed

    # ==================================================================
    # 起局
    # ==================================================================
    @classmethod
    async def new_run(
        cls, cfg: GameConfig, legacy: dict | None = None, world: Any = None
    ) -> "RunEngine":
        seed = new_seed()
        rng = RNG(seed)
        b = cfg.balance["player"]

        state: dict[str, Any] = {
            "seed": seed,
            "rng": rng.get_state(),
            "depth": 1,
            "turn": 0,
            "hp": b["hp"],
            "hp_max": b["hp"],
            "stamina": b["stamina"],
            "infection": 0,
            "noise": 0.0,
            "horde": False,
            "flashlight": 100,
            "evac_countdown": None,
            "kills": 0,
            "score": 0,
            "humanity": 0,
            # 耐久必须从物品配置初始化。写成 None 会让 _damage_weapon 直接跳过，
            # 近战武器就永远不会磨损——"用久了会断"这个机制会静默失效。
            "weapon": {
                "id": b["start_weapon"],
                "durability": int(cfg.item(b["start_weapon"]).get("durability") or 0) or None,
            },
            "armor": None,
            "backpack": None,
            "inventory": [],
            "buffs": [],
            "log": [],
            "flavor_pending": {},
            "status": "active",
            "ammo_total": 0,
            "zombified": False,
            "zombify_rooms_left": 0,
            "boss_alive": False,
            "boss_lured": 0,
            "boss_lure_spent": False,
            "campfire_used": False,
            "first_combat_done": False,
            "pending_decision": None,
            "legacy_choices": None,
            # P7 升级系统（本局内成长，死亡清零）
            "xp": 0,
            "growth_level": 0,
            "pending_levelups": 0,
            "talents": [],
            "started_at": int(time.time()),
        }

        # 开局物资
        for iid, qty in b.get("start_items") or []:
            loot.grant(cfg, state, iid, qty)

        eng = cls(cfg, state, world=world)
        if legacy:
            eng._apply_legacy(legacy)
            # 上一局是撤离成功 → 不触发天赋。
            # 通关已经给了满耐久遗物 + 撤离津贴；再叠天赋会让"故意去死"重新变成最优解。
            if legacy.get("earned_by") == "escaped":
                eng.state["talent_eligible"] = False
        # 弹药：不再默认发放（用户拍板）。仅当继承到远程武器时按其弹药类型
        # 发 ammo_start——不对口的弹药等于废铁，没有远程就白手起家。
        _w = cfg.item(state["weapon"]["id"]) if state.get("weapon") else None
        if _w and _w.get("kind") == "ranged":
            loot.grant(cfg, state, _w["ammo_type"], b["ammo_start"])
        # 有待选天赋时先不下地牢——否则玩家会在选完天赋前就撞上第一场遭遇，
        # 而"最大生命 +8"这类天赋必须在这之前生效才算数。
        if not eng._draw_talents():
            await eng._enter_level(1, first=True)
        eng.persist_rng()
        return eng

    # ------------------------------------------------------------------
    def _draw_talents(self, reason: str = "spawn") -> bool:
        """抽三选一天赋并挂起决策。reason: spawn=开局, levelup=局内升级。

        开局（复活进场）首局也会给——新手不该必须先死一次才能见到这个系统。
        """
        st = self.state
        if (
            not self.talents_enabled
            or st.get("talent_eligible") is False
            or not self.cfg.talents_cfg.get("talents")
        ):
            return False
        # 升级抽取时排除已拥有的（开局第一次抽取无此限制）
        exclude = talents.owned_ids(st) if reason == "levelup" else []
        options = talents.draw(self.cfg, self.rng, exclude=exclude)
        if not options:
            return False
        st["talent_options"] = [
            {"id": t["id"], "name": t["name"], "desc": t["desc"]} for t in options
        ]
        st["pending_decision"] = "talent"
        if reason == "levelup":
            self._log("你从这场搏杀里悟到了什么。选择你的成长：")
        else:
            self._log("你在一处废弃的地下室里恢复意识。身上只剩下三样东西能指望：")
        return True

    async def _act_talent(self, payload: dict) -> None:
        """三选一。开局：选完才真正开始这一局；升级：选完回到行动流。"""
        st = self.state
        idx = payload.get("index", -1)
        options = st.get("talent_options") or []
        if not isinstance(idx, int) or not (0 <= idx < len(options)):
            self._log("你得选一个。")
            return
        talent = talents.get_by_id(self.cfg, options[idx]["id"])
        if not talent:
            return
        talents.apply(self.cfg, st, talent)
        st["pending_decision"] = None
        st.pop("talent_options", None)
        self._log(f"【天赋：{talent['name']}】{talent['desc']}")
        # 开局抽取：选完才正式下地牢，让生命上限类天赋在第一场战斗前生效。
        # 判据用 level_map（new_run 就把 depth 置 1 了，depth<1 永远不成立），
        # _enter_level(1, first=True) 生成 level_map 后，开局与局内升级共用这条路。
        if "level_map" not in st:
            await self._enter_level(1, first=True)
        else:
            # 升级抽取：若还有排队的升级，继续弹下一个三选一
            self._settle_levelups()
            # 级联尾巴补查溢出：战斗结束点先弹了天赋时，欠下的整理排队等在这
            self._check_bag_overflow()

    # ------------------------------------------------------------------
    def _apply_legacy(self, legacy: dict) -> None:
        """继承遗物。

        继承不再抵扣耐久（用户拍板）：无论死亡还是撤离都**满耐久**继承，
        继承近战武器则不再发撬棍（武器槽被直接替换）、继承枪械发对口子弹
        （见 new_run）。传承代价轴 = 武器伤害衰减（×0.85/代）/
        护甲命中惩罚（−1/代）/ 传满 max_passes 代直接报废——
        最后这条是"越玩越强"正反馈的终止条件。
        """
        cfg, st = self.cfg, self.state
        rules = cfg.legacy_rules()
        max_passes = int(rules.get("max_passes", 3))
        iid = legacy["id"]
        item = cfg.item(iid)

        # 撤离带回来的装备不算一次传承磨损；死亡继承多算一代。
        earned_by = legacy.get("earned_by", "death")
        survived = earned_by == "escaped"
        passes = int(legacy.get("passes", 0)) + (0 if survived else 1)
        if passes > max_passes:
            st["legacy_lost"] = {"name": item["name"], "passes": passes}
            self._log(
                f"上一位留下的{item['name']}终于散架了，你只捡回一把碎铁。"
            )
            return

        kind = cfg.item_kind(iid)
        dmg_mult = float(rules.get("weapon_dmg_mult", 0.85)) ** passes
        armor_delta = -int(rules.get("armor_penalty", 1)) * passes

        # 满耐久继承（耐久不再随代次扣减）
        maxd = int(item.get("durability", 0) or 0)
        dur = maxd or legacy.get("durability")

        if kind == "weapon":
            st["weapon"] = {
                "id": iid, "durability": dur,
                "dmg_mult": round(dmg_mult, 4), "passes": passes,
            }
        elif kind == "armor":
            st["armor"] = {
                "id": iid, "durability": maxd,
                # 实例上限：修甲会磨上限（每修一次 −1），撤离继承带满上限
                "max_durability": maxd or None,
                "passes": passes,
            }
        else:
            loot.grant(cfg, st, iid, 1, dur)
            st["legacy_trinket"] = {"name": item["name"]}

        st["legacy_taken"] = {
            "id": iid, "name": item["name"], "passes": passes,
            "earned_by": earned_by,
        }

        # 撤离津贴：通关带回来的不只是那件装备
        if survived:
            stip = rules.get("escape", {}).get("stipend") or {}
            if stip.get("ammo"):
                aid, aqty = stip["ammo"]
                loot.grant(cfg, st, aid, int(aqty))
            if stip.get("item"):
                loot.grant(cfg, st, stip["item"], 1)
            if stip:
                parts = []
                if stip.get("ammo"):
                    parts.append(
                        f"{cfg.item(stip['ammo'][0])['name']} ×{stip['ammo'][1]}"
                    )
                if stip.get("item"):
                    parts.append(cfg.item(stip["item"])["name"])
                self._log("撤离者的补给：" + "、".join(parts) + "。")
            self._log(f"你带上了{item['name']}。撤离时你把它保养得很好，和新的一样。")
        elif passes > 1:
            wear = "、".join(
                p for p in (
                    f"伤害 ×{dmg_mult:.2f}" if kind == "weapon" else None,
                    f"防御 {armor_delta:+d}" if kind == "armor" else None,
                ) if p
            )
            self._log(
                f"你带上了{item['name']}（第 {passes} 次传承"
                + (f"，{wear}" if wear else "")
                + "）。它比记忆中钝了一些。"
            )
        else:
            self._log(f"你带上了上一位留下的{item['name']}。")

    # ==================================================================
    # 层
    # ==================================================================
    async def _enter_level(self, level: int, first: bool = False) -> None:
        st = self.state
        st["level_map"] = mapgen.generate_level(self.cfg, self.rng, level)
        lines = level_rules.on_enter_level(self.cfg, st, level)
        if not first:
            lines.append("")  # 空行分隔
        self._log_many(lines)
        # P8 地区化：每个地区的最后一层都是撤离点/地区 Boss 层
        st["boss_alive"] = level == self.cfg.last_level_of(level)
        st["boss_lured"] = 0
        st["boss_lure_spent"] = False
        await self._enter_room(st["level_map"]["entry"], entering_level=True)

    async def _descend(self) -> list[str]:
        st = self.state
        if st["depth"] >= self.cfg.max_level:
            return ["已经是最后一层了。"]
        theme = self.cfg.level_theme(st["depth"])
        out: list[str] = []
        if theme.get("exit_text"):
            out.append(theme["exit_text"])
        score_add = self.cfg.balance["scoring"]["per_level"]
        st["score"] += score_add

        # 层间喘息：下楼时喘口气，回一点血。
        # 这让每层成为可独立校准的难度单元，而不是一条单向消耗的血条。
        pct = float(self.cfg.balance.get("progression", {}).get("descend_heal_pct", 0))
        pct += float(talents.mod(st, "descend_heal_add", 0.0))
        if pct > 0 and st["hp"] < st["hp_max"]:
            heal = max(1, int(round(st["hp_max"] * pct)))
            before = st["hp"]
            st["hp"] = min(st["hp_max"], st["hp"] + heal)
            if st["hp"] > before:
                out.append(f"你在楼梯间靠着墙喘了口气。（HP +{st['hp'] - before}）")

        await self._enter_level(st["depth"] + 1)
        return out

    # ==================================================================
    # 房间
    # ==================================================================
    def _log(self, text: str) -> None:
        if not text:
            return
        self.state["log"].append(text)
        if len(self.state["log"]) > 300:
            del self.state["log"][:-300]
        self._out.append(text)

    def _log_many(self, texts: list[str]) -> None:
        for t in texts:
            self._log(t)

    async def _enter_room(self, idx: int, entering_level: bool = False) -> None:
        st = self.state
        lmap = st["level_map"]
        room = mapgen.move_to(lmap, idx)
        st["room"] = {
            "idx": idx,
            "type": room["type"],
            "tpl": room["tpl"],
            "name": room["name"],
            "kind": room["kind"],
            "cleared": room["cleared"],
            "searched": room["searched"],
        }

        if not entering_level:
            noise.decay(self.cfg, st, self.cfg.level_theme(st["depth"]))
            if noise.check_horde(self.cfg, st):
                self._log("噪音引来了它们。地面在震——不止一只。")
                st["horde"] = True

        theme = self.cfg.level_theme(st["depth"])
        for line in level_rules.before_room(self.cfg, st, room):
            self._log(line)
        self._sync_hp_max()  # 高危污染房可能刚改过感染度

        # 感染持续伤害（每进一房）
        inf = inf_mod.modifiers(self.cfg, st["infection"])
        if inf.get("dot_per_room"):
            st["hp"] -= int(inf["dot_per_room"])
            self._log(f"溃烂的伤口在渗血。（HP −{int(inf['dot_per_room'])}）")

        # 尸变模式倒计时
        if st.get("zombified"):
            st["zombify_rooms_left"] -= 1
            if st["zombify_rooms_left"] <= 0:
                self._log("身体终于彻底不听使唤了。")
                st["hp"] = 0

        if st["hp"] <= 0:
            await self._die("感染与失血")
            return

        # ---- 房间氛围（AI，异步补丁）----
        vibe = await flavor.render(
            "room_atmosphere",
            {
                "level_name": theme["name"],
                "level_subtitle": theme["subtitle"],
                "level": st["depth"],
                "room_name": room["name"],
                "vibe": room.get("vibe") or room["name"],
            },
            target_id=ROOM_TARGET.format(depth=st["depth"], idx=idx),
            state=st,
        )
        self._log(f"{room['name']}。{vibe}")

        # ---- 按房间类型生成内容 ----
        if room["type"] == "special":
            await self._enter_special(room)
        elif room["type"] == "combat":
            await self._enter_combat(room)
        elif room["type"] == "loot":
            self._log("这里也许还有能用的东西。")
        elif room["type"] == "event":
            self._enter_event(room)
        elif room["type"] == "grave":
            self._enter_grave(room)
        elif room["type"] == "merchant":
            self._enter_merchant(room)
        elif room["type"] == "hazard":
            self._enter_hazard(room)
        elif room["type"] == "nest":
            await self._enter_nest(room)
        else:
            self._log("什么都没有。")

    async def _enter_special(self, room: dict) -> None:
        st = self.state
        kind = room.get("special_kind")
        if kind == "stairs":
            if st["depth"] == self.cfg.last_level_of(st["depth"]):
                st["room"]["name"] = self.cfg.levels_cfg["boss"]["room_name"]
                await self._enter_boss()
            else:
                self._log("一道向下的楼梯。往下是更黑的地方。")
                self._enter_stairs_elite(room)
        elif kind == "campfire":
            if st.get("campfire_used"):
                self._log("一堆冷掉的灰。你用过一次了。")
            else:
                self._log("有人在这里生过火，还有余温。可以歇一会，但火光会暴露位置。")
        elif kind == "npc":
            self._enter_npc(room)

    def _enter_stairs_elite(self, room: dict) -> None:
        """楼梯口守门精英（避战流太强）：每层出口必刷，不可逃跑，必须消灭。

        skip_levels 里的层（如新手第 1 层）改刷 normal_monster 普通战斗——
        有威慑但留退路。
        """
        st = self.state
        if room.get("cleared"):
            return  # 上一局已击杀过（重进房间不重复刷）
        ecfg = self.cfg.balance["noise"]["horde"].get("elite") or {}
        skip = ecfg.get("skip_levels") or []
        if st["depth"] in skip:
            mid = ecfg.get("normal_monster")
            if mid and mid in self.cfg.monsters:
                room["elite_guard"] = True
                st["room"]["elite_guard"] = True
                enemies = [combat.make_enemy(self.cfg, mid, st["depth"])]
                st["combat"] = {"enemies": enemies, "round": 0}
                st["in_combat"] = True
                self._log(f"楼梯口蹲着一头{enemies[0]['name']}，抬头盯住了你。")
            return
        mid = ecfg.get("monster") or "gatekeeper"
        # P8 地区精英：monsters 映射按地区覆盖全局默认（地区 2 = 变异军士）
        rid = self.cfg.region_id_for_level(st["depth"])
        per_region = ecfg.get("monsters") or {}
        mid = per_region.get(str(rid)) or per_region.get(rid) or mid
        if mid not in self.cfg.monsters:
            return  # 配置指向不存在的怪物时静默跳过（loader 校验兜底）
        enemies = [combat.make_enemy(self.cfg, mid, st["depth"])]
        room["elite_guard"] = True
        st["combat"] = {"enemies": enemies, "round": 0}
        st["in_combat"] = True
        st["room"]["elite_guard"] = True
        e = enemies[0]
        self._log(f"楼梯口立着一堵肉墙——{e['name']}。它缓缓转过身，堵死了下去的路。")
        self._log("跑不掉的。想下楼，就得从它身上踏过去。")

    def _elite_guard_active(self) -> bool:
        """当前战斗里是否有活着的守门精英。"""
        st = self.state
        if not st.get("in_combat"):
            return False
        return any(
            e.get("elite") and e["hp"] > 0
            for e in st.get("combat", {}).get("enemies", [])
        )

    async def _enter_boss(self) -> None:
        st = self.state
        if not st.get("boss_alive"):
            self._log("撤离点空着。直升机随时会来。")
            return
        # P8 地区化：Boss 与 flavor 文案按层所属地区取（不再读全局暴君）
        boss_id = self.cfg.region_boss(self.cfg.region_id_for_level(st["depth"]))
        enemies = combat.spawn_encounter(self.cfg, self.rng, st["depth"], boss=True)
        st["combat"] = {"enemies": enemies, "round": 0}
        st["in_combat"] = True
        st["room"]["boss"] = True
        st["boss_seen"] = True
        self._log(self.cfg.levels_cfg["boss"]["encounter_text"])
        desc = await flavor.render(
            "encounter_open",
            {
                "level_name": self.cfg.level_theme(st["depth"])["name"],
                "level": st["depth"],
                "monster_name": self.cfg.monster(boss_id)["name"],
                "count": 1,
                "monster_desc": self.cfg.monster(boss_id).get("desc", ""),
            },
            target_id=f"enc:{st['depth']}:{boss_id}",
            state=st,
        )
        self._log(desc)

    async def _enter_combat(self, room: dict) -> None:
        st = self.state
        if room["cleared"]:
            self._log("地上躺着几具不再动的躯体。已经清过了。")
            return

        density = level_rules.zombie_density(self.cfg, st["depth"])
        rate = level_rules.encounter_rate_mult(self.cfg, st)
        if st.get("horde"):
            rate *= float(self.cfg.balance["noise"]["horde"]["density_mult"])

        if not self.rng.chance(min(0.95, density * rate)):
            self._log("你屏住呼吸听了很久。这里暂时是空的。")
            room["cleared"] = True
            st["room"]["cleared"] = True
            return

        enemies = combat.spawn_encounter(
            self.cfg, self.rng, st["depth"], bonus=int(room.get("enemy_bonus", 0) or 0)
        )
        # 开局 mercy（用户反馈"出门老撞 3 只"）：本局第一场遭遇固定 1 只。
        # 实测第一战数量本就是 [1,3] 均匀分布，并非固定 3——这是印象性平衡，
        # 用确定性规则把它钉死。
        if not st.get("first_combat_done"):
            enemies = enemies[:1]
        st["first_combat_done"] = True
        if st.get("horde"):
            enemies += combat.spawn_horde(self.cfg, self.rng, st["depth"])

        st["combat"] = {"enemies": enemies, "round": 0}
        st["in_combat"] = True

        first = enemies[0]
        mcfg = self.cfg.monster(first["id"])
        desc = await flavor.render(
            "encounter_open",
            {
                "level_name": self.cfg.level_theme(st["depth"])["name"],
                "level": st["depth"],
                "monster_name": first["name"],
                "count": len(enemies),
                "monster_desc": mcfg.get("desc", ""),
            },
            target_id=f"enc:{st['depth']}:{first['id']}:{len(enemies)}",
            state=st,
        )
        self._log(desc)
        names = "、".join(f"{e['name']}({e['hp']})" for e in enemies)
        self._log(f"敌人：{names}")

    def _enter_event(self, room: dict) -> None:
        st = self.state
        if room.get("resolved"):
            self._log("这里已经处理过了。")
            return
        pool = [
            e for e in self.cfg.events
            if e["levels"][0] <= st["depth"] <= e["levels"][1]
        ]
        if not pool:
            self._log("什么都没有。")
            return
        weights = [e["weight"] for e in pool]
        ev = self.rng.weighted_choice(pool, weights)
        room["event"] = ev["id"]
        st["room"]["event"] = {
            "id": ev["id"],
            "text": ev["text"],
            "choices": [{"id": c["id"], "label": c["label"]} for c in ev["choices"]],
        }
        # 文本去重：氛围文（vibe）已在进门行打印过（"事件点。<vibe>"），
        # 事件行不再织入 {ai_desc}——否则同一段描写会连续出现两遍。
        # 8 个事件模板均为 "{ai_desc}。<增量>" 形式，去掉后语法成立。
        text = ev["text"].replace("{ai_desc}", "").lstrip("。").strip()
        self._log(f"   事件：{text}")

    def _enter_grave(self, room: dict) -> None:
        """墓碑/遗骸房间（P4：注入其他玩家的真实尸体）。

        通过注入的 world 提供者挑一具"别人的、还有货"的墓碑。
        重入同一房间时复用已注入的尸体，避免每次进门都换一具。
        没有可用墓碑（空服务器/全被搜空）时退回「无名遗骸」占位。
        """
        st = self.state
        if room.get("resolved"):
            self._log("这具遗体你已经处理过了。")
            return
        # 重入复用
        existing = st["room"].get("grave")
        if existing and existing.get("grave_id"):
            return

        grave = self.world.pick_grave(st["depth"])
        if grave:
            gear = json.loads(grave.get("gear_json") or "[]")
            st["room"]["grave"] = {
                "grave_id": grave["id"],
                "owner_id": grave["player_id"],
                "player_name": grave["player_name"],
                "level": grave["level"],
                "gear": gear,
                "looted": False,
            }
            room["grave_id"] = grave["id"]
            name = grave["player_name"]
            if gear:
                items = "、".join(g["name"] for g in gear)
                self._log(f"墙角蜷着一具遗体——是 {name} 的。身上还有：{items}。")
            else:
                self._log(f"墙角蜷着一具遗体——是 {name} 的，但已经被搜刮一空了。")
        else:
            st["room"]["grave"] = {"player_name": "无名遗骸", "gear": [], "looted": False}
            self._log("前一具遗体靠在墙边，装备已经被翻过一次，但似乎还剩点东西。")

    # ==================================================================
    # 灾害房（P6.2.2）
    # ==================================================================
    def _enter_hazard(self, room: dict) -> None:
        """灾害房：进房先吃一发「开场效果」，随后有 countdown 个行动窗口。

        每个玩家行动（act）都会推进 hazard_countdown；归零时下一次 act
        触发「恶化」。选择权在玩家：赌一把翻找高价值物资，或者贴边通过。
        """
        st = self.state
        if room.get("resolved"):
            self._log("灾害已经过去了。地上只剩下痕迹。")
            return
        tpl = self.cfg.room_template("hazard", room["tpl"])
        # 重入同一房间不重复吃开场（房间在地图上存了 hazard_started）
        if not room.get("hazard_started"):
            room["hazard_started"] = True
            room["countdown"] = int(tpl.get("countdown", 3))
            self._apply_hazard_effect(tpl.get("onset") or {}, "刚踏进去")
        else:
            self._log(f"{tpl['name']}。{st['room'].get('hazard_note') or '这里还没安全。'}")
            return
        st["room"]["hazard"] = {
            "id": tpl["id"],
            "name": tpl["name"],
            "countdown": room["countdown"],
            "choices": [
                {"id": c["id"], "label": c["label"]} for c in tpl.get("choices") or []
            ],
        }

    def _apply_hazard_effect(self, eff: dict, when: str) -> None:
        """灾害效果的通用结算：infection / cut / noise / heal。"""
        st = self.state
        if not eff:
            return
        if eff.get("infection"):
            delta = self.rng.rand_value(eff["infection"])
            old, new = self._add_infection(delta)
            self._log(f"（{when}·感染 {new - old:+d}）")
            line = inf_mod.describe_change(self.cfg, old, new)
            if line:
                self._log(line)
        if eff.get("cut"):
            dmg = int(self.rng.rand_value(eff["cut"]))
            st["hp"] -= dmg
            self._log(f"（{when}·HP −{dmg}）")
        if eff.get("noise"):
            noise.add(self.cfg, st, int(eff["noise"]))
            self._log(f"（{when}·噪音 +{int(eff['noise'])}）")
        if eff.get("heal"):
            h = int(self.rng.rand_value(eff["heal"]))
            before = st["hp"]
            st["hp"] = min(st["hp_max"], st["hp"] + h)
            self._log(f"（{when}·HP +{st['hp'] - before}）")

    async def _tick_hazard(self) -> None:
        """每次玩家行动后推进灾害倒计时。归零 → 恶化一次并解除。"""
        st = self.state
        hz = st.get("room", {}).get("hazard")
        if not hz:
            return
        room = mapgen.current_room(st["level_map"])
        room["countdown"] = int(room.get("countdown", 0)) - 1
        hz["countdown"] = max(0, room["countdown"])
        if room["countdown"] > 0:
            self._log(f"灾害还在持续。你有 {room['countdown']} 个行动的时间离开或解决它。")
            return
        tpl = self.cfg.room_template("hazard", room["tpl"])
        self._log("** 灾害恶化了！**")
        self._apply_hazard_effect(tpl.get("worsening") or {}, "恶化")
        room["resolved"] = True          # 恶化后灾害平息，不再反复结算
        st["room"].pop("hazard", None)

    async def _act_hazard(self, payload: dict) -> None:
        """灾害房抉择：赌一把（grab/dig）或安全通过（press_on）。

        outcomes 走事件同款加权语法，但效果结算走 _apply_hazard_effect +
        loot_category/quality——比事件多一个品质加成（灾害房的高风险溢价）。
        """
        st = self.state
        room = mapgen.current_room(st["level_map"])
        hz = st.get("room", {}).get("hazard")
        if not hz or room.get("resolved"):
            self._log("这里没有要处理的灾害。")
            return
        tpl = self.cfg.room_template("hazard", room["tpl"])
        choice = next(
            (c for c in tpl.get("choices") or [] if c["id"] == payload.get("choice")),
            None,
        )
        if not choice:
            self._log("你犹豫了。")
            return

        if choice.get("noise"):
            noise.add(self.cfg, st, int(choice["noise"]))

        outcome = self.rng.weighted_choice(
            choice["outcomes"], [o["p"] for o in choice["outcomes"]]
        )
        self._log(outcome.get("text", ""))
        self._apply_hazard_effect(outcome, "结算")
        if outcome.get("loot_category"):
            quality = int(outcome.get("quality", 0))
            for iid, qty in loot.roll_loot(
                self.cfg, self.rng, st, outcome["loot_category"], 1, quality
            ):
                self._acquire(iid, qty)
                self._log(f"获得 {loot.describe(self.cfg, iid, qty)}。")

        # 任何抉择都算解决：这个房间的灾害交互到此为止（倒计时随之解除）
        room["resolved"] = True
        st["room"].pop("hazard", None)

        if st["hp"] <= 0:
            await self._die("被灾害吞没")
            return
        self._check_horde()

    # ==================================================================
    # 变异巢穴（P6.2.2）
    # ==================================================================
    async def _enter_nest(self, room: dict) -> None:
        """巢穴：进房必遇敌（模板 enemy_bonus 额外加怪）。

        清巢奖励不在这里发——等 _enemy_round 判定战斗结束、房间 cleared 时
        由 _nest_harvest 发放（战斗中途逃跑不算清巢，绕路赌输了就是输了）。
        """
        st = self.state
        if room.get("cleared"):
            self._log("被掏空的巢穴。组织已经干瘪发黑。")
            return
        tpl = self.cfg.room_template("nest", room["tpl"])
        enemies = combat.spawn_encounter(
            self.cfg, self.rng, st["depth"],
            bonus=int(tpl.get("enemy_bonus", 2) or 0),
        )
        if st.get("horde"):
            enemies += combat.spawn_horde(self.cfg, self.rng, st["depth"])
        st["combat"] = {"enemies": enemies, "round": 0}
        st["in_combat"] = True
        self._log("你惊动了整个巢。它们从组织的褶皱里涌了出来。")
        first = enemies[0]
        mcfg = self.cfg.monster(first["id"])
        desc = await flavor.render(
            "encounter_open",
            {
                "level_name": self.cfg.level_theme(st["depth"])["name"],
                "level": st["depth"],
                "monster_name": first["name"],
                "count": len(enemies),
                "monster_desc": mcfg.get("desc", ""),
            },
            target_id=f"nest:{st['depth']}:{tpl['id']}:{len(enemies)}",
            state=st,
        )
        self._log(desc)
        names = "、".join(f"{e['name']}({e['hp']})" for e in enemies)
        self._log(f"敌人：{names}")

    def _nest_harvest(self) -> None:
        """清巢奖励：割下巢穴组织换取高价值物资（在战斗结束处调用）。"""
        st = self.state
        room = mapgen.current_room(st["level_map"])
        if room.get("type") != "nest" or room.get("harvested"):
            return
        room["harvested"] = True
        tpl = self.cfg.room_template("nest", room["tpl"])
        tables = tpl.get("harvest_tables") or ["material"]
        rolls = int(tpl.get("harvest_rolls", 2))
        quality = int(tpl.get("harvest_quality", 1))
        found: list[str] = []
        for _ in range(rolls):
            cat = self.rng.choice(tables)
            for iid, qty in loot.roll_loot(self.cfg, self.rng, st, cat, 1, quality):
                self._acquire(iid, qty)
                found.append(loot.describe(self.cfg, iid, qty))
        cash = tpl.get("harvest_cash")
        if cash:
            amt = self.rng.rand_range_int(cash)
            self._acquire("cash", amt)
            found.append(f"现金 ×{amt}")
        self._log(
            "你从巢穴壁上割下了还没坏死的部分——"
            + ("、".join(found) if found else "但没什么能用的。")
        )

    # ==================================================================
    # 幸存者 NPC（P6.2.2）
    # ==================================================================
    def _enter_npc(self, room: dict) -> None:
        """幸存者：活人交易点。只收废料（拾荒者不认纸币）。

        感染 ≥ 75（狂躁档，npc_hostile）时对方先动手——你看起来已经不像人了。
        每层至多 1 个（mapgen 保证）；交易过一次后离开即收场（防双向边刷人道）。
        """
        st = self.state
        if room.get("resolved"):
            self._log("这里已经没有人了。")
            return
        if st["room"].get("npc"):
            self._log("那个身影还在，隔着一段安全距离盯着你。")
            return

        tpl = self.cfg.room_template("special", "survivor_npc")
        threshold = int(tpl.get("hostile_if_infection_ge", 75))
        if st["infection"] >= threshold:
            # 敌对：触发遭遇（用 Walker 们不成体统——用普通遭遇表更合理）
            self._log(
                "对方看清你的脸后猛地后退，抓起东西朝你砸过来："
                "「怪物！滚开！」——你感染太深，在对方眼里你已经不是人了。"
            )
            room["resolved"] = True
            enemies = combat.spawn_encounter(self.cfg, self.rng, st["depth"])
            st["combat"] = {"enemies": enemies, "round": 0}
            st["in_combat"] = True
            names = "、".join(f"{e['name']}({e['hp']})" for e in enemies)
            self._log(f"敌人：{names}")
            return

        # 铺货：从商人 other_pool 抽几件，废料价 = value × scrap_rate
        npc_cfg = self.cfg.balance.get("survivor_npc", {}) or {}
        rate = float(tpl.get("scrap_rate", 0.6))
        pool = self.cfg.balance.get("merchant", {}).get("other_pool") or []
        rolls = int(npc_cfg.get("stock_rolls", 2))
        stock: list[dict] = []
        seen: set[str] = set()
        for _ in range(rolls):
            if not pool:
                break
            iid = self.rng.choice(pool)
            if iid in seen:
                continue
            seen.add(iid)
            value = int(self.cfg.item(iid).get("value", 1))
            stock.append({
                "id": iid,
                "cost": max(1, int(round(value * rate))),
                "value": value,
                "kind": self.cfg.item_kind(iid),
                "sold": False,
            })
        st["room"]["npc"] = {"stock": stock, "gift_chance": float(tpl.get("gift_chance", 0.4))}
        if stock:
            names = "、".join(f"{self.cfg.item(s['id'])['name']}（🧱{s['cost']}）" for s in stock)
            self._log(
                f"一个裹着防尘布的身影从掩体后探出头，指了指你背包里的废铁。"
                f"他愿意用这些换：{names}。"
            )
        else:
            self._log("一个幸存者朝你比了个手势——他没什么可交易的，但也不打算找麻烦。")

    async def _act_npc(self, payload: dict) -> None:
        """幸存者交互：buy（废料换物）/ share（分食物，人道+回赠）/ leave。"""
        st = self.state
        cfg = self.cfg
        room = mapgen.current_room(st["level_map"])
        npc = st["room"].get("npc")
        if room.get("resolved") or not npc:
            self._log("这里没有人。")
            return
        choice = payload.get("choice")

        if choice == "leave":
            room["resolved"] = True
            st["room"].pop("npc", None)
            self._log("你们互相点了点头，各自继续赶路。")
            return

        if choice == "buy":
            iid = payload.get("item")
            entry = next((s for s in npc["stock"] if s["id"] == iid), None)
            if not entry or entry.get("sold"):
                self._log("那件东西已经换出去了。")
                return
            cost = int(entry["cost"])
            if loot.count(st, "scrap") < cost:
                self._log(f"废料不够——他要 🧱{cost}，你只有 🧱{loot.count(st, 'scrap')}。")
                return
            loot.remove(st, "scrap", cost)
            self._acquire(iid, 1)
            entry["sold"] = True
            st["humanity"] += int(self.cfg.room_template("special", "survivor_npc").get("buy_humanity", 1))
            self._log(f"你用 {cost} 废料换来了 {cfg.item(iid)['name']}。（人道 +1）")
            return

        if choice == "share":
            if npc.get("shared"):
                self._log("你已经分过一次了。对方的自尊心不允许你再施舍第二次。")
                return
            # 分食物：需要身上有「能吃/能喝」的消耗品（有治疗量或明确食物/水）
            food_ids = [
                e["id"] for e in st["inventory"]
                if cfg.item_kind(e["id"]) == "consumable"
                and (cfg.item(e["id"]).get("heal") or e["id"] in ("canned", "clean_water"))
            ]
            if not food_ids:
                self._log("你翻遍背包，没什么能分给他的食物。")
                return
            fid = next(
                (i for i in food_ids if loot.count(st, i) > 0), None
            )
            if not fid:
                self._log("你翻遍背包，没什么能分给他的食物。")
                return
            loot.remove(st, fid, 1)
            tpl = cfg.room_template("special", "survivor_npc")
            st["humanity"] += int(tpl.get("share_humanity", 5))
            npc["shared"] = True
            self._log(
                f"你把{cfg.item(fid)['name']}掰了一半递过去。他愣了一下，接了。"
                f"（人道 +{tpl.get('share_humanity', 5)}）"
            )
            if self.rng.chance(float(npc.get("gift_chance", 0.4))):
                pool = cfg.balance.get("merchant", {}).get("other_pool") or []
                if pool:
                    gid = self.rng.choice(pool)
                    self._acquire(gid, 1)
                    self._log(f"他犹豫了一下，从怀里摸出{cfg.item(gid)['name']}塞回你手里：「拿着。谢谢。」")
            room["resolved"] = True
            st["room"].pop("npc", None)
            return

        self._log("你不明白自己想做什么。")

    def _enter_merchant(self, room: dict) -> None:
        """商人房间：进入时随机铺货（1 武器 / 1 装备 / 1 背包 / 3 其他），
        并按概率决定商人类型（普通 / 感染）与是否触发「作者怜悯」。"""
        st = self.state
        if room.get("resolved"):
            self._log("商人已经收摊走了。")
            return
        # 已生成过则不复抓（重入保持同一商人/同一批货）。
        # 注意：房间必须由双向边连回来才可能重入，而重入意味着玩家可以
        # "卖→出门→再进来"无限刷——所以一旦发生过任何成交，进房即收摊（见成交处）。
        if st["room"].get("merchant"):
            m = st["room"]["merchant"]
            if m["type"] == "plagued":
                self._log("那个溃烂的身影还在原地，等着你拿命换货。")
            else:
                self._log("他摊开一块破布，上面零零碎碎全是货：「废铁换命，懂？」")
            return

        mcfg = self.cfg.balance.get("merchant", {})
        pcfg = mcfg.get("plagued", {})
        plagued_chance = float(pcfg.get("spawn_chance", 0))
        is_plagued = bool(plagued_chance and self.rng.chance(plagued_chance))

        mercy = (not is_plagued) and bool(
            mcfg.get("authors_mercy_chance", 0)
            and self.rng.chance(float(mcfg["authors_mercy_chance"]))
        )

        # 感染商人：商店按**原价**铺货——半价要靠上交生命解锁（一次性）
        shop = self._roll_shop(mcfg, 1.0)
        m = {
            "type": "plagued" if is_plagued else "normal",
            "authors_mercy": mercy,
            "mercy_taken": False,
            "shop": shop,
        }
        if is_plagued:
            m["toll_hp"] = int(pcfg.get("toll_hp", 20))
            m["discount"] = float(pcfg.get("discount", 0.5))
            m["toll_armed"] = False
        st["room"]["merchant"] = m

        if is_plagued:
            self._log(
                "一个浑身溃烂的身影挡在路中间，皮肤下有什么在蠕动："
                f"「想活命？拿你的命来换。」（上交 {m['toll_hp']} 点生命，换取一次半价）"
            )
        else:
            self._log("他摊开一块破布，上面零零碎碎全是货：「废铁换命，懂？」")
            if mercy:
                self._log("他今天格外好说话，冲你挤了挤眼：「挑一件，算我送你的。」")

    def _shop_entry(self, iid: str, discount: float) -> dict:
        item = self.cfg.item(iid)
        value = int(item.get("value", 1))
        return {
            "id": iid,
            "cost": max(1, int(round(value * discount))),
            "value": value,
            "kind": self.cfg.item_kind(iid),
            # 每个商品槽只能成交一次（买或卖）：防"无限买同一件再卖回"刷钱
            "sold": False,
        }

    def _roll_shop(self, mcfg: dict, discount: float) -> list[dict]:
        """按 shop_slots 模板随机铺货：1 武器 / 1 装备（护甲或背包）/ 1 背包 / 3 其他。

        P8：各池按当前地区过滤 min_region（军械/军用装备只在地区 2 上架）。
        """
        cfg = self.cfg
        rid = cfg.region_id_for_level(self.state["depth"])

        def _region_ok(iid: str) -> bool:
            return int(cfg.item(iid).get("min_region", 1) or 1) <= rid

        slots = mcfg.get("shop_slots", {}) or {}
        out: list[dict] = []
        for _ in range(int(slots.get("weapon", 0))):
            # 拳头（weight 0）任何渠道都不可获得——兜底近战不是商品；
            # 撬棍可上架（用户拍板：只有拳头不可获取）
            pool = [
                w["id"] for w in cfg.items_cfg["weapons"]
                if int(w.get("weight", 0) or 0) > 0 and _region_ok(w["id"])
            ]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        for _ in range(int(slots.get("gear", 0))):
            pool = [a["id"] for a in cfg.items_cfg["armor"] if _region_ok(a["id"])] + \
                   [b["id"] for b in cfg.items_cfg["backpacks"] if _region_ok(b["id"])]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        for _ in range(int(slots.get("backpack", 0))):
            pool = [b["id"] for b in cfg.items_cfg["backpacks"] if _region_ok(b["id"])]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        pool = [i for i in (mcfg.get("other_pool") or []) if _region_ok(i)]
        for _ in range(int(slots.get("other", 0))):
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        return out

    # ==================================================================
    # 行动分发
    # ==================================================================
    async def act(self, action: str, payload: dict | None = None) -> dict:
        """执行一个玩家行动。返回响应 dict。"""
        payload = payload or {}
        self._out: list[str] = []
        st = self.state

        # 强制决策优先（尸化 / 选遗物）
        if st.get("pending_decision"):
            return await self._handle_pending(action, payload)

        # P9 弹匣装填：非战斗装填是「整理动作」——不推进回合/倒计时/感染 tick。
        # 战斗中装填走正常行动流（耗费 1 回合，装完挨一轮打）。
        if action == "reload" and not st.get("in_combat"):
            self._out = []
            await self._act_reload(payload)
            self.persist_rng()
            return self._response()

        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                self._log("你不明白自己想做什么。")
            else:
                await handler(payload)
                if st["status"] == "active":
                    # 灾害倒计时：每个行动都推进（搜刮/移动/攻击…）。
                    # 归零 → 恶化。注意 _act_hazard 自己解决灾害后 hazard 已弹掉，不会重复结算。
                    if (st.get("room") or {}).get("hazard"):
                        await self._tick_hazard()
                    for line in level_rules.tick_turn(self.cfg, st):
                        self._log(line)
                    # 主题 tick 可能直改感染（L3 病毒培养区每回合 +1）——同步生命上限
                    self._sync_hp_max()
                    if st.get("evac_countdown") is not None and st["evac_countdown"] <= 0:
                        await self._die("错过了撤离")
        except RunEnded as e:
            st["status"] = e.status
            st["death_cause"] = e.cause

        self.persist_rng()
        return self._response()

    async def _handle_pending(self, action: str, payload: dict) -> dict:
        st = self.state
        decision = st["pending_decision"]
        self._out = []

        if decision == "talent":
            await self._act_talent(payload)
        elif decision == "zombify":
            if action == "zombify":
                st["zombified"] = True
                st["zombify_rooms_left"] = int(
                    self.cfg.balance["infection"]["zombify"]["max_rooms"]
                )
                st["pending_decision"] = None
                self._log("你放弃了抵抗。视野变红了，但身体前所未有的有力。")
                for e in st.get("combat", {}).get("enemies", []):
                    e["_ignore_player"] = True
            else:
                await self._die("感染爆发")
        elif decision == "legacy":
            idx = payload.get("index", -1)
            choices = st.get("legacy_choices") or []
            legacy = None
            if isinstance(idx, int) and 0 <= idx < len(choices):
                legacy = choices[idx]
            st["pending_decision"] = None
            st["legacy_choices"] = None
            st["chosen_legacy"] = legacy
            await self._die(st.get("death_cause") or "未知", legacy=legacy)
        elif decision == "grave_pick":
            # 选一件带走 / 什么都不拿 都复用 _act_grave 的处理
            await self._act_grave(payload)
        elif decision == "bag_overflow":
            # 背包超载：丢弃物品回到容量以内（先拿后丢模型的强制清理）。
            # 消耗品允许"用了"代替"丢了"——能用掉的就不该逼玩家白扔。
            if action == "use":
                await self._act_use(payload)
                # 修复：用完同样要检查是否已回到容量以内——此前只有丢弃
                # 路径会解除决策，用掉超载物后决策卡死，逼玩家再丢一件才能关闭。
                self._settle_bag_overflow()
            else:
                await self._act_discard(payload)

        elif decision == "evac_carry":
            # P8 撤离带装：选 1 武器 + 1 装备 + 1 其他带进下一地区
            if action != "carry":
                self._log("直升机不等人——先挑好三样东西，再跳下去。")
                return
            await self._apply_evac_carry(payload)

        self.persist_rng()
        return self._response()

    async def _apply_evac_carry(self, payload: dict) -> None:
        """P8 撤离带装：挑 1 武器 + 1 装备 + 1 其他带进下一地区，其余全部留下。

        payload: {weapon: id|None, gear: id|None, other: id|None}（各槽可空）。
        - 武器没带 → 发新撬棍（start_weapon）；带远程枪 → 发对口弹药 ×ammo_start，
          没带远程 → 1 废料（与死亡继承同一口径，用户拍板）
        - 前端传来的只是界面状态；这里逐一校验 id 归属，不是信任来源
        - 完成后直接进入下一地区首层（status 始终 active，对局不断）
        """
        st = self.state
        cfg = self.cfg

        def _inv(iid):
            if not iid:
                return None
            return next(
                (e for e in st["inventory"] if e["id"] == iid and e["qty"] > 0), None
            )

        # 先快照手持装备（清槽前），带装判定基于快照
        held_weapon = dict(st["weapon"]) if st.get("weapon") else None
        held_armor = dict(st["armor"]) if st.get("armor") else None
        held_pack = dict(st["backpack"]) if st.get("backpack") else None

        # ---- 武器 ----
        wid = payload.get("weapon") or None
        weapon_obj = None
        if wid and held_weapon and held_weapon.get("id") == wid:
            weapon_obj = held_weapon
        else:
            e = _inv(wid)
            if e and cfg.item_kind(wid) == "weapon":
                weapon_obj = {
                    "id": wid,
                    "durability": e.get("durability"),
                    "max_durability": e.get("max_durability"),
                }
        if weapon_obj is None:
            sw = cfg.balance["player"]["start_weapon"]
            weapon_obj = {
                "id": sw,
                "durability": int(cfg.item(sw).get("durability") or 0) or None,
            }
            self._log(f"你没带武器。舱门边扔着一把制式{cfg.item(sw)['name']}——你捡了起来。")
        st["weapon"] = weapon_obj

        # ---- 装备（护甲或背包，二选一）----
        gid = payload.get("gear") or None
        st["armor"] = None
        st["backpack"] = None
        if gid and held_armor and held_armor.get("id") == gid:
            st["armor"] = held_armor
        elif gid and held_pack and held_pack.get("id") == gid:
            st["backpack"] = held_pack
        elif gid:
            e = _inv(gid)
            kind = cfg.item_kind(gid) if e else None
            if kind == "armor":
                st["armor"] = {
                    "id": gid,
                    "durability": e.get("durability"),
                    "max_durability": e.get("max_durability"),
                }
            elif kind == "backpack":
                st["backpack"] = {"id": gid, "slots": int(cfg.item(gid).get("slots", 0))}
            else:
                gid = None  # 不是装备 → 该槽作废

        # ---- 其他（任意非装备物品，整组带走）----
        oid = payload.get("other") or None
        other_entry = None
        if oid and oid not in (wid, gid):
            e = _inv(oid)
            if e and cfg.item_kind(oid) not in ("weapon", "armor", "backpack"):
                other_entry = dict(e)
        st["inventory"] = [other_entry] if other_entry else []

        # ---- 弹药/废料（与死亡继承同一口径）----
        wcfg = cfg.item(st["weapon"]["id"]) if st.get("weapon") else None
        ammo_start = int(cfg.balance["player"]["ammo_start"])
        if wcfg and wcfg.get("kind") == "ranged":
            loot.grant(cfg, st, wcfg["ammo_type"], ammo_start)
            self._log(
                f"舱门边码着它的弹药箱：{cfg.item(wcfg['ammo_type'])['name']} ×{ammo_start} 归你了。"
            )
        else:
            loot.grant(cfg, st, "scrap", 1)
            self._log("你只从舱边摸到一块废料。")

        kept = [st["weapon"].get("id")]
        if st.get("armor"):
            kept.append(st["armor"]["id"])
        if st.get("backpack"):
            kept.append(st["backpack"]["id"])
        if other_entry:
            kept.append(other_entry["id"])
        self._log(
            "你把带得走的都捆在了身上："
            + "、".join(cfg.item(i)["name"] for i in kept if i)
            + "。其余的，都留给了这座城市。"
        )

        # ---- 进入下一地区首层 ----
        rid = int(st.get("region_clear_pending") or 1)
        nxt = rid + 1
        first = min(self.cfg.regions[nxt].get("levels") or [rid * 5 + 1])
        region = self.cfg.regions[nxt]
        st["pending_decision"] = None
        self._log("")
        self._log(f"【地区 {nxt} · {region['name']}】{region.get('subtitle', '')}")
        st["depth"] = first
        await self._enter_level(first)

    # ------------------------------------------------------------------
    # 具体行动
    # ------------------------------------------------------------------
    async def _act_move(self, payload: dict) -> None:
        st = self.state
        if st.get("in_combat"):
            self._log("它们挡在路中间，你走不掉。")
            return
        lmap = st["level_map"]
        exits = mapgen.current_room(lmap)["exits"]
        if not exits:
            self._log("这是个死胡同。")
            return

        target = payload.get("to")
        if target is None:
            target = exits[0]["to"]
        if not any(e["to"] == target for e in exits):
            self._log("那个方向走不通。")
            return

        label = next((e["label"] for e in exits if e["to"] == target), "继续")
        self._log(f"— {label} —")
        # 商人在本房有过成交 → 离开即收摊（防双向边"成交→出门→再进来"无限刷）
        if st["room"].get("merchant_traded"):
            room = mapgen.current_room(lmap)
            if room.get("type") == "merchant":
                room["resolved"] = True
            st["room"]["merchant_traded"] = False
        await self._enter_room(target)

    async def _act_search(self, payload: dict) -> None:
        st = self.state
        if st.get("in_combat"):
            self._log("先解决眼前的东西。")
            return
        room = mapgen.current_room(st["level_map"])
        if room["type"] not in ("loot", "combat", "empty", "grave", "event", "special", "nest", "hazard"):
            self._log("这里没什么可翻的。")
            return
        ok, why = level_rules.can_search(self.cfg, st)
        if not ok:
            self._log(why)
            return
        if room.get("searched"):
            self._log("你已经翻过这里了。")
            return

        room["searched"] = True
        st["room"]["searched"] = True

        cfg = self.cfg
        s = cfg.balance["loot"]["search"]
        max_items = int(s.get("max_items", 3))
        extra_base = float(s.get("extra_base_chance", 0.6))
        decay = float(s.get("extra_decay", 0.5))
        extra_flat = level_rules.search_extra_chance(cfg, st)  # 新手层额外加成

        # 基础掉落：物资房按模板 rolls；其余房间 45% 摸一件
        found: list[str] = []
        if room["type"] == "loot":
            tpl = cfg.room_template("loot", room["tpl"])
            rolls = self.rng.rand_range_int(tpl.get("rolls", [1, 1]))
            rolls = max(1, int(round(rolls * level_rules.loot_rolls_mult(cfg, st))))
            if room.get("hazmat"):
                rolls *= level_rules.medical_bonus(cfg, st)
            quality = level_rules.loot_quality_bonus(cfg, st)
            tables = tpl.get("tables") or ["ammo"]

            if tpl.get("pry_required"):
                if not (cfg.item(st["weapon"]["id"]).get("pry") if st["weapon"] else False):
                    self._log("你得用能撬的东西才打得开。")
                    return
                noise.add(cfg, st, tpl.get("noise_on_pry", 2))
                self._log("你用撬棍别开了它，响声不小。")
                self._damage_weapon(1)

            # 拾荒直觉：额外一次判定的机会
            extra = float(talents.mod(st, "loot_extra_roll_chance", 0.0))
            if extra and self.rng.chance(extra):
                rolls += 1
                self._log("你的手比眼睛先找到了东西。")

            # 基础掉落也受 max_items 封顶，保证"单次搜索最多 N 件"
            for _ in range(min(rolls, max_items)):
                self._grant_search_find(room, tables, quality, found)
        else:
            if self.rng.chance(0.45):
                self._grant_search_find(room, None, 0, found)
            else:
                self._log("你翻遍了每个角落，一无所获。")
                self._check_horde()
                return

        # 额外多件：拿到一件后按 p·decay^(k-1) 续 roll，直到失败或到顶
        k = 1
        while found and len(found) < max_items:
            p = extra_base * (decay ** (k - 1)) + (extra_flat if k == 1 else 0.0)
            if p <= 0 or not self.rng.chance(min(0.95, p)):
                break
            tables = (
                cfg.room_template("loot", room["tpl"]).get("tables") or ["ammo"]
                if room["type"] == "loot" else None
            )
            self._grant_search_find(room, tables, 0, found)
            k += 1

        # 现金：搜索时「独立」概率获取（不走类别权重，与上面的掉落互不干扰）
        cash_cfg = self.cfg.balance.get("loot_cash") or {}
        if cash_cfg.get("chance") and self.rng.chance(float(cash_cfg["chance"])):
            amt = self.rng.rand_range_int(
                [int(cash_cfg.get("min", 1)), int(cash_cfg.get("max", 5))]
            )
            self._acquire("cash", amt)
            self._log(f"你在杂物堆里摸到 {amt} 现金。")

        if found:
            self._log("你找到了：" + "、".join(found))
        else:
            self._log("空的。什么都没剩下。")
        self._check_horde()

    def _grant_search_find(
        self, room: dict, tables, quality: int, found: list[str]
    ) -> None:
        """从一次搜索判定里产出一件物品并入包（先拿后丢：超容量由 bag_overflow 决策兜底）。

        tables 为物资房模板表（可能含 "weapons"）；None 表示走全局类别权重。
        """
        cfg = self.cfg
        st = self.state
        cat = self.rng.choice(tables) if tables is not None else loot.roll_category(cfg, self.rng)
        if cat == "weapons":
            self._roll_weapon(found)
            return
        for iid, qty in loot.roll_loot(cfg, self.rng, st, cat, 1, quality):
            self._acquire(iid, qty)
            found.append(loot.describe(cfg, iid, qty))

    def _roll_weapon(self, found: list[str]) -> None:
        """枪店类房间的武器掉落。拳头（weight 0）与地区外军械（min_region）不可掉。"""
        rid = self.cfg.region_id_for_level(self.state["depth"])
        pool = [
            w for w in self.cfg.items_cfg["weapons"]
            if int(w.get("weight", 0) or 0) > 0
            and int(w.get("min_region", 1) or 1) <= rid
        ]
        if not pool:
            return
        w = self.rng.weighted_choice(pool, [x.get("weight", 10) for x in pool])
        dur = int(w.get("durability", 0)) or None
        self._acquire(w["id"], 1, dur)
        found.append(w["name"])

    async def _act_attack(self, payload: dict) -> None:
        await self._player_attack(payload, ranged=False)

    async def _act_shoot(self, payload: dict) -> None:
        await self._player_attack(payload, ranged=True)

    async def _player_attack(self, payload: dict, ranged: bool) -> None:
        st = self.state
        if not st.get("in_combat"):
            self._log("现在没有要打的东西。")
            return

        enemies = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
        if not enemies:
            st["in_combat"] = False
            self._log("周围安静下来了。")
            self._check_bag_overflow()
            return

        wcfg = combat.equipped_weapon(self.cfg, st)
        shot_meta = None
        shots = 1
        if ranged:
            if not wcfg:
                self._log("你手上是空的。")
                return
            if wcfg.get("kind") != "ranged":
                self._log("这不是枪。")
                return
            # P9 弹夹系统：弹药从武器弹匣供给（装填时从背包压入），不再直读背包
            clip_ammo = (st.get("weapon") or {}).get("clip_ammo")
            clip_count = int((st.get("weapon") or {}).get("clip_count") or 0)
            if not clip_ammo or clip_count <= 0:
                self._log("弹夹空了——需要装填弹药。（随身面板点武器的「装填」）")
                return
            atype = clip_ammo
            need = int(wcfg.get("ammo_per_shot", 1))
            # 连射武器（burst）：一次攻击随机射出 N 发，弹匣不足时有多少打多少。
            # 单发/齐射武器（ammo_per_shot ≥ 2）弹匣不足则打不出。
            if wcfg.get("burst"):
                lo, hi = wcfg["burst"]
                shots = self.rng.randint(int(lo), int(hi))
                shots = min(shots, clip_count)
            else:
                shots = need
                if clip_count < need:
                    self._log(f"弹夹弹药不足（{clip_count}/{need}）——需要装填。")
                    return
            st["weapon"]["clip_count"] = clip_count - shots
            # 弹药品质：已装填弹种的伤害百分比（resolve_attack 内乘算）
            shot_meta = {"dmg_mult": float(
                self.cfg.item(atype).get("dmg_mult", 1.0) or 1.0
            )}
            # 噪音按一次攻击算一次——扫射再密，动静也只是一轮枪声
            noise.add(self.cfg, st, wcfg.get("noise_key", "gunshot"))
        else:
            # 拳头兜底：持枪或空手也能近战（默认近战武器拳头）。
            # 挥拳不算武器磨损——拳头无耐久概念，枪也不该被徒手挥坏。
            if not wcfg or wcfg.get("kind") != "melee":
                wcfg = self.cfg.item("fists")
            else:
                self._damage_weapon(1)

        # 远程命中率由天赋 / buff / 武器 acc_mod 决定，与体力无关（设计红线）。
        pp = combat.player_profile(
            self.cfg, st, weapon=None if ranged else wcfg
        )

        # 霰弹枪等 aoe 武器：一次齐射对所有敌人单独结算（各自独立命中/闪避/暴击）
        aoe = bool(ranged and wcfg.get("aoe"))

        if aoe:
            alive = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
            if not alive:
                st["in_combat"] = False
                self._log("周围安静下来了。")
                await self._enemy_round()
                return
            self._log("你扣下扳机，霰弹横扫向所有敌人！")
            for enemy in alive:
                await self._player_hit_one(enemy, pp, ranged, shot_meta)
        elif shots > 1:
            # 连射：每发独立 roll 命中与伤害（复用单敌结算）。当前目标倒下后
            # 剩余发数自动转向下一个敌人——扫射不看弹匣里的仇恨。
            self._log(f"你扣住扳机扫射，{wcfg['name']}倾泻出 {shots} 发弹药！")
            for _ in range(shots):
                alive = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
                if not alive:
                    break
                await self._player_hit_one(alive[0], pp, ranged, shot_meta)
        else:
            target = payload.get("target")
            enemy = enemies[0]
            if isinstance(target, int) and 0 <= target < len(st["combat"]["enemies"]):
                cand = st["combat"]["enemies"][target]
                enemy = cand if cand["hp"] > 0 else enemy
            await self._player_hit_one(enemy, pp, ranged, shot_meta)

        await self._enemy_round()

    async def _player_hit_one(
        self, enemy: dict, pp: dict, ranged: bool, meta: dict | None = None
    ) -> None:
        """对单个敌人结算一次玩家攻击（命中/闪避/暴击各自独立）。

        meta：attacker_meta 直透 resolve_attack（P9 弹药 dmg_mult 走这里）。
        """
        st = self.state
        ep = combat.enemy_profile(self.cfg, enemy)
        res = combat.resolve_attack(
            self.cfg, self.rng, pp, ep, attacker_meta=meta
        )
        if not res["hit"]:
            self._log(f"你扑了个空。{enemy['name']}擦着你的手滑了过去。")
            return
        enemy["hp"] -= res["dmg"]
        verb = "开枪命中" if ranged else "砸中"
        if res["crit"]:
            self._log(f"** 暴击 ** 你{verb}{enemy['name']}，造成 {res['dmg']} 点伤害。")
        else:
            self._log(f"你{verb}{enemy['name']}，造成 {res['dmg']} 点伤害。")
        if enemy["hp"] <= 0:
            await self._kill_enemy(enemy, ranged)

    def _damage_weapon(self, amount: int) -> None:
        st = self.state
        w = st.get("weapon")
        if not w or w.get("durability") is None:
            return
        w["durability"] = max(0, int(w["durability"]) - amount)
        if w["durability"] == 0:
            self._log(f"{self.cfg.item(w['id'])['name']}快断了，挥起来没多少力道。")

    # ------------------------------------------------------------------
    # 背包容量系统（先拿后丢）
    #
    # 单格 = 一个物品条目。可堆叠物资（弹药/绷带/材料/纪念品）只占 1 格，
    # qty 累加不占新格，天然限制"无限囤积"。总容量 = 基础 + 天赋 + 背包 slots + 护甲口袋。
    # 获取（搜索/掉落/买卖/墓碑拾取）总是直接入包——允许暂时超过上限；
    # 但只要格数超过容量，就暂停为 bag_overflow 决策：必须丢到容量以内才能继续任何行动。
    # 换上更小装备导致容量缩水时，同样触发 bag_overflow。
    # ------------------------------------------------------------------
    def _bag_cap(self) -> int:
        st = self.state
        cap = int(self.cfg.balance["player"].get("bag_slots", 10))
        cap += int(talents.mod(st, "bag_slots", 0))
        bp = st.get("backpack")
        if bp and bp.get("id"):
            cap += int(self.cfg.item(bp["id"]).get("slots", 0))
        ar = st.get("armor")
        if ar and ar.get("id"):
            cap += int(self.cfg.item(ar["id"]).get("pockets", 0))
        return max(1, cap)

    def _over_capacity(self) -> bool:
        """当前是否超过背包格数上限。"""
        return len(self.state["inventory"]) > self._bag_cap()

    def _check_bag_overflow(self) -> bool:
        """超容量（含换装缩水）时暂停为 bag_overflow 决策：丢到装得下为止。

        战斗中延后：杀怪掉落/战斗换装会超容量，但战斗里弹整理背包会锁住
        攻击/逃跑——先拿着打，等战斗结束再弹（_enemy_round 清场分支、
        _act_flee 成功处补查）。另外只在空闲时触发，绝不覆盖正在显示的
        其他决策（天赋三选一/尸化/墓碑……被覆盖即丢失，比延后严重）。
        """
        st = self.state
        if st.get("in_combat"):
            return False
        if self._over_capacity() and st.get("pending_decision") is None:
            st["pending_decision"] = "bag_overflow"
            self._log("背包塞得太满了——先丢掉一些东西，才能继续行动。")
            return True
        return False

    def _acquire(self, item_id: str, qty: int = 1, durability: int | None = None) -> bool:
        """入包的唯一入口：先拿后丢——总是直接 grant（允许暂时超上限），
        超了就暂停为 bag_overflow 决策。返回是否成功入包。
        现金走独立计数，不占格、永不触发超载。
        捡到背包类物品自动装备：没背包装上；有背包且新的更大也自动换——
        否则玩家捡到背包反而先被"超上限丢东西"逼着做无意义决策。"""
        st = self.state
        if item_id == "cash":
            loot.grant(self.cfg, st, "cash", qty)
            return True
        loot.grant(self.cfg, st, item_id, qty, durability)
        if (
            qty >= 1
            and self.cfg.item_kind(item_id) == "backpack"
        ):
            self._auto_equip_backpack(item_id)
        if self._over_capacity():
            name = self.cfg.item(item_id)["name"]
            self._log(f"你硬把 {name} 塞了进去——背包超载了，得丢掉一些东西才能继续。")
            self._check_bag_overflow()
        return True

    def _auto_equip_backpack(self, item_id: str) -> None:
        """捡到背包的自动装备规则：
        * 身上没有背包 → 直接装上（容量净增，不可能溢出）
        * 已有背包且新的 slots 更大 → 换上，旧背包回背包（容量只增不减）
        * 同样大或更小 → 留在包里由玩家自己决定（避免抢走"留着送人/卖钱"的选择）"""
        st = self.state
        new_slots = int(self.cfg.item(item_id).get("slots", 0))
        cur = st.get("backpack")
        if not cur or not cur.get("id"):
            st["backpack"] = {"id": item_id}
            loot.remove(st, item_id, 1)
            self._log(f"你顺手把{self.cfg.item(item_id)['name']}背上了。（容量 +{new_slots}）")
            return
        cur_slots = int(self.cfg.item(cur["id"]).get("slots", 0))
        if new_slots > cur_slots:
            st["backpack"] = {"id": item_id}
            loot.remove(st, item_id, 1)
            loot.grant(self.cfg, st, cur["id"], 1)
            self._log(
                f"你换上了更大的{self.cfg.item(item_id)['name']}，"
                f"{self.cfg.item(cur['id'])['name']}收进了背包。（容量 {cur_slots}→{new_slots}）"
            )

    def _grave_take(self, uid: str) -> None:
        """从尸体上带走一件：先拿后丢——直接入包，超上限则暂停为 bag_overflow。"""
        st = self.state
        grave = st["room"].get("grave")
        pick = next(
            (g for g in (grave or {}).get("gear", []) if str(g.get("uid")) == str(uid)),
            None,
        )
        if not pick:
            self._log("你没找到那件东西。")
            return
        self._grave_finish_take(uid)
        self._check_bag_overflow()

    def _stamina_max(self) -> int:
        """体力上限 = 配置基础值 + 天赋 stamina_max（P7）。"""
        return int(self.cfg.balance["player"]["stamina"]) + int(
            talents.mod(self.state, "stamina_max", 0)
        )

    def _grave_finish_take(self, uid: str) -> None:
        """真正把尸体上的某件塞进背包，并从尸体移除。"""
        st = self.state
        grave = st["room"].get("grave")
        if not grave:
            st["pending_decision"] = None
            return
        pick = next(
            (g for g in grave.get("gear", []) if str(g.get("uid")) == str(uid)),
            None,
        )
        if not pick:
            st["pending_decision"] = None
            return
        loot.grant(self.cfg, st, pick["id"], 1, pick.get("durability"))
        self._log(f"你带走了 {pick['name']}。")
        # 标记给 API 层落盘（把这件从尸体上真正删掉，避免被后来者重复摸）
        st["_grave_claim"] = {
            "uid": uid,
            "grave_id": grave.get("grave_id"),
            "item_id": pick["id"],
        }
        grave["gear"] = [g for g in grave["gear"] if str(g.get("uid")) != str(uid)]
        st.pop("grave_choices", None)
        st["pending_decision"] = None
        # 尸体已空 → 移出房间，下次进门不再刷这具
        if not grave.get("gear"):
            room = mapgen.current_room(st["level_map"])
            room["resolved"] = True
            st["room"].pop("grave", None)

    def _settle_bag_overflow(self) -> None:
        """用/丢之后若已回到容量以内，解除 bag_overflow 决策（两条路径共用）。

        解除后补结算升级：巢穴割巢等路径会在战斗结束点先占住决策槽
        （溢出先弹），排队的升级从这里唤起，否则滞留到下一场战斗。
        """
        st = self.state
        if st.get("pending_decision") == "bag_overflow" and not self._over_capacity():
            st["pending_decision"] = None
            self._log("背包腾出了空间。")
            self._settle_levelups()

    async def _act_discard(self, payload: dict) -> None:
        """处理 bag_overflow：丢弃物品，直到格数回到容量以内才能继续行动。"""
        st = self.state
        choice = payload.get("choice")

        if choice == "skip":
            self._log("背包还塞不下——你得先丢一些东西。")
            return

        # 丢弃背包里的某件物品（整件/整组移除，确保腾出所占格）
        iid = payload.get("item")
        if not iid:
            self._log("你没指定要丢什么。")
            return
        entry = loot.find_equipment(st, iid) or loot.find_stackable(st, iid)
        if not entry:
            self._log("你身上没有那个。")
            return
        loot.remove(st, iid, entry["qty"])
        self._log(f"你丢掉了 {self.cfg.item(iid)['name']}。")

        # 丢弃后若仍超容量，保持决策继续让玩家丢；回到容量内则解除
        self._settle_bag_overflow()

    async def _kill_enemy(self, enemy: dict, ranged: bool) -> None:
        st = self.state
        st["kills"] += 1
        st["score"] += int(enemy.get("score", 8))
        self._log(f"{enemy['name']}倒下了。")

        # P7 升级系统：XP 累积 + 精英/Boss 直升一级
        self._grant_xp(enemy)
        # 肾上腺素：击杀回血
        heal = int(talents.mod(st, "kill_heal", 0))
        if heal and st["hp"] < st["hp_max"]:
            st["hp"] = min(st["hp_max"], st["hp"] + heal)
            self._log(f"你喘了口气。（HP +{heal}）")

        if ranged:
            noise.add(self.cfg, st, "gun_kill")
        else:
            noise.add(self.cfg, st, "melee_kill")

        # 死亡特效：肿尸爆炸
        od = enemy.get("on_death") or {}
        if "explode" in od:
            ex = od["explode"]
            dmg = self.rng.rand_range_int(ex["damage"] if "damage" in ex else ex["dmg"])
            st["hp"] -= dmg
            inf_old, inf_new = self._add_infection(
                self.rng.rand_value(ex["infection"])
            )
            noise.add(self.cfg, st, ex.get("noise", 3))
            self._log(f"它的身体炸开了！腐臭的液体溅了你一身。（HP −{dmg}，感染 +{inf_new - inf_old}）")

        if enemy.get("boss"):
            st["boss_alive"] = False
            st["score"] += int(self.cfg.balance["scoring"]["boss_kill_bonus"])
            self._log(self.cfg.levels_cfg["boss"]["kill_text"])

        # 掉落
        if self.rng.chance(0.35):
            cat = loot.roll_category(self.cfg, self.rng)
            for iid, qty in loot.roll_loot(self.cfg, self.rng, st, cat, 1):
                self._acquire(iid, qty)
                self._log(f"它身上掉出了 {loot.describe(self.cfg, iid, qty)}。")

    async def _enemy_round(self) -> None:
        st = self.state
        alive = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
        if not alive:
            st["in_combat"] = False
            mapgen.current_room(st["level_map"])["cleared"] = True
            st["room"]["cleared"] = True
            self._log("这一片清干净了。")
            # 巢穴清完 → 割巢拿高价值掉落（P6.2.2）
            self._nest_harvest()
            # 尸潮期间清空战斗 = 消灭一波尸潮：潮水退去 + 噪音按
            # clear_noise_cut 削减（打退追兵后，死寂反而比来之前更彻底）。
            # 仅当清掉的这场**真的有潮兵**——尸潮标记可能在普通战斗中途置位
            # （潮还在路上），清掉普通战斗不该算"打退尸潮"。
            if st.get("horde") and any(
                e.get("horde") for e in st["combat"]["enemies"]
            ):
                noise.cut_after_wave_clear(self.cfg, st)
                cut = float(
                    self.cfg.balance["noise"]["horde"].get("clear_noise_cut", 0)
                )
                self._log("追兵被你打退了，潮水正在退去。")
                if cut > 0:
                    self._log(f"四周安静下来。（噪音 −{round(cut * 100)}%）")
            self._check_horde()
            # P7：战斗结束 → 结算待处理的升级（弹三选一）
            self._settle_levelups()
            # 战斗中掉落/换装欠下的整理：现在才弹（修复战斗中锁操作）
            self._check_bag_overflow()
            return

        for line in level_rules.on_combat_turn(self.cfg, st):
            self._log(line)

        pp = combat.player_profile(self.cfg, st)
        for enemy in alive:
            if enemy["hp"] <= 0:
                continue
            # Boss 技能
            used_ability = False
            for ab in enemy.get("abilities") or []:
                cd = int(ab.get("cd_left", 0))
                if cd > 0:
                    ab["cd_left"] = cd - 1
                    continue
                ab["cd_left"] = int(ab.get("cooldown", 4))
                dmg = self.rng.rand_range_int(ab["dmg"])
                dmg = max(1, dmg - int(pp["armor"]) - int(pp.get("taken_dmg", 0)))
                absorbed = self._apply_armor_absorb(dmg)
                taken = max(0, dmg - absorbed)
                st["hp"] -= taken
                noise.add(self.cfg, st, ab.get("noise", 0))
                if absorbed:
                    self._log(f"{enemy['name']}使出【{ab['name']}】！你受到 {taken} 点伤害（护甲吸收 {absorbed}）。")
                else:
                    self._log(f"{enemy['name']}使出【{ab['name']}】！你受到 {dmg} 点伤害。")
                used_ability = True
                break
            if used_ability:
                continue

            ep = combat.enemy_profile(self.cfg, enemy)
            # P8 扫射（地区 2 远程怪）：burst [min,max] 发连射，每发独立
            # 命中/伤害/护甲吸收——防弹衣按发数吃吸收，对弹幕更有效（用户拍板）。
            # 玩家倒下即刻停止剩余射击。
            burst = enemy.get("burst")
            shots = max(1, self.rng.rand_range_int(burst)) if burst else 1
            hit_shots = 0
            total_taken = 0
            total_absorbed = 0
            bite_infection_total = 0
            for _ in range(shots):
                if st["hp"] <= 0:
                    break
                res = combat.resolve_attack(
                    self.cfg, self.rng, ep, pp,
                    attacker_meta={
                        "bite_chance": enemy.get("bite_chance", 0),
                        "bite_infection": enemy.get("bite_infection", [3, 6]),
                    },
                )
                if not res["hit"]:
                    continue
                hit_shots += 1
                absorbed = self._apply_armor_absorb(res["dmg"])
                taken = max(0, res["dmg"] - absorbed)
                total_taken += taken
                total_absorbed += absorbed
                st["hp"] -= taken
                if "bite" in res["effects"] and res["infection"]:
                    bite_infection_total += int(res["infection"])

            if burst:
                abs_txt = f"（护甲吸收 {total_absorbed}）" if total_absorbed else ""
                self._log(
                    f"{enemy['name']}扣动扳机扫射，{hit_shots}/{shots} 发命中——"
                    f"你受到 {total_taken} 点伤害{abs_txt}。"
                )
            elif hit_shots == 0:
                self._log(f"{enemy['name']}扑空了。")
            else:
                abs_txt = f"（护甲吸收 {total_absorbed}）" if total_absorbed else ""
                self._log(f"{enemy['name']}击中你，造成 {total_taken} 点伤害{abs_txt}。")

            if bite_infection_total:
                old, new = self._add_infection(bite_infection_total)
                self._log(f"它咬了你一口。（感染 +{new - old}）")
                line = inf_mod.describe_change(self.cfg, old, new)
                if line:
                    self._log(line)

            oh = enemy.get("on_hit") or {}
            if oh.get("noise_add") and hit_shots > 0:
                noise.add(self.cfg, st, oh["noise_add"])
                self._log(f"{enemy['name']}尖叫起来，声音传出去很远。（噪音 +{oh['noise_add']}）")

        if st["hp"] <= 0:
            await self._die("被撕碎")
            return

        # 尸化判定
        if st["infection"] >= 100 and not st.get("zombified"):
            st["pending_decision"] = "zombify"
            self._log("感染已经完全占据了你。你可以现在放弃，或者……索性随它去。")
            return

        self._check_horde()

    def _check_horde(self) -> None:
        st = self.state
        if noise.check_horde(self.cfg, st):
            self._log("** 噪音到了临界点，整层都动起来了。**")
            self._log("脚步声从四面八方涌来。尸潮来了。")

    # ------------------------------------------------------------------
    # P7 升级系统（本局内成长，死亡清零）
    #
    # 经验来源：击杀按怪物 xp 配置累积；守门精英 / Boss 击杀**直接**触发
    # 一次三选一（不走 XP 条——精英奖励要有即时仪式感）。
    # 普通怪 XP 攒满 xp_next() → 也触发三选一。
    # 结算时机：pending_levelups 在战斗清空后统一弹出（不打断战斗节奏）。
    # ------------------------------------------------------------------
    def _growth_cfg(self) -> dict:
        return self.cfg.balance.get("growth") or {}

    def _xp_next(self) -> int:
        """升到下一级所需的 XP（首级 xp_base，每级 ×xp_curve）。"""
        g = self._growth_cfg()
        base = float(g.get("xp_base", 60))
        curve = float(g.get("xp_curve", 1.4))
        lvl = int(self.state.get("growth_level", 0))
        return max(1, int(round(base * (curve ** lvl))))

    def _grant_xp(self, enemy: dict) -> None:
        st = self.state
        g = self._growth_cfg()
        if not g:
            return
        xp = int(enemy.get("xp", 0))
        if xp <= 0:
            return
        # 精英/Boss：直接升 1 级（承诺兑现：打赢精英获得一个额外天赋），不走 XP 条
        if enemy.get("elite") or enemy.get("boss"):
            st["pending_levelups"] = int(st.get("pending_levelups", 0)) + 1
            self._log("肾上腺素仍在翻涌——你感到自己变强了。")
            return
        # 普通怪：攒条升级（可能连升）
        st["xp"] = int(st.get("xp", 0)) + xp
        while st["xp"] >= self._xp_next():
            st["xp"] -= self._xp_next()
            st["growth_level"] = int(st.get("growth_level", 0)) + 1
            st["pending_levelups"] = int(st.get("pending_levelups", 0)) + 1
            self._log("** 你变强了。**")

    def _settle_levelups(self) -> None:
        """战斗清空后结算待处理的升级：弹出三选一（一次弹一个，选完再弹）。

        L5（撤离层）不弹：都打到撤离点了，击杀暴君的意义就是"能上直升机"，
        撤离前再涨一级天赋改变不了什么，反而打乱"撤离=满状态收官"的节奏。
        待处理的升级计数清零（XP/等级照常累计，只是不触发抽取）。
        """
        st = self.state
        # P8 地区化：撤离层特判按「所在地区最后一层」——地区 1 的 L5 撤离点
        # 同样不弹天赋（max_level=10 后旧判定会漏掉它）
        if st["depth"] == self.cfg.last_level_of(st["depth"]) and st.get("boss_alive") is False:
            if int(st.get("pending_levelups", 0)) > 0:
                st["pending_levelups"] = 0
                self._log("你已经站在撤离点了——现在想这些没什么用。")
            return
        if int(st.get("pending_levelups", 0)) <= 0:
            return
        if st.get("pending_decision"):  # 已有别的决策在排队
            return
        if self._draw_talents(reason="levelup"):
            st["pending_levelups"] = int(st.get("pending_levelups", 0)) - 1

    async def _act_flee(self, payload: dict) -> None:
        st = self.state
        if not st.get("in_combat"):
            self._log("没什么好逃的。")
            return
        alive = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
        # 守门精英不可逃跑：楼梯口就一条路，绕是绕不过去的
        if any(e.get("elite") for e in alive):
            self._log("它堵着楼梯口——身后就是绝路，你没地方可退。")
            return
        # Boss 不可逃跑（用户报告的软锁）：暴君/葬列守着撤离点，战斗只在
        # 进房时触发——逃掉就再也没入口，对局直接卡死。正面击杀是唯一解。
        if any(e.get("boss") for e in alive):
            self._log("它堵在你和直升机之间。这条路上，没有退路。")
            return
        fastest = max((e.get("speed", 5) for e in alive), default=5)
        agi = combat.player_agility(st) + int(talents.mod(st, "flee_bonus", 0)) // 5
        # 敌人越多越难脱身：每只额外敌人 −2%（flee_per_enemy，可配）
        enemy_count = len(alive)
        # 体力加成按扣减前的当前体力计：体力越满越容易逃掉（每点 +0.5%，可配）
        cost = int(self.cfg.balance["combat"].get("flee_stamina_cost", 0))
        ok = combat.try_flee(
            self.cfg, self.rng, agi, fastest,
            stamina=st["stamina"], enemy_count=enemy_count,
        )
        # 逃跑=冲刺：无论成败都耗体力（跑成了甩掉它们，跑输了也在拼命跑）
        if cost:
            st["stamina"] = max(0, st["stamina"] - cost)
        if ok:
            st["in_combat"] = False
            noise.add(self.cfg, st, "sprint")
            self._log(f"你转身就跑，把它们甩在了身后。（噪音 +2，体力 −{cost}）" if cost
                      else "你转身就跑，把它们甩在了身后。（噪音 +2）")
            self._check_horde()
            # 战斗中欠下的整理（掉落/换装超容量）：逃掉了也逃不掉整理背包
            self._check_bag_overflow()
        else:
            self._log(f"你没能甩掉它们。（体力 −{cost}）" if cost else "你没能甩掉它们。")
            await self._enemy_round()

    async def _act_repair(self, payload: dict) -> None:
        """非商人区域的修理：用废料（或现金）逐点修装备。

        战斗中不可修；商人房间走 merchant.repair（同一套换算）。
        """
        st = self.state
        if st.get("in_combat"):
            self._log("它们就在眼前——现在可不是修武器的时候。")
            return
        iid = payload.get("item")
        pay = payload.get("pay", "scrap")
        ok, msg = self._do_repair(iid, pay)
        self._log(msg)

    def _mag_size(self, wcfg: dict) -> int:
        """武器弹匣容量（配置 mag_size × 扩容弹匣天赋 mag_size_mult）。"""
        base = int(wcfg.get("mag_size", 0) or 0)
        if not base:
            return 0
        mult = float(talents.mod(self.state, "mag_size_mult", 1.0) or 1.0)
        return max(1, int(math.floor(base * mult + 0.5)))

    def _clip_state(self) -> tuple[dict | None, dict | None, int, str | None, int]:
        """当前武器与弹匣状态：(武器实例, 武器配置, 弹匣现有数, 弹种 id, 弹匣容量)。"""
        st = self.state
        w = st.get("weapon") or {}
        wcfg = combat.equipped_weapon(self.cfg, st) or {}
        size = self._mag_size(wcfg)
        count = int(w.get("clip_count") or 0)
        return (w if w else None), wcfg, count, w.get("clip_ammo"), size

    async def _act_reload(self, payload: dict) -> None:
        """P9 弹匣装填：从背包把子弹压进当前武器弹匣。

        - 装填量 = min(容量 − 现有, 背包存量)；弹匣只装同一种子弹
        - 换弹种时旧弹退回背包（用户拍板），再装新弹到上限
        - 战斗中调用（正常行动流）会推进回合并触发敌人回合——装填要时间；
          非战斗由 act() 的 reload 分支直接进来，不推进任何计时
        """
        st = self.state
        w, wcfg, cur, cur_ammo, size = self._clip_state()
        if not w or not wcfg or wcfg.get("kind") != "ranged":
            self._log("手上没有需要装填的枪。")
            return
        if not size:
            self._log("这把枪没有弹匣结构。")
            return
        ammo_id = payload.get("ammo")
        if not ammo_id:
            self._log("没有选择要装填的弹药。")
            return
        if self.cfg.item_kind(ammo_id) != "ammo":
            self._log("那不是能装进弹夹的东西。")
            return
        if cur > 0 and cur_ammo and cur_ammo != ammo_id:
            # 异类换弹：弹匣里旧弹退回背包（用户拍板），再装新弹
            loot.grant(self.cfg, st, cur_ammo, cur)
            self._log(f"弹匣里剩下的{self.cfg.item(cur_ammo)['name']}退回了背包。")
            cur = 0
        owned = loot.count(st, ammo_id)
        if owned <= 0:
            self._log(f"你没有{self.cfg.item(ammo_id)['name']}。")
            return
        load = min(size - cur, owned)
        if load <= 0:
            self._log("弹夹已经满了。")
            return
        loot.remove(st, ammo_id, load)
        w["clip_ammo"] = ammo_id
        w["clip_count"] = cur + load
        self._log(
            f"你把 {load} 发{self.cfg.item(ammo_id)['name']}压进弹匣。"
            f"（弹匣 {w['clip_count']}/{size}）"
        )
        if st.get("in_combat"):
            # 快速装填天赋：装填不触发敌人回合（不耗费这 1 回合的反击）
            if not int(talents.mod(st, "reload_free", 0)):
                await self._enemy_round()

    async def _act_use(self, payload: dict) -> None:
        st = self.state
        cfg = self.cfg
        iid = payload.get("item")
        if not iid or loot.count(st, iid) <= 0:
            self._log("你没有那个东西。")
            return
        item = self.cfg.item(iid)
        if self.cfg.item_kind(iid) != "consumable":
            self._log(f"{item['name']}不是能直接用的东西。")
            return

        loot.remove(st, iid, 1)
        parts: list[str] = [f"你用了{item['name']}。"]

        # 防弹插板（P8 地区 2 专属）：插进防弹衣类护甲 +20 耐久、无上限惩罚
        if item.get("armor_plate"):
            armor = st.get("armor") or {}
            if not armor.get("id"):
                loot.grant(cfg, st, iid, 1)
                self._log("你身上没穿护甲——插板没地方安。")
                return
            acfg = cfg.item(armor["id"])
            if not acfg.get("plate_compatible"):
                loot.grant(cfg, st, iid, 1)
                self._log(f"{acfg['name']}装不了防弹插板——只有防弹衣类的甲面吃这个。")
                return
            maxd = self._armor_max(armor)
            before = int(armor.get("durability") or 0)
            after = min(before + int(item["armor_plate"]), maxd)
            armor["durability"] = after
            self._log(
                f"你把防弹插板压进{acfg['name']}的甲面。（耐久 {before}→{after}，"
                "插板不伤甲——上限不变）"
            )
            if st.get("in_combat"):
                await self._enemy_round()
            return

        if item.get("heal"):
            amount = self.rng.rand_range_int(item["heal"])
            before = st["hp"]
            st["hp"] = min(st["hp_max"], st["hp"] + amount)
            parts.append(f"HP +{st['hp'] - before}")
        if item.get("heal_stamina"):
            before = st.get("stamina", 0)
            st["stamina"] = min(
                self._stamina_max(),
                before + int(item["heal_stamina"]),
            )
            parts.append(f"体力 +{st['stamina'] - before}")
        if item.get("infection"):
            amount = int(item["infection"])
            # 铁胃类天赋：抑制类消耗品（负感染）每份多减 food_infection_bonus。
            # 只放大削减、不动正感染（伏特加 +3 的代价不因天赋消失）。
            if amount < 0:
                amount -= int(talents.mod(st, "food_infection_bonus", 0))
            old, new = self._add_infection(amount)
            delta = new - old
            parts.append(f"感染 {delta:+d}")
            line = inf_mod.describe_change(self.cfg, old, new)
            if line:
                parts.append(line)
        if item.get("flashlight"):
            st["flashlight"] = min(100, int(st.get("flashlight") or 0) + int(item["flashlight"]))
            parts.append(f"手电电量 +{item['flashlight']}")
        if item.get("buff"):
            b = dict(item["buff"])
            b["name"] = item["name"]
            st["buffs"].append(b)
            parts.append(f"获得增益（{b.get('turns', 0)} 回合）")

        self._log("".join(parts))

        if st.get("in_combat"):
            await self._enemy_round()

    async def _act_brace(self, payload: dict) -> None:
        """战斗中消耗体力"瞄准"，换取临时命中加成。

        设计红线：绝不对命中做任何体力惩罚——低体力只是用不了瞄准，
        绝不降低基础命中。命中加成走已有的 buff 系统（combat.player_profile 已读取）。
        """
        st = self.state
        if not st.get("in_combat"):
            self._log("现在没东西需要你瞄准。")
            return
        cost = int(self.cfg.balance["combat"].get("brace_stamina_cost", 0))
        bonus = int(self.cfg.balance["combat"].get("brace_acc_bonus", 0)) + int(
            talents.mod(st, "brace_acc_bonus_add", 0)
        )
        turns = int(self.cfg.balance["combat"].get("brace_turns", 3))
        if st.get("stamina", 0) < cost:
            self._log("体力不够，没法稳住准星。")
            return
        st["stamina"] = max(0, st["stamina"] - cost)
        # 瞄准是免费动作：刷新（而非叠加）已有的「瞄准」buff，避免反复瞄准无限堆命中。
        st["buffs"] = [b for b in st["buffs"] if b.get("name") != "瞄准"]
        st["buffs"].append({"name": "瞄准", "acc": bonus, "turns": turns})
        self._log(
            f"你深吸一口气，稳住准星。（体力 −{cost} · 命中 +{bonus} · {turns} 回合）"
        )
        # 关键：瞄准不触发敌人回合（敌人不获得一次行动）。战斗推进交由玩家随后的
        # 攻击 / 射击 / 用道具等真正动作负责——这样瞄准就是纯粹的「预备动作」，不占回合。

    async def _act_equip(self, payload: dict) -> None:
        st = self.state
        iid = payload.get("item")
        entry = loot.find_equipment(st, iid or "")
        if not entry:
            self._log("你没有那个东西。")
            return
        kind = self.cfg.item_kind(iid)
        if kind == "weapon":
            old = st["weapon"]
            st["weapon"] = {
                "id": iid,
                "durability": entry.get("durability"),
                # P9 弹匣状态随武器实例往返（背包里的枪带着已装填的弹匣）
                "clip_ammo": entry.get("clip_ammo"),
                "clip_count": int(entry.get("clip_count") or 0),
            }
            loot.remove(st, iid, 1)
            if old:
                clip = (
                    (old.get("clip_ammo"), int(old.get("clip_count") or 0))
                    if old.get("clip_ammo")
                    else None
                )
                loot.grant(self.cfg, st, old["id"], 1, old.get("durability"), clip)
            self._log(f"你换上了{self.cfg.item(iid)['name']}。")
        elif kind == "armor":
            old = st["armor"]
            st["armor"] = {"id": iid, "durability": entry.get("durability")}
            loot.remove(st, iid, 1)
            if old:
                loot.grant(self.cfg, st, old["id"], 1, old.get("durability"))
            self._log(f"你穿上了{self.cfg.item(iid)['name']}。")
            # 换上护甲可能改变容量（口袋）；缩水导致溢出则暂停
            self._check_bag_overflow()
        elif kind == "backpack":
            old = st.get("backpack")
            st["backpack"] = {"id": iid}
            loot.remove(st, iid, 1)
            if old:
                loot.grant(self.cfg, st, old["id"], 1)
            self._log(f"你换上了{self.cfg.item(iid)['name']}。")
            # 换上更小背包会缩水容量 → 溢出则暂停
            self._check_bag_overflow()
        else:
            self._log("这东西不能装备。")

    async def _act_unequip(self, payload: dict) -> None:
        """卸下已装备的武器/护甲/背包，放回背包。超容量走 bag_overflow 兜底。

        卸背包/护甲会缩水容量——缩水导致超载时同样暂停为 bag_overflow。
        手上没武器（空手）时"卸下"只是回到空手，不产生任何条目。
        """
        st = self.state
        slot = payload.get("slot")
        if slot == "weapon":
            w = st.get("weapon")
            if not w or not w.get("id"):
                self._log("你手上本来就空着。")
                return
            clip = (
                (w.get("clip_ammo"), int(w.get("clip_count") or 0))
                if w.get("clip_ammo")
                else None
            )
            loot.grant(self.cfg, st, w["id"], 1, w.get("durability"), clip)
            st["weapon"] = None
            self._log(f"你收起了{self.cfg.item(w['id'])['name']}。")
        elif slot == "armor":
            a = st.get("armor")
            if not a or not a.get("id"):
                self._log("你身上没穿护甲。")
                return
            loot.grant(self.cfg, st, a["id"], 1, a.get("durability"))
            st["armor"] = None
            self._log(f"你脱下了{self.cfg.item(a['id'])['name']}。")
            # 脱甲缩水容量（口袋消失）→ 溢出则暂停
            self._check_bag_overflow()
        elif slot == "backpack":
            bp = st.get("backpack")
            if not bp or not bp.get("id"):
                self._log("你本来就没背背包。")
                return
            loot.grant(self.cfg, st, bp["id"], 1)
            st["backpack"] = None
            self._log(f"你放下了{self.cfg.item(bp['id'])['name']}。")
            # 卸背包缩水容量 → 溢出则暂停
            self._check_bag_overflow()
        else:
            self._log("没这个槽位可卸。")

    async def _act_event(self, payload: dict) -> None:
        st = self.state
        room = mapgen.current_room(st["level_map"])
        ev_id = room.get("event")
        if not ev_id:
            self._log("这里没有要处理的事。")
            return
        ev = next((e for e in self.cfg.events if e["id"] == ev_id), None)
        if not ev:
            room["resolved"] = True
            return

        choice_id = payload.get("choice")
        choice = next((c for c in ev["choices"] if c["id"] == choice_id), None)
        if not choice:
            self._log("你犹豫了。")
            return

        if choice.get("require_pry") and not (
            self.cfg.item(st["weapon"]["id"]).get("pry") if st["weapon"] else False
        ):
            self._log("你需要能撬开它的东西。")
            return

        room["resolved"] = True
        st["room"].pop("event", None)

        if choice.get("noise"):
            noise.add(self.cfg, st, int(choice["noise"]))

        outcome = self.rng.weighted_choice(
            choice["outcomes"], [o["p"] for o in choice["outcomes"]]
        )
        self._log(outcome.get("text", ""))

        if outcome.get("infection"):
            delta = self.rng.rand_value(outcome["infection"])
            old, new = self._add_infection(delta)
            self._log(f"（感染 {new - old:+d}）")
        if outcome.get("cut"):
            dmg = self.rng.rand_value(outcome["cut"])
            st["hp"] -= dmg
            self._log(f"（HP −{dmg}）")
        if outcome.get("heal"):
            h = self.rng.rand_value(outcome["heal"])
            before = st["hp"]
            st["hp"] = min(st["hp_max"], st["hp"] + h)
            self._log(f"（HP +{st['hp'] - before}）")
        if outcome.get("humanity"):
            st["humanity"] += int(outcome["humanity"])
        if outcome.get("noise"):
            noise.add(self.cfg, st, int(outcome["noise"]))
        if outcome.get("item"):
            name = self.cfg.item(outcome["item"])["name"]
            self._acquire(outcome["item"], 1)
            self._log(f"获得 {name}。")
        if outcome.get("loot_category"):
            for iid, qty in loot.roll_loot(
                self.cfg, self.rng, st, outcome["loot_category"], 1
            ):
                self._acquire(iid, qty)
                self._log(f"获得 {loot.describe(self.cfg, iid, qty)}。")
        if outcome.get("spawn"):
            count = int(outcome.get("count", 1))
            enemies = [
                combat.make_enemy(self.cfg, outcome["spawn"], st["depth"])
                for _ in range(count)
            ]
            st["combat"] = {"enemies": enemies, "round": 0}
            st["in_combat"] = True
            self._log(f"{enemies[0]['name']}出现了。")

        if st["hp"] <= 0:
            await self._die("失血过多")
            return
        self._check_horde()

    async def _act_grave(self, payload: dict) -> None:
        """遗骸交互（P4）：搜刮 → 从尸体上只挑「一件」带走 / 掩埋 / 离开。

        一具尸体最多被 3 个不同玩家各摸走一件（claim_cap），其余留在尸上留给后来的人。
        真正删数据库那件靠 API 层的 _persist_grave_effects，引擎只在这里下标记位，
        保持引擎纯逻辑、可被模拟器直接驱动。
        """
        st = self.state
        room = mapgen.current_room(st["level_map"])
        grave = st["room"].get("grave")
        if not grave:
            self._log("这里没有遗体。")
            return
        act = payload.get("choice")

        # 进入"选一件"决策
        if act == "loot":
            if not grave.get("gear"):
                noise.add(self.cfg, st, 1)
                self._log(f"{grave.get('player_name', '这具遗体')} 身上已经空空如也。（噪音 +1）")
                return
            st["pending_decision"] = "grave_pick"
            st["grave_choices"] = [dict(g) for g in grave["gear"]]
            names = "、".join(g["name"] for g in grave["gear"])
            self._log(
                f"你蹲下身，从 {grave.get('player_name', '遗体')} 的遗体上翻找——"
                f"只能带走一件：{names}。"
            )
            return

        if act == "bury":
            grave_id = grave.get("grave_id")
            room["resolved"] = True
            st["room"].pop("grave", None)
            st.pop("grave_choices", None)
            old, new = self._add_infection(-5)
            st["humanity"] += 5
            # 交给 API 层把墓碑移出世界池（claim_cap 压到已认领数）
            st["_grave_bury"] = grave_id
            self._log("你用碎石和碎布盖住了他。（感染 −5，人道 +5）")
            return

        if act == "leave":
            self._log("你没去碰他，转身离开。")
            return

        # 决策中的"选一件"：take=带走某件 / skip=什么都不拿
        if act in ("take", "skip"):
            if act == "skip":
                self._log("你什么也没拿，起身离开。")
                st["pending_decision"] = None
                st.pop("grave_choices", None)
                # 选完后尸体已空 → 移出房间，下次进门不再刷这具
                if grave and not grave.get("gear"):
                    room["resolved"] = True
                    st["room"].pop("grave", None)
                return
            uid = payload.get("uid")
            if uid is None:
                uid = payload.get("index")
            pick = next(
                (g for g in (grave or {}).get("gear", []) if str(g.get("uid")) == str(uid)),
                None,
            )
            if not pick:
                self._log("你没找到那件东西。")
                return
            # 先拿后丢：直接带走，超容量则触发 bag_overflow 决策并暂停
            self._grave_take(uid)
            return

        self._log("你不明白自己想对遗体做什么。")

    def _armor_max(self, obj: dict, oid: str | None = None) -> int:
        """护甲实例的耐久上限：修甲每次 −1（实例字段优先），缺省回退配置值。

        旧存档/新拾取的护甲没有 max_durability 字段 → 配置值；有则取实例值
        （只可能比配置低——上限只减不增）。
        """
        oid = oid or obj.get("id")
        cfg_max = int(self.cfg.item(oid).get("durability", 0) or 0) if oid else 0
        inst = obj.get("max_durability")
        if inst is None:
            return cfg_max
        inst = int(inst)
        return min(inst, cfg_max) if cfg_max else inst

    def _repair_target(self, iid: str | None = None):
        """找一个还能修的装备（近战武器 / 护甲），返回 (obj, item_id, max_dur, cur_dur)。

        优先当前装备，其次背包里的；可指定 iid 精确选某件。
        耐久已到当前上限（无可修复）的不会入选；远程无耐久不修。
        """
        st = self.state
        cfg = self.cfg

        def _check(obj, oid):
            if obj is None or oid is None:
                return None
            dur = obj.get("durability")
            if dur is None:
                return None
            # 护甲上限读实例值（修甲会磨上限）；武器维持配置上限
            if cfg.item_kind(oid) == "armor":
                maxd = self._armor_max(obj, oid)
            else:
                maxd = int(cfg.item(oid).get("durability", 0) or 0)
            cur = int(dur)
            if cur < maxd:
                return (obj, oid, maxd, cur)
            return None

        if iid:
            # 前端随身面板的手持/穿戴条目用占位 id（__held_weapon__ 等），
            # 按槽位映射到真实物品——否则"修手上的消防斧"永远匹配不到。
            if iid == "__held_weapon__":
                iid = (st.get("weapon") or {}).get("id")
            elif iid == "__held_armor__":
                iid = (st.get("armor") or {}).get("id")
        if iid:
            if (st.get("weapon") or {}).get("id") == iid:
                r = _check(st["weapon"], iid)
                if r:
                    return r
            # armor 可能为 None（卖出/卸下后残留的修理按钮 id）——不可用 {} 兜底
            if (st.get("armor") or {}).get("id") == iid:
                r = _check(st["armor"], iid)
                if r:
                    return r
            for e in st["inventory"]:
                if e["id"] == iid and e["qty"] > 0:
                    r = _check(e, iid)
                    if r:
                        return r
            return None

        r = _check(st.get("weapon"), (st.get("weapon") or {}).get("id"))
        if r:
            return r
        r = _check(st.get("armor"), (st.get("armor") or {}).get("id"))
        if r:
            return r
        for e in st["inventory"]:
            if e["qty"] > 0 and cfg.item_kind(e["id"]) in ("weapon", "armor"):
                r = _check(e, e["id"])
                if r:
                    return r
        return None

    def _repair_rates(self) -> tuple[float, float]:
        """修理换算：返回 (每点耐久耗废料, 每点耐久耗现金)。天赋 repair_bonus 提高每次修的耐久量。"""
        rcfg = self.cfg.balance.get("merchant", {}).get("repair", {})
        bonus = max(0.0, float(talents.mod(self.state, "repair_bonus", 0.0)))
        return (
            float(rcfg.get("scrap_per_point", 0.3)),
            float(rcfg.get("cash_per_point", 0.5)),
        ), bonus

    @staticmethod
    def _repair_click(per: float) -> tuple[int, int]:
        """把「每点耐久单价」换算成一次点击的 (消耗, 修几点)。

        用户的口径：1 废料 = 修 3 点耐久（0.3/点，0.1 余数舍弃）。
        即单价 ≤1 时一次消耗 1 份资源、修 floor(1/per) 点，不足一整点的零头浪费；
        单价 >1 时维持旧的向上取整（1 点耗 ceil(per) 份）。
        """
        if per <= 0:
            return 1, 1
        if per <= 1.0:
            return 1, max(1, int(1 / per))
        return math.ceil(per), 1

    def _tape_points(self) -> int:
        """胶带修理量：每个胶带修几点（balance.merchant.repair.tape_points，默认 2）。"""
        return int(
            self.cfg.balance.get("merchant", {}).get("repair", {}).get("tape_points", 2)
        )

    def _do_repair(self, iid: str, pay: str) -> tuple[bool, str]:
        """用废铁/现金/胶带修理一件装备，每次点击消耗 1 份资源修几点耐久。

        pay ∈ {"scrap","cash","tape"}。1 废料修 3 点（0.3/点，余数舍弃）；
        1 现金修 2 点（0.5/点）；1 胶带修 2 点（固定点数，胶带是"应急补"的定位）。
        天赋 repair_bonus 让一次多修几点。玩家可以反复点，一点一点把武器修满——
        不再强制一次修到满。非商人区域也可用（前端从随身面板对废料装备发起，
        走同一入口）。
        """
        st = self.state
        cfg = self.cfg
        tgt = self._repair_target(iid)
        if not tgt:
            return False, "没有需要修理的装备。"
        obj, wid, maxd, cur = tgt
        (scrap_per, cash_per), bonus = self._repair_rates()
        if pay == "scrap":
            cur_res, res_name = loot.count(st, "scrap"), "废料"
        elif pay == "tape":
            cur_res, res_name = loot.count(st, "duct_tape"), "胶带"
        else:
            cur_res, res_name = loot.count(st, "cash"), "现金"

        # 这次修几点：整份资源的换算点数 + 天赋奖励；不超过缺失量（零头浪费）
        missing = maxd - cur
        if pay == "tape":
            base_cost, base_points = 1, self._tape_points()
        else:
            base_cost, base_points = self._repair_click(
                scrap_per if pay == "scrap" else cash_per
            )
        cost = base_cost
        points = min(base_points + int(bonus), missing)
        if cur_res < cost or points <= 0:
            return False, (
                f"{res_name}不够——修 1 次要 {cost} {res_name}"
                f"（你只有 {cur_res}）。"
            )
        res_id = {"scrap": "scrap", "tape": "duct_tape", "cash": "cash"}[pay]
        loot.remove(st, res_id, cost)
        new_cur = cur + points
        # 护甲每修一次耐久上限 −1（修起来的每一刀都在伤甲本身）；
        # 溢出钳制：上限回缩吃掉超出的部分（卡着满修那一刀 = 白修，逼你早修）
        if cfg.item_kind(wid) == "armor":
            new_max = max(1, self._armor_max(obj, wid) - 1)
            obj["durability"] = min(new_cur, new_max)
            obj["max_durability"] = new_max
            return True, (
                f"你用 {cost} {res_name} 把{cfg.item(wid)['name']}修了 {points} 点耐久"
                f"（{cur}→{obj['durability']}，耐久上限 {new_max}）。"
            )
        obj["durability"] = new_cur
        return True, (
            f"你用 {cost} {res_name} 把{cfg.item(wid)['name']}修了 {points} 点耐久"
            f"（{cur}→{cur + points}）。"
        )

    def _repair_options(self) -> list[dict]:
        """列出当前可修理的装备（近战武器 / 护甲），含废料与现金两种单价。

        每次点击消耗 1 份资源、修 floor(1/per) 点（1 废料 = 3 点，余数舍弃）。
        前端据此渲染修理按钮并展示换算比例，无需自己读配置算价。
        """
        cfg = self.cfg
        (scrap_per, cash_per), _bonus = self._repair_rates()
        scrap_cost, scrap_pts = self._repair_click(scrap_per)
        cash_cost, cash_pts = self._repair_click(cash_per)
        tape_pts = self._tape_points()
        out: list[dict] = []
        # 当前装备优先，再扫背包里的武器/护甲
        candidates = []
        if st_w := self.state.get("weapon"):
            candidates.append((st_w, st_w.get("id"), st_w.get("durability")))
        if st_a := self.state.get("armor"):
            candidates.append((st_a, st_a.get("id"), st_a.get("durability")))
        for e in self.state["inventory"]:
            if e["qty"] > 0 and cfg.item_kind(e["id"]) in ("weapon", "armor"):
                candidates.append((e, e["id"], e.get("durability")))
        for obj, iid, dur in candidates:
            if iid is None or dur is None:
                continue
            # 护甲上限读实例值（修甲磨上限）；武器维持配置上限
            if cfg.item_kind(iid) == "armor":
                maxd = self._armor_max(obj, iid)
            else:
                maxd = int(cfg.item(iid).get("durability", 0) or 0)
            cur = int(dur)
            if cur < maxd:
                out.append({
                    "id": iid,
                    "name": cfg.item(iid)["name"],
                    "kind": cfg.item_kind(iid),
                    "max": maxd,
                    "cur": cur,
                    # 每次点击的（消耗, 修几点）：1 废料修 3 点，1 现金/胶带修 2 点
                    "scrap_cost": scrap_cost,
                    "cash_cost": cash_cost,
                    "scrap_points": scrap_pts,
                    "cash_points": cash_pts,
                    "tape_points": tape_pts,
                })
        return out

    def _repair_rate_hints(self) -> dict:
        """修理换算的展示口径：每次点击消耗多少资源、可修几点（零头舍弃）。"""
        (scrap_per, cash_per), _bonus = self._repair_rates()
        scrap_cost, scrap_pts = self._repair_click(scrap_per)
        cash_cost, cash_pts = self._repair_click(cash_per)
        return {
            "scrap_cost": scrap_cost,
            "cash_cost": cash_cost,
            "scrap_points": scrap_pts,
            "cash_points": cash_pts,
            "tape_points": self._tape_points(),
        }

    async def _act_merchant(self, payload: dict) -> None:
        """商人交互：随机铺货的买卖、废铁/现金修理、出售换现金、作者怜悯。"""
        st = self.state
        cfg = self.cfg
        room = mapgen.current_room(st["level_map"])
        if room.get("resolved"):
            self._log("商人已经不在了。")
            return
        m = st["room"].get("merchant")
        if not m:
            self._log("这里没有商人。")
            return
        choice = payload.get("choice")

        if choice == "leave":
            room["resolved"] = True
            st["room"]["resolved"] = True  # 镜像同步：响应读的是这里，不同步面板不消失
            st["room"]["merchant_left"] = True
            self._log("你冲商人点了点头，继续往前走。")
            return

        # 感染商人「血税」：不是成交——不标记 merchant_traded（付了血转身走，
        # 优惠留在房间里，回头还能用）。成交标记只属于买/卖/修理/怜悯。
        if choice == "toll":
            if m["type"] != "plagued":
                self._log("它不是那种商人。")
                return
            if m.get("toll_armed"):
                self._log("你已经付过血了——它舔着嘴唇等你挑货。")
                return
            toll = int(m.get("toll_hp", 20))
            if st["hp"] <= toll:
                self._log(f"你的血不够它要的数（需要 {toll} 点以上）。")
                return
            st["hp"] -= toll
            m["toll_armed"] = True
            self._log(
                f"你割开手掌，血顺着它的指缝往下滴。（HP −{toll}）"
                "它满意地嘶笑：「下一件货，半价。」"
            )
            return

        # 任何成交（买/卖/修理/怜悯）后标记：玩家离开房间时商人收摊。
        # 防双向边"成交→出门→再进来"无限刷。实际置 resolved 在 _act_move 离开时。
        st["room"]["merchant_traded"] = True

        # 感染商人规则（用户拍板重做）：
        #   上交 toll_hp 生命 → **下一次购买**半价（一次性，用掉可再交）。
        #   商店按原价铺货；没有门槛、没有每笔抽血、没有"原价惩罚"。
        if m["type"] == "plagued":
            toll = int(m.get("toll_hp", 20))
            discount = float(m.get("discount", 0.5))
        else:
            toll, discount = 0, 1.0

        if choice == "repair":
            iid = payload.get("item")
            pay = payload.get("pay", "scrap")
            ok, msg = self._do_repair(iid, pay)
            self._log(msg)
            return

        if choice == "buy":
            iid = payload.get("item")
            entry = next((s for s in m["shop"] if s["id"] == iid), None)
            if not entry:
                self._log("商人没有这个货。")
                return
            if entry.get("sold"):
                self._log("那件货已经易主了。")
                return
            # 感染商人：上交过血税 → 下一件半价（一次性，用掉即失效）
            cost = int(entry["value"])
            if m["type"] == "plagued" and m.get("toll_armed"):
                cost = max(1, math.ceil(cost * discount))
                m["toll_armed"] = False
                self._log("它按着你还在渗血的手掌收了货款：这件，半价。")
            if loot.count(st, "cash") < cost:
                self._log(
                    f"现金不够——{cfg.item(iid)['name']} 要 {cost}，你只有 {loot.count(st, 'cash')}。"
                )
                return
            # 先拿后丢：直接成交入包（超容量由 bag_overflow 决策兜底）
            self._acquire(iid, 1)
            loot.remove(st, "cash", cost)
            entry["sold"] = True  # 一件商品只卖一次，堵死"反复买同一件"的路
            self._log(f"你花 {cost} 现金换来了 {cfg.item(iid)['name']}。")
            return

        if choice == "sell":
            iid = payload.get("item")
            if not iid or iid == "cash":
                self._log("那个不能卖。")
                return
            if loot.count(st, iid) <= 0:
                self._log("你没有那个东西。")
                return
            kind = cfg.item_kind(iid)
            if kind in ("ammo", "consumable"):
                self._log(f"{cfg.item(iid)['name']}不值当卖。")
                return
            # 该商人名下同款商品槽已成交（买走或卖出过）→ 不再收第二件，
            # 堵死"买折价→卖回收价"的套利循环
            if any(s["id"] == iid and s.get("sold") for s in m.get("shop", [])):
                self._log("它的货架上这件已经有主了，不收第二件。")
                return
            # 耐久门槛：武器/护甲低于总耐久一定比例（默认 50%）拒收——
            # 0 耐久必拒，防止"把打空的装备全卖给商人"变成无本废品回收。
            # 上限以配置 durability 为准（与修理同一权威来源）；无耐久概念
            # 的物品（纪念品/材料等）不受约束。背包里同名装备不合并，取第一件。
            maxd = int(cfg.item(iid).get("durability", 0) or 0)
            if kind in ("weapon", "armor") and maxd > 0:
                min_ratio = float(
                    cfg.balance.get("merchant", {}).get("min_durability_ratio", 0.5)
                )
                entry = next(
                    (e for e in st["inventory"] if e["id"] == iid), None
                )
                cur = int(entry.get("durability") or 0) if entry else 0
                if cur / maxd < min_ratio:
                    pct = int(min_ratio * 100)
                    self._log(
                        f"它掂了掂{cfg.item(iid)['name']}直摇头：磨损太厉害了，"
                        f"修到 {pct}% 以上再来卖。"
                    )
                    return
            value = int(cfg.item(iid).get("value", 1))
            ratio = float(cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
            price = max(1, int(round(value * ratio)))
            # 每次只卖 1 件（此前整组堆叠一起卖，既不直观也让回收价失真）
            loot.remove(st, iid, 1)
            loot.grant(cfg, st, "cash", price)  # 现金独立计数，不进背包
            # 该商人名下同款商品槽标记为已成交——卖出去的东西不回货架、不能回购，
            # 商人也不再收第二件同款（防"买折价→卖回收价"套利）
            for s in m.get("shop", []):
                if s["id"] == iid:
                    s["sold"] = True
            self._log(f"你把 {cfg.item(iid)['name']} 卖了 {price} 现金。")
            return

        if choice == "mercy":
            if not m.get("authors_mercy"):
                self._log("今天没有这种好事。")
                return
            if m.get("mercy_taken"):
                self._log("你已经拿过他送的东西了。")
                return
            iid = payload.get("item")
            entry = next((s for s in m["shop"] if s["id"] == iid), None)
            if not entry:
                self._log("他没那件货。")
                return
            self._acquire(iid, 1)
            m["mercy_taken"] = True
            self._log(f"他大手一挥：{cfg.item(iid)['name']} 归你了，算我请的。")
            return

        self._log("你不明白自己想跟商人做什么。")

    async def _act_campfire(self, payload: dict) -> None:
        st = self.state
        room = mapgen.current_room(st["level_map"])
        if room.get("special_kind") != "campfire":
            self._log("这里没有火。")
            return
        if st.get("campfire_used"):
            self._log("火已经灭了。")
            return
        tpl = self.cfg.room_template("special", "campfire")
        st["campfire_used"] = True
        before = st["hp"]
        st["hp"] = min(st["hp_max"], st["hp"] + int(tpl.get("heal", 0)))
        old, new = self._add_infection(int(tpl.get("infection", 0)))
        sta_before = st.get("stamina", 0)
        st["stamina"] = min(
            self._stamina_max(), sta_before + int(tpl.get("stamina", 0))
        )
        # 每个效果都要有可见反馈——漏了体力的汇报，玩家会以为"加了但没生效"（罐头同款教训）
        parts = [f"HP +{st['hp'] - before}", f"感染 {new - old}"]
        sta_gain = st["stamina"] - sta_before
        if sta_gain:
            parts.append(f"体力 +{sta_gain}")
        self._log(f"你在火边坐了一会。（{'，'.join(parts)}）")

        if self.rng.chance(float(tpl.get("ambush_chance", 0))):
            enemies = [combat.make_enemy(self.cfg, "walker", st["depth"]) for _ in range(2)]
            st["combat"] = {"enemies": enemies, "round": 0}
            st["in_combat"] = True
            self._log("火光把东西引来了。")

    async def _act_descend(self, payload: dict) -> None:
        st = self.state
        room = mapgen.current_room(st["level_map"])
        if room.get("special_kind") != "stairs":
            self._log("这里没有楼梯。")
            return
        if st.get("in_combat"):
            # 守门精英给出针对性提示，普通战斗维持原样
            if self._elite_guard_active():
                self._log("它挡在楼梯口。不解决它，你连一级台阶都下不去。")
            else:
                self._log("有东西挡在路上。")
            return
        self._log_many(await self._descend())

    async def _act_evac(self, payload: dict) -> None:
        st = self.state
        if st["depth"] != self.cfg.last_level_of(st["depth"]):
            self._log("这里不是撤离点。")
            return
        if st.get("boss_alive") and st.get("boss_lured", 0) <= 0:
            self._log("它还站在那里。你上不去。")
            return
        st["score"] += int(self.cfg.balance["scoring"]["escape_bonus"])
        st["score"] += int(st.get("ammo_total", 0)) * int(
            self.cfg.balance["scoring"]["ammo_score"]
        )
        st["score"] += int(loot.count(st, "wedding_ring") + loot.count(st, "dog_tag")
                           + loot.count(st, "photo")) * int(
            self.cfg.balance["scoring"]["trinket_score"]
        ) * float(talents.mod(st, "trinket_score_mult", 1.0))
        # 撤离成功即脱离倒计时——换区选择界面不能再被倒计时追杀
        st["evac_countdown"] = None
        rid = self.cfg.region_id_for_level(st["depth"])

        if rid < self.cfg.max_region:
            # P8：中间地区的撤离 = 换区继续（对局不结束）。
            # status 保持 active，走 evac_carry 待决策（选 3 件带装进下一地区）；
            # 继承码/地区进度/解锁广播由 API 层在 act 后按 region_clear_pending 发放。
            st["region_clear_pending"] = rid
            st["pending_decision"] = "evac_carry"
            self._log("* 你抓住起落架下的货梯把手，被拉上了运输直升机。 *")
            self._log(f"** 地区 {rid} 撤离成功。（+撤离分，继承码稍后发放） **")
            self._log("直升机调头向南。舱门再打开时，就该跳下去了——")
            self._log("带不走的都得留下。选好你的三样东西：")
            return

        st["status"] = "escaped"
        self._log("* 你抓住起落架，被拉进了机舱。城市在下面越来越小。 *")
        self._log(f"** 撤离成功。最终得分 {st['score']} **")
        st["pending_decision"] = "legacy"
        st["legacy_choices"] = self._legacy_candidates(escaped=True)
        if st["legacy_choices"]:
            self._log("你能带走的只有一样。选一个：")
        raise RunEnded("escaped")

    async def _act_lure(self, payload: dict) -> None:
        """制造噪音引开 Boss——已下线：暴君对噪音免疫，必须正面击杀。

        保留方法仅为老存档兼容（旧客户端可能还有按钮）；直接说明并消耗回合。
        """
        st = self.state
        if not st.get("boss_alive"):
            self._log("它已经不在了。")
            return
        self._log(
            "你砸碎玻璃、用力敲打栏杆——它连头都没回。"
            "噪音对它毫无意义。想过去，只有从它身上踏过去这一条路。"
        )

    async def _act_status(self, payload: dict) -> None:
        st = self.state
        band = inf_mod.band_name(self.cfg, st["infection"])
        self._log(
            f"HP {st['hp']}/{st['hp_max']} · 感染 {st['infection']}%"
            + (f"（{band}）" if band else "")
            + f" · 噪音 {noise.value(st):.1f}/10"
        )
        # 文本版背包。纪念品/材料/弹药不会生成操作按钮，
        # 不给一份文字清单的话，命令行玩家完全看不到自己有什么。
        if st.get("weapon"):
            w = st["weapon"]
            wear = f"（耐久 {w['durability']}）" if w.get("durability") is not None else ""
            self._log(f"  武器：{self.cfg.item(w['id'])['name']}{wear}")
        if st.get("armor"):
            self._log(f"  护甲：{self.cfg.item(st['armor']['id'])['name']}")

        order = {"weapon": 0, "armor": 1, "consumable": 2, "trinket": 3, "material": 4, "ammo": 5}
        bag = sorted(
            (e for e in st["inventory"] if e["qty"] > 0),
            key=lambda e: (order.get(self.cfg.item_kind(e["id"]), 9),
                           self.cfg.item(e["id"])["name"]),
        )
        if bag:
            parts = []
            for e in bag:
                nm = self.cfg.item(e["id"])["name"]
                parts.append(f"{nm}×{e['qty']}" if e["qty"] > 1 else nm)
            self._log("  随身：" + "、".join(parts))
        else:
            self._log("  随身：空空如也")

        # 天赋效果：选中后只在顶部 UI 显示名字，玩家很容易忘掉具体加成，
        # 这里把效果明文打出来，配合顶部 chip 的悬停提示双保险。
        ts = talents.summary(st)
        if ts:
            for t in ts:
                self._log(f"  天赋：{t['name']} —— {t['desc']}")

        # 当前生效的临时增益（瞄准等），让玩家清楚自己这回合的命中加成从哪来
        if st.get("buffs"):
            active = [b for b in st["buffs"] if b.get("turns", 0) > 0]
            if active:
                bits = "、".join(
                    f"{b['name']}（{b['turns']}回合"
                    + (f" · 命中 +{b['acc']}" if b.get("acc") else "")
                    + "）"
                    for b in active
                )
                self._log(f"  增益：{bits}")

    async def _act_give_up(self, payload: dict) -> None:
        await self._die("放弃了")

    # ==================================================================
    # 死亡与结算
    # ==================================================================
    def _legacy_blocked(self, escaped: bool = False) -> list[str]:
        """身上有、但因为品质太高而不能带走的物品名。

        撤离成功时 T3 武器可以继承（玩家应得的通关奖励），不再列入 blocked。
        """
        st = self.state
        names: list[str] = []
        ids = []
        if st.get("weapon"):
            ids.append(st["weapon"]["id"])
        if st.get("armor"):
            ids.append(st["armor"]["id"])
        ids += [e["id"] for e in st["inventory"]]
        for iid in ids:
            if self.cfg.item_kind(iid) in ("weapon", "armor") and not self._legacy_allowed(iid, escaped):
                nm = self.cfg.item(iid)["name"]
                if nm not in names:
                    names.append(nm)
        return names

    def _legacy_allowed(self, iid: str, escaped: bool = False) -> bool:
        """遗物资格。撤离成功时放宽：T3 武器也可继承（通关奖励）；
        消耗品/弹药等 excluded kinds 仍然不行。死亡时维持原规则（T3 不可带）。"""
        if escaped and self.cfg.item_kind(iid) == "weapon":
            excluded = set(self.cfg.items_cfg.get("legacy_exclude_kinds") or [])
            return self.cfg.item_kind(iid) not in excluded
        return self.cfg.legacy_allowed(iid)

    def _legacy_candidates(self, escaped: bool = False) -> list[dict]:
        """可选作遗物的物品（单件）。消耗品不在其中；
        死亡时重火力（tier 3）不可选，撤离成功时 T3 武器可选。"""
        st = self.state
        out: list[dict] = []
        seen: set[str] = set()

        def _entry(iid: str, durability: int | None, passes: int) -> None:
            if iid in seen or not self._legacy_allowed(iid, escaped):
                return
            seen.add(iid)
            out.append({
                "id": iid,
                "name": self.cfg.item(iid)["name"],
                "durability": durability,
                "passes": passes,
                "tier": int(self.cfg.item(iid).get("tier", 1)),
            })

        w = st.get("weapon")
        if w:
            _entry(w["id"], w.get("durability"), int(w.get("passes", 0)))
        a = st.get("armor")
        if a:
            _entry(a["id"], None, int(a.get("passes", 0)))
        for e in st["inventory"]:
            # 堆叠物资只出现一个候选（继承也只带一件，qty 不随组带走）
            _entry(e["id"], e.get("durability"), int(e.get("passes", 0)))

        # 普通在前，稀有在后——列表顺序不应该诱导玩家选最强的
        out.sort(key=lambda x: x["tier"])
        return out[:8]

    async def _die(self, cause: str, legacy: dict | None = None) -> None:
        st = self.state
        if st["status"] != "active":
            return
        st["hp"] = 0
        st["in_combat"] = False
        st["death_cause"] = cause

        theme = self.cfg.level_theme(st["depth"])
        narration = await flavor.render(
            "death_narration",
            {"level_name": theme["name"], "level": st["depth"], "death_cause": cause},
            target_id=f"death:{st['turn']}",
            state=st,
            wait=True,
        )
        self._log(narration)
        self._log(f"** 你死了。死因：{cause} **")
        self._log(f"抵达深度 {st['depth']} 层 · 击杀 {st['kills']} · 得分 {st['score']}")

        epitaph = await flavor.render(
            "epitaph",
            {
                "player_name": st.get("player_name", "无名者"),
                "level_name": theme["name"],
                "level": st["depth"],
                "death_cause": cause,
                "infection": st["infection"],
            },
            target_id=f"epitaph:{st['turn']}",
            state=st,
            wait=True,
        )
        st["epitaph"] = epitaph
        st["pending_decision"] = "legacy"
        st["legacy_choices"] = self._legacy_candidates(escaped=False)
        if st["legacy_choices"]:
            self._log("你最后能留下的只有一样东西。选一个：")

        # 状态在这里就写好，不要依赖调用方接住异常后再赋值——
        # 那种写法只要有一处调用方忘了 catch，这一局就永远不会被判定为结束，
        # /api/run/flee 曾经就是这么漏的。RunEnded 只用来做控制流。
        st["status"] = "zombified" if st.get("zombified") else "dead"
        raise RunEnded(st["status"], cause)

    # ==================================================================
    # 响应
    # ==================================================================
    def _response(self) -> dict:
        st = self.state
        band = inf_mod.band_name(self.cfg, st["infection"])
        enemies = [
            {"name": e["name"], "hp": max(0, e["hp"]), "hp_max": e["hp_max"],
             # 战斗列表原始索引：前端点名攻击时回传 target 用
             "idx": i}
            for i, e in enumerate(st.get("combat", {}).get("enemies", []))
            if e["hp"] > 0
        ]
        wcfg = combat.equipped_weapon(self.cfg, st)
        ammo = {
            a["id"]: loot.count(st, a["id"]) for a in self.cfg.items_cfg["ammo"]
        }
        st["ammo_total"] = sum(ammo.values())

        inventory = []
        _sell_ratio = float(self.cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
        _min_dur_ratio = float(
            self.cfg.balance.get("merchant", {}).get("min_durability_ratio", 0.5)
        )
        for e in st["inventory"]:
            item = self.cfg.item(e["id"])
            kind = self.cfg.item_kind(e["id"])
            sellable = kind not in ("ammo", "consumable") and e["id"] != "cash"
            # 商人耐久门槛预判：武器/护甲低于比例上限时前端标"拒收"（预估价保留，
            # 但出售按钮禁用），0 耐久必拒。无耐久概念的物品不受约束。
            _dur_rejected = False
            if sellable and kind in ("weapon", "armor"):
                _maxd = int(item.get("durability", 0) or 0)
                if _maxd > 0:
                    _cur = int(e.get("durability") or 0)
                    _dur_rejected = _cur / _maxd < _min_dur_ratio
            inventory.append({
                "id": e["id"],
                "name": item["name"],
                "qty": e["qty"],
                "kind": kind,
                "wearable": kind in ("weapon", "armor", "backpack"),
                "usable": kind == "consumable",
                "durability": e.get("durability"),
                # 品级（仅武器/护甲/背包有）：前端画 T1-T6 徽标
                "tier": item.get("tier"),
                # P9 弹匣（仅远程武器）：实例弹匣状态随物品往返
                "mag_size": int(item.get("mag_size", 0) or 0) if kind == "weapon" else 0,
                "clip_ammo": e.get("clip_ammo") if kind == "weapon" else None,
                "clip_count": int(e.get("clip_count") or 0) if kind == "weapon" else 0,
                # 护甲实例上限（修甲磨上限）：前端显示 cur/max
                "max_durability": e.get("max_durability") if kind == "armor" else None,
                "desc": _item_desc(item, kind)
                + (
                    f" · 耐久 {int(e['durability'])}/{self._armor_max(e)}"
                    if kind == "armor" and e.get("durability") is not None
                    else ""
                ),
                # 可出售类道具的预估回收价，前端直接展示，不必自己读配置
                "sell": max(1, int(round(int(item.get("value", 1)) * _sell_ratio))) if sellable else None,
                # 耐久低于商人门槛 → 出售按钮禁用并标注拒收原因
                "sell_rejected": _dur_rejected,
            })

        lmap = st.get("level_map", {})
        room = mapgen.current_room(lmap) if lmap else {}
        exits = [
            {"to": e["to"], "label": e["label"]}
            for e in (room.get("exits") if room else [])
        ]

        return {
            "narrative": self._out,
            "patches": flavor.drain(st),
            "state": {
                "status": st["status"],
                "hp": st["hp"],
                "hp_max": st["hp_max"],
                "stamina": st.get("stamina", 0),
                "stamina_max": self._stamina_max(),
                "infection": st["infection"],
                "infection_band": band,
                "noise": round(noise.value(st), 1),
                "noise_max": noise.noise_max(self.cfg, st["depth"]),
                "horde": bool(st.get("horde")),
                "flashlight": st.get("flashlight"),
                "depth": st["depth"],
                "max_depth": self.cfg.max_level,
                "turn": st["turn"],
                "kills": st["kills"],
                "score": st["score"],
                "humanity": int(st.get("humanity", 0)),
                "evac_countdown": st.get("evac_countdown"),
                "zombified": bool(st.get("zombified")),
                "weapon": {
                    "name": wcfg["name"] if wcfg else "空手",
                    "id": wcfg["id"] if wcfg else None,
                    "durability": (st.get("weapon") or {}).get("durability"),
                    "tier": wcfg.get("tier") if wcfg else None,
                    "ranged": bool(wcfg and wcfg.get("kind") == "ranged"),
                    # P9 弹匣：容量/弹种/现有发数（近战为 0）
                    "mag_size": self._mag_size(wcfg) if wcfg else 0,
                    "clip_ammo": (st.get("weapon") or {}).get("clip_ammo"),
                    "clip_count": int((st.get("weapon") or {}).get("clip_count") or 0),
                    "desc": _item_desc(wcfg, "weapon") if wcfg else None,
                },
                # 护甲：名字 + 剩余耐久 + 总耐久。此前只下发名字，玩家看不到
                # 护甲磨损（自行车头盔明明在掉耐久却像永远满的）。
                "armor": (
                    {
                        "id": st["armor"]["id"],
                        "name": self.cfg.item(st["armor"]["id"])["name"],
                        "durability": (st["armor"] or {}).get("durability"),
                        "max_durability": self._armor_max(st["armor"]),
                        "tier": self.cfg.item(st["armor"]["id"]).get("tier"),
                    }
                    if st.get("armor") else None
                ),
                "armor_desc": (
                    _item_desc(self.cfg.item(st["armor"]["id"]), "armor", self._armor_absorb_pct())
                    + f" · 耐久 {(st['armor'] or {}).get('durability')}/{self._armor_max(st['armor'])}"
                    if st.get("armor") else None
                ),
                "backpack": (
                    {
                        "id": st["backpack"]["id"],
                        "name": self.cfg.item(st["backpack"]["id"])["name"],
                        "slots": int(self.cfg.item(st["backpack"]["id"]).get("slots", 0)),
                        "tier": self.cfg.item(st["backpack"]["id"]).get("tier"),
                        "desc": _item_desc(self.cfg.item(st["backpack"]["id"]), "backpack"),
                    }
                    if st.get("backpack") else None
                ),
                "bag_cap": self._bag_cap(),
                "bag_used": len(st["inventory"]),
                "ammo": ammo,
                # P9 装填面板数据：弹种清单（高 tier 在前）+ 背包存量
                "ammo_types": sorted(
                    (
                        {
                            "id": a["id"],
                            "name": a["name"],
                            "tier": int((a["id"].removeprefix("ammo_t") or 0) or 0),
                            "dmg_mult": float(a.get("dmg_mult", 1.0) or 1.0),
                            "count": ammo.get(a["id"], 0),
                        }
                        for a in self.cfg.items_cfg["ammo"]
                    ),
                    key=lambda r: -r["tier"],
                ),
                # 现金独立计数：不进背包；旧局背包里的现金条目向下兼容并入显示
                "cash": loot.count(st, "cash"),
                "scrap": loot.count(st, "scrap"),
                "tape": loot.count(st, "duct_tape"),
                # 修理换算提示：每次修 1 点耐久的单价（向上取整）
                "repair_rates": self._repair_rate_hints(),
                "inventory": inventory,
                "repair_options": self._repair_options(),
                "merchant": (
                    {
                        "type": m["type"],
                        "authors_mercy": bool(m.get("authors_mercy")),
                        "mercy_taken": bool(m.get("mercy_taken")),
                        # 感染商人血税：上交 toll_hp 生命 → 下一件购买半价（一次性）
                        "toll_hp": m.get("toll_hp"),
                        "toll_armed": bool(m.get("toll_armed")),
                        "shop": [
                            {
                                "id": s["id"],
                                "name": self.cfg.item(s["id"])["name"],
                                "kind": s["kind"],
                                "cost": int(s["cost"]),
                                "value": int(s["value"]),
                                "sold": bool(s.get("sold")),
                                "desc": _item_desc(self.cfg.item(s["id"]), s["kind"]),
                            }
                            for s in m.get("shop", [])
                        ],
                    }
                    if (m := st.get("room", {}).get("merchant")) else None
                ),
                "room": {
                    "name": st.get("room", {}).get("name", ""),
                    "type": st.get("room", {}).get("type", ""),
                    "idx": st.get("room", {}).get("idx", 0),
                    "resolved": bool(st.get("room", {}).get("resolved")),
                    "total": lmap.get("total", 0),
                    "searched": st.get("room", {}).get("searched", False),
                    # 灾害房状态（P6.2.2）：倒计时 + 抉择
                    "hazard": st.get("room", {}).get("hazard"),
                    # 幸存者货架（P6.2.2）
                    "npc": (
                        {
                            "stock": [
                                {
                                    "id": s["id"],
                                    "name": self.cfg.item(s["id"])["name"],
                                    "kind": s["kind"],
                                    "cost": int(s["cost"]),
                                    "sold": bool(s.get("sold")),
                                    "desc": _item_desc(self.cfg.item(s["id"]), s["kind"]),
                                }
                                for s in npc["stock"]
                            ],
                            "shared": bool(npc.get("shared")),
                        }
                        if (npc := st.get("room", {}).get("npc")) else None
                    ),
                    "grave": (
                        {
                            "player_name": st["room"]["grave"].get("player_name", "无名者"),
                            "level": st["room"]["grave"].get("level"),
                            "gear": [
                                {
                                    "uid": g["uid"], "name": g["name"], "kind": g.get("kind"),
                                    "desc": _item_desc(self.cfg.item(g["id"]), g.get("kind", "weapon")),
                                }
                                for g in st["room"]["grave"].get("gear", [])
                            ],
                        }
                        if st.get("room", {}).get("grave") else None
                    ),
                },
                "exits": exits,
                "in_combat": bool(st.get("in_combat")),
                "enemies": enemies,
                "buffs": [
                    {
                        "name": b.get("name", "增益"),
                        "acc": int(b.get("acc", 0)),
                        "turns": int(b.get("turns", 0)),
                        "taken_dmg": int(b.get("taken_dmg", 0)),
                    }
                    for b in st.get("buffs", [])
                ],
                "boss_alive": bool(st.get("boss_alive")),
                "level": level_rules.level_brief(self.cfg, st["depth"]),
            },
            "available_actions": self._available_actions(),
            "icons": self._icons(),
            "epitaph": st.get("epitaph"),
            "death_cause": st.get("death_cause"),
            "talent_options": st.get("talent_options"),
            "talent": talents.summary(st),   # 兼容字段（旧前端单天赋）
            "talents": talents.summary(st),  # 多天赋列表（P7 升级系统）
            "xp": {"cur": int(st.get("xp", 0)), "next": self._xp_next(),
                   "level": int(st.get("growth_level", 0))},
            "legacy_choices": [
                {"name": c["name"], "index": i, "tier": c.get("tier", 1),
                 "passes": c.get("passes", 0)}
                for i, c in enumerate(st.get("legacy_choices") or [])
            ],
            # 重火力带不走，得让玩家知道原因（撤离时 T3 武器可带，不算 blocked）
            "legacy_blocked": self._legacy_blocked(escaped=st.get("status") == "escaped"),
            "pending_decision": st.get("pending_decision"),
        }

    def _icons(self) -> dict:
        """本帧涉及的图标映射。前端拿不到配置全量时也能正确渲染。"""
        st = self.state
        cfg = self.cfg
        icons: dict[str, str] = {}
        if st.get("weapon"):
            icons[st["weapon"]["id"]] = cfg.item(st["weapon"]["id"]).get("icon", "")
        if st.get("armor"):
            icons[st["armor"]["id"]] = cfg.item(st["armor"]["id"]).get("icon", "")
        if st.get("backpack"):
            icons[st["backpack"]["id"]] = cfg.item(st["backpack"]["id"]).get("icon", "")
        for e in st["inventory"]:
            icons[e["id"]] = cfg.item(e["id"]).get("icon", "")
        for e in st.get("combat", {}).get("enemies", []):
            icons[e["id"]] = cfg.monster(e["id"]).get("icon", "")
        icons["_level"] = cfg.level_theme(st["depth"]).get("icon", "")
        return icons

    def _available_actions(self) -> list[dict]:
        st = self.state
        acts: list[dict] = []

        # 顺序很重要：**待决策必须先于"本局是否已结束"判断**。
        # 死亡会同时把 status 置为 dead 和 pending_decision 置为 legacy，
        # 如果先判断 status，就会返回空列表——玩家看不到遗物选项、也没有任何按钮，
        # 整局永久卡死（这个 bug 真的发生过）。
        if st.get("pending_decision") == "talent":
            return [
                {"id": "talent", "label": f"{t['name']}｜{t['desc']}",
                 "index": i, "kind": "primary"}
                for i, t in enumerate(st.get("talent_options") or [])
            ]

        if st.get("pending_decision") == "zombify":
            return [
                {"id": "zombify", "label": "随它去（尸变 3 房）", "kind": "danger"},
                {"id": "end", "label": "就到这里", "kind": "primary"},
            ]
        if st.get("pending_decision") == "legacy":
            return [
                {"id": "legacy", "label": f"留下：{c['name']}", "index": i, "kind": "primary"}
                for i, c in enumerate(st.get("legacy_choices") or [])
            ] + [{"id": "legacy", "label": "什么都不留", "index": -1, "kind": "ghost"}]

        if st.get("pending_decision") == "grave_pick":
            # 从尸体上只挑「一件」带走；uuid 作为 payload 透传给后端
            return [
                {"id": "grave", "label": f"带走：{g['name']}",
                 "choice": "take", "uid": g["uid"], "desc": _item_desc(self.cfg.item(g["id"]), g.get("kind", "weapon")), "kind": "primary"}
                for g in (st.get("grave_choices") or [])
            ] + [{"id": "grave", "label": "什么都不拿", "choice": "skip", "kind": "ghost"}]

        if st.get("pending_decision") == "evac_carry":
            # 撤离带装：三槽选择在专属面板（renderEvacCarry）里做，
            # 命令区保持空——面板确认按钮直接发 carry 动作
            return []

        if st.get("pending_decision") == "bag_overflow":
            # 背包超载（先拿后丢 / 换装缩水）：必须丢到容量以内，无"放弃"选项。
            # 消耗品多给一个"用了"——能用掉的就不用白扔；用完仍超载则决策继续。
            acts: list[dict] = []
            for it in st["inventory"]:
                acts.append({
                    "id": "discard",
                    "label": f"丢掉：{self.cfg.item(it['id'])['name']}",
                    "choice": "drop", "item": it["id"], "kind": "danger",
                })
            usable_ids = []
            for it in st["inventory"]:
                if (
                    it["qty"] > 0
                    and self.cfg.item_kind(it["id"]) == "consumable"
                    and it["id"] not in usable_ids
                ):
                    usable_ids.append(it["id"])
            for uid in usable_ids:
                acts.append({
                    "id": "use",
                    "label": f"用了：{self.cfg.item(uid)['name']}",
                    "item": uid, "kind": "safe",
                })
            return acts

        # 没有待决策、且本局已结束 —— 这才是真正的"无事可做"
        if st["status"] != "active":
            return acts
        # 开局天赋还没选（未下地牢）：没有房间状态，决策选完才进层
        if "level_map" not in st:
            return acts

        room = mapgen.current_room(st["level_map"])
        r = st["room"]

        # Boss 的"引开"已整体下线（用户拍板）：暴君必须正面击杀。
        # 撤离段的威胁被噪音引开消解得太彻底，L5 名存实亡。
        # 保留 _act_lure 只为老存档兼容（按钮不暴露、调用直接拒绝）。
        # 原战斗中 lure 的例外逻辑一并作废——进 Boss 战后只能硬拼。

        if st.get("in_combat"):
            held = combat.equipped_weapon(self.cfg, st) or {}
            melee_held = held.get("kind") == "melee"
            acts.append({"id": "attack", "label": "攻击" if melee_held else "挥拳", "kind": "danger"})
            if held.get("kind") == "ranged":
                acts.append({"id": "shoot", "label": "射击", "kind": "danger"})
                # P9 装填入口恒显示：满弹匣也要能换弹种（旧弹退包）
                acts.append({"id": "reload", "label": "装填（耗 1 回合）", "kind": "safe"})
            acts.append({"id": "flee", "label": "逃跑", "kind": "ghost"})
            # 瞄准：消耗体力换临时命中加成。低体力只是用不了，绝不影响基础命中。
            cost = int(self.cfg.balance["combat"].get("brace_stamina_cost", 0))
            if cost and st.get("stamina", 0) >= cost:
                bonus = int(self.cfg.balance["combat"].get("brace_acc_bonus", 0))
                acts.append({
                    "id": "brace",
                    "label": f"瞄准（耗 {cost} 体力 · 命中 +{bonus} · 不占回合）",
                    "kind": "primary",
                })
            # 物品不再生成按钮：界面上的「随身」面板统一负责展示与操作，
            # 否则物品一多，命令区会被"用绷带/用罐头/换上砍刀…"淹没。
            return acts

        if room.get("special_kind") == "stairs":
            if st["depth"] == self.cfg.last_level_of(st["depth"]):
                if not st.get("boss_alive") or st.get("boss_lured", 0) > 0:
                    acts.append({"id": "evac", "label": "登上直升机", "kind": "primary"})
            elif self._elite_guard_active():
                # 守门精英还站着：不给下楼按钮，只有打
                held = combat.equipped_weapon(self.cfg, st) or {}
                melee_held = held.get("kind") == "melee"
                acts.append({"id": "attack", "label": "攻击" if melee_held else "挥拳", "kind": "danger"})
                if held.get("kind") == "ranged":
                    acts.append({"id": "shoot", "label": "射击", "kind": "danger"})
                    # P9 装填入口恒显示：满弹匣也要能换弹种（旧弹退包）
                    acts.append({"id": "reload", "label": "装填（耗 1 回合）", "kind": "safe"})
            else:
                acts.append({"id": "descend", "label": "下一层", "kind": "primary"})

        if room.get("special_kind") == "campfire" and not st.get("campfire_used"):
            acts.append({"id": "campfire", "label": "在火边休息", "kind": "safe"})

        # 灾害房抉择（P6.2.2）
        if r.get("hazard"):
            for c in r["hazard"]["choices"]:
                acts.append({
                    "id": "hazard", "label": c["label"],
                    "choice": c["id"], "kind": "danger",
                })

        # 幸存者交互（P6.2.2）：面板级数据走 state.npc，命令区只放核心抉择
        if r.get("npc") and not room.get("resolved"):
            acts.append({"id": "npc", "label": "分他一点食物", "choice": "share", "kind": "safe"})
            acts.append({"id": "npc", "label": "离开", "choice": "leave", "kind": "ghost"})

        if r.get("event"):
            for c in r["event"]["choices"]:
                acts.append({
                    "id": "event", "label": c["label"],
                    "choice": c["id"], "kind": "primary",
                })

        if r.get("grave"):
            acts += [
                {"id": "grave", "label": "搜刮遗体", "choice": "loot", "kind": "danger"},
                {"id": "grave", "label": "掩埋", "choice": "bury", "kind": "safe"},
                {"id": "grave", "label": "离开", "choice": "leave", "kind": "ghost"},
            ]

        # 商人：买卖/修理/出售/作者怜悯的详细 UI 由前端的「商人面板」负责渲染
        # （数据来自 state.merchant / state.repair_options / state.inventory），
        # 命令区只保留「离开」，避免按钮淹没决策。
        if room.get("type") == "merchant" and not room.get("resolved"):
            acts.append({"id": "merchant", "label": "离开商人", "choice": "leave", "kind": "ghost"})

        # 手电耗尽时搜刮必然失败——那就不要给这个按钮。
        # 否则玩家（和自动模拟）会陷入"反复点击无效的搜刮"的死循环。
        # 同时必须和 _act_search 允许的类型保持一致，否则会出现"点搜索却说没东西翻"的死按钮。
        # 灾害房不开放搜刮：没东西可搜，且每点一次都在烧倒计时。
        # 巢穴清空后可以搜（地上有尸体掉落），未清时战场混乱不给搜。
        searchable = room["type"] in ("loot", "combat", "empty", "grave", "event", "special") or (
            room["type"] == "nest" and room.get("cleared")
        )
        if searchable and not r.get("searched") and level_rules.can_search(self.cfg, st)[0]:
            acts.append({"id": "search", "label": "搜刮这里", "kind": "primary"})

        for e in room.get("exits") or []:
            acts.append({"id": "move", "label": e["label"], "to": e["to"], "kind": "move"})

        # 同上：物品操作交给「随身」面板，命令区只保留本回合的决策
        acts.append({"id": "status", "label": "查看状态", "kind": "ghost"})
        acts.append({"id": "give_up", "label": "放弃这一局", "kind": "ghost"})
        return acts


__all__ = ["RunEngine", "RunEnded"]
