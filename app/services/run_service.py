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
            p.append(f"每发 {item.get('ammo_per_shot', 1)} 弹")
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

    # ------------------------------------------------------------------
    # 存档
    # ------------------------------------------------------------------
    def persist_rng(self) -> None:
        self.state["rng"] = self.rng.get_state()

    # ------------------------------------------------------------------
    # 感染 → 最大生命
    # ------------------------------------------------------------------
    def _sync_hp_max(self) -> None:
        """感染会压低最大生命，改完感染度必须同步一次，否则数值是死的。"""
        st = self.state
        inf = inf_mod.modifiers(self.cfg, st["infection"])
        base = int(self.cfg.balance["player"]["hp"])
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
            "campfire_used": False,
            "pending_decision": None,
            "legacy_choices": None,
            "started_at": int(time.time()),
        }

        # 开局物资
        for iid, qty in b.get("start_items") or []:
            loot.grant(cfg, state, iid, qty)
        # 弹药：给通用手枪弹
        loot.grant(cfg, state, "ammo_pistol", b["ammo_start"])

        eng = cls(cfg, state, world=world)
        if legacy:
            eng._apply_legacy(legacy)
            # 上一局是撤离成功 → 不触发天赋。
            # 通关已经给了满耐久遗物 + 撤离津贴；再叠天赋会让"故意去死"重新变成最优解。
            if legacy.get("earned_by") == "escaped":
                eng.state["talent_eligible"] = False
        # 有待选天赋时先不下地牢——否则玩家会在选完天赋前就撞上第一场遭遇，
        # 而"最大生命 +8"这类天赋必须在这之前生效才算数。
        if not eng._draw_talents():
            await eng._enter_level(1, first=True)
        eng.persist_rng()
        return eng

    # ------------------------------------------------------------------
    def _draw_talents(self) -> bool:
        """抽三个待选天赋。返回是否进入待选状态。"""
        """复活进场时抽三个天赋待选。

        首局也会给——新手不该必须先死一次才能见到这个系统。
        """
        st = self.state
        if (
            not self.talents_enabled
            or st.get("talent_eligible") is False
            or not self.cfg.talents_cfg.get("talents")
        ):
            return False
        options = talents.draw(self.cfg, self.rng)
        if not options:
            return False
        st["talent_options"] = [
            {"id": t["id"], "name": t["name"], "desc": t["desc"]} for t in options
        ]
        st["pending_decision"] = "talent"
        self._log("你在一处废弃的地下室里恢复意识。身上只剩下三样东西能指望：")
        return True

    async def _act_talent(self, payload: dict) -> None:
        """三选一。选完才真正开始这一局。"""
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
        # 选完才正式下地牢，让生命上限类天赋在第一场战斗前生效
        await self._enter_level(1, first=True)

    # ------------------------------------------------------------------
    def _apply_legacy(self, legacy: dict) -> None:
        """继承遗物，并按传承代次衰减。

        每传一代：伤害 ×0.85、耐久 ×0.6、护甲 −1。
        传满 max_passes 代直接报废——任何装备都有寿命，
        这条是"越玩越强"正反馈的终止条件。
        """
        cfg, st = self.cfg, self.state
        rules = cfg.legacy_rules()
        max_passes = int(rules.get("max_passes", 3))
        iid = legacy["id"]
        item = cfg.item(iid)

        # 撤离带回来的装备保养过，不算一次传承磨损；
        # 死亡继承的是从尸体上扒下来的，多磨损一代。
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
        dur_mult = float(rules.get("durability_mult", 0.6)) ** passes
        armor_delta = -int(rules.get("armor_penalty", 1)) * passes

        dur = legacy.get("durability")
        if dur is not None:
            if survived and rules.get("escape", {}).get("restore_durability", True):
                dur = int(item.get("durability", dur)) or dur  # 撤离：带回满耐久
            else:
                dur = max(1, int(round(float(dur) * dur_mult)))

        if kind == "weapon":
            st["weapon"] = {
                "id": iid, "durability": dur,
                "dmg_mult": round(dmg_mult, 4), "passes": passes,
            }
        elif kind == "armor":
            adur = int(self.cfg.item(iid).get("durability", 0) or 0)
            if adur:
                adur = max(1, int(round(adur * dur_mult)))
            st["armor"] = {"id": iid, "durability": adur, "passes": passes}
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
                    f"耐久 {dur}" if dur is not None else None,
                    f"护甲 {armor_delta:+d}" if kind == "armor" else None,
                ) if p
            )
            self._log(
                f"你带上了{item['name']}（第 {passes} 次传承"
                + (f"，{wear}" if wear else "")
                + "）。它比记忆里更旧了。"
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
        st["boss_alive"] = level == self.cfg.max_level
        st["boss_lured"] = 0
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
        else:
            self._log("什么都没有。")

    async def _enter_special(self, room: dict) -> None:
        st = self.state
        kind = room.get("special_kind")
        if kind == "stairs":
            if st["depth"] == self.cfg.max_level:
                st["room"]["name"] = self.cfg.levels_cfg["boss"]["room_name"]
                await self._enter_boss()
            else:
                self._log("一道向下的楼梯。往下是更黑的地方。")
        elif kind == "campfire":
            if st.get("campfire_used"):
                self._log("一堆冷掉的灰。你用过一次了。")
            else:
                self._log("有人在这里生过火，还有余温。可以歇一会，但火光会暴露位置。")

    async def _enter_boss(self) -> None:
        st = self.state
        if not st.get("boss_alive"):
            self._log("撤离点空着。直升机随时会来。")
            return
        boss_id = self.cfg.levels_cfg["boss"]["id"]
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
        vibe = st["log"][-1] if st["log"] else ""
        self._log(f"   事件：{ev['text'].replace('{ai_desc}', vibe.rstrip('。') or '四周一片死寂')}")

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

    def _enter_merchant(self, room: dict) -> None:
        """商人房间：进入时随机铺货（1 武器 / 1 装备 / 1 背包 / 3 其他），
        并按概率决定商人类型（普通 / 感染）与是否触发「作者怜悯」。"""
        st = self.state
        if room.get("resolved"):
            self._log("商人已经收摊走了。")
            return
        # 已生成过则不复抓（重入保持同一商人/同一批货）
        if st["room"].get("merchant"):
            m = st["room"]["merchant"]
            if m["type"] == "plagued":
                self._log("那个溃烂的身影还在原地，等着你拿命换货。")
            else:
                self._log("他摊开一块破布，上面零零碎碎全是货：「废铁换命，懂？」")
            return

        mcfg = self.cfg.balance.get("merchant", {})
        plagued_chance = float(mcfg.get("plagued", {}).get("spawn_chance", 0))
        is_plagued = bool(plagued_chance and self.rng.chance(plagued_chance))
        discount = 1.0
        if is_plagued:
            discount = float(mcfg.get("plagued", {}).get("discount", 0.5))

        mercy = (not is_plagued) and bool(
            mcfg.get("authors_mercy_chance", 0)
            and self.rng.chance(float(mcfg["authors_mercy_chance"]))
        )

        shop = self._roll_shop(mcfg, discount)
        st["room"]["merchant"] = {
            "type": "plagued" if is_plagued else "normal",
            "discount": discount,
            "authors_mercy": mercy,
            "mercy_taken": False,
            "shop": shop,
        }

        if is_plagued:
            self._log(
                "一个浑身溃烂的身影挡在路中间，皮肤下有什么在蠕动："
                "「想活命？拿你的命来换。」（血量过半即可交易，成交时你缺多少血它抽多少）"
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
        }

    def _roll_shop(self, mcfg: dict, discount: float) -> list[dict]:
        """按 shop_slots 模板随机铺货：1 武器 / 1 装备（护甲或背包）/ 1 背包 / 3 其他。"""
        cfg = self.cfg
        slots = mcfg.get("shop_slots", {}) or {}
        out: list[dict] = []
        for _ in range(int(slots.get("weapon", 0))):
            pool = [w["id"] for w in cfg.items_cfg["weapons"] if w["id"] != "crowbar"]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        for _ in range(int(slots.get("gear", 0))):
            pool = [a["id"] for a in cfg.items_cfg["armor"]] + \
                   [b["id"] for b in cfg.items_cfg["backpacks"]]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        for _ in range(int(slots.get("backpack", 0))):
            pool = [b["id"] for b in cfg.items_cfg["backpacks"]]
            if pool:
                out.append(self._shop_entry(self.rng.choice(pool), discount))
        pool = mcfg.get("other_pool") or []
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

        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                self._log("你不明白自己想做什么。")
            else:
                await handler(payload)
                if st["status"] == "active":
                    for line in level_rules.tick_turn(self.cfg, st):
                        self._log(line)
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
            # 背包超载：丢弃物品回到容量以内（先拿后丢模型的强制清理）
            await self._act_discard(payload)

        self.persist_rng()
        return self._response()

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
        await self._enter_room(target)

    async def _act_search(self, payload: dict) -> None:
        st = self.state
        if st.get("in_combat"):
            self._log("先解决眼前的东西。")
            return
        room = mapgen.current_room(st["level_map"])
        if room["type"] not in ("loot", "combat", "empty", "grave", "event", "special"):
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
        """枪店类房间的武器掉落。"""
        pool = [w for w in self.cfg.items_cfg["weapons"] if w["id"] != "crowbar"]
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
            return

        wcfg = combat.equipped_weapon(self.cfg, st)
        if not wcfg:
            self._log("你手上是空的。")
            return

        if ranged:
            if wcfg.get("kind") != "ranged":
                self._log("这不是枪。")
                return
            atype = wcfg["ammo_type"]
            need = int(wcfg.get("ammo_per_shot", 1))
            if loot.count(st, atype) < need:
                self._log(f"{self.cfg.item(atype)['name']}不够了。")
                return
            loot.remove(st, atype, need)
            noise.add(self.cfg, st, wcfg.get("noise_key", "gunshot"))
        else:
            if wcfg.get("kind") != "melee":
                self._log("这东西不适合近身挥。")
                return
            self._damage_weapon(1)

        # 远程命中率由天赋 / buff / 武器 acc_mod 决定，与体力无关（设计红线）。
        pp = combat.player_profile(self.cfg, st)

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
                await self._player_hit_one(enemy, pp, ranged)
        else:
            target = payload.get("target")
            enemy = enemies[0]
            if isinstance(target, int) and 0 <= target < len(st["combat"]["enemies"]):
                cand = st["combat"]["enemies"][target]
                enemy = cand if cand["hp"] > 0 else enemy
            await self._player_hit_one(enemy, pp, ranged)

        await self._enemy_round()

    async def _player_hit_one(self, enemy: dict, pp: dict, ranged: bool) -> None:
        """对单个敌人结算一次玩家攻击（命中/闪避/暴击各自独立）。"""
        st = self.state
        ep = combat.enemy_profile(self.cfg, enemy)
        res = combat.resolve_attack(self.cfg, self.rng, pp, ep)
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
        """超容量（含换装缩水）时暂停为 bag_overflow 决策：丢到装得下为止。"""
        st = self.state
        if self._over_capacity() and st.get("pending_decision") != "bag_overflow":
            st["pending_decision"] = "bag_overflow"
            self._log("背包塞得太满了——先丢掉一些东西，才能继续行动。")
            return True
        return False

    def _acquire(self, item_id: str, qty: int = 1, durability: int | None = None) -> bool:
        """入包的唯一入口：先拿后丢——总是直接 grant（允许暂时超上限），
        超了就暂停为 bag_overflow 决策。返回是否成功入包。
        现金走独立计数，不占格、永不触发超载。"""
        st = self.state
        if item_id == "cash":
            loot.grant(self.cfg, st, "cash", qty)
            return True
        loot.grant(self.cfg, st, item_id, qty, durability)
        if self._over_capacity():
            name = self.cfg.item(item_id)["name"]
            self._log(f"你硬把 {name} 塞了进去——背包超载了，得丢掉一些东西才能继续。")
            self._check_bag_overflow()
        return True

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
        if st.get("pending_decision") == "bag_overflow":
            if not self._over_capacity():
                st["pending_decision"] = None
                self._log("背包腾出了空间。")

    async def _kill_enemy(self, enemy: dict, ranged: bool) -> None:
        st = self.state
        st["kills"] += 1
        st["score"] += int(enemy.get("score", 8))
        self._log(f"{enemy['name']}倒下了。")

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
            self._check_horde()
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
            res = combat.resolve_attack(
                self.cfg, self.rng, ep, pp,
                attacker_meta={
                    "bite_chance": enemy.get("bite_chance", 0),
                    "bite_infection": enemy.get("bite_infection", [3, 6]),
                },
            )
            if not res["hit"]:
                self._log(f"{enemy['name']}扑空了。")
                continue

            absorbed = self._apply_armor_absorb(res["dmg"])
            taken = max(0, res["dmg"] - absorbed)
            st["hp"] -= taken
            if absorbed:
                self._log(f"{enemy['name']}击中你，造成 {taken} 点伤害（护甲吸收 {absorbed}）。")
            else:
                self._log(f"{enemy['name']}击中你，造成 {res['dmg']} 点伤害。")

            if "bite" in res["effects"] and res["infection"]:
                old, new = self._add_infection(res["infection"])
                self._log(f"它咬了你一口。（感染 +{new - old}）")
                line = inf_mod.describe_change(self.cfg, old, new)
                if line:
                    self._log(line)

            oh = enemy.get("on_hit") or {}
            if oh.get("noise_add"):
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

    async def _act_flee(self, payload: dict) -> None:
        st = self.state
        if not st.get("in_combat"):
            self._log("没什么好逃的。")
            return
        alive = [e for e in st["combat"]["enemies"] if e["hp"] > 0]
        fastest = max((e.get("speed", 5) for e in alive), default=5)
        agi = combat.player_agility(st) + int(talents.mod(st, "flee_bonus", 0)) // 5
        # 体力加成按扣减前的当前体力计：体力越满越容易逃掉（每点 +0.5%，可配）
        if combat.try_flee(self.cfg, self.rng, agi, fastest, stamina=st["stamina"]):
            st["in_combat"] = False
            noise.add(self.cfg, st, "sprint")
            self._log("你转身就跑，把它们甩在了身后。（噪音 +2）")
            self._check_horde()
        else:
            self._log("你没能甩掉它们。")
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

    async def _act_use(self, payload: dict) -> None:
        st = self.state
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

        if item.get("heal"):
            amount = self.rng.rand_range_int(item["heal"])
            before = st["hp"]
            st["hp"] = min(st["hp_max"], st["hp"] + amount)
            parts.append(f"HP +{st['hp'] - before}")
        if item.get("heal_stamina"):
            before = st.get("stamina", 0)
            st["stamina"] = min(
                self.cfg.balance["player"]["stamina"],
                before + int(item["heal_stamina"]),
            )
            parts.append(f"体力 +{st['stamina'] - before}")
        if item.get("infection"):
            old, new = self._add_infection(int(item["infection"]))
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
        bonus = int(self.cfg.balance["combat"].get("brace_acc_bonus", 0))
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
            st["weapon"] = {"id": iid, "durability": entry.get("durability")}
            loot.remove(st, iid, 1)
            if old:
                loot.grant(self.cfg, st, old["id"], 1, old.get("durability"))
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
            maxd = int(cfg.item(oid).get("durability", 0) or 0)
            cur = int(dur)
            if cur < maxd:
                return (obj, oid, maxd, cur)
            return None

        if iid:
            if st.get("weapon", {}).get("id") == iid:
                r = _check(st["weapon"], iid)
                if r:
                    return r
            if st.get("armor", {}).get("id") == iid:
                r = _check(st["armor"], iid)
                if r:
                    return r
            for e in st["inventory"]:
                if e["id"] == iid and e["qty"] > 0:
                    r = _check(e, iid)
                    if r:
                        return r
            return None

        r = _check(st.get("weapon"), st.get("weapon", {}).get("id"))
        if r:
            return r
        r = _check(st.get("armor"), st.get("armor", {}).get("id"))
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

    def _do_repair(self, iid: str, pay: str) -> tuple[bool, str]:
        """用废铁或现金修理一件装备，每次修「一点」耐久。

        pay ∈ {"scrap","cash"}。每修 1 点耐久耗 scrap_per_point / cash_per_point
        单位资源，向上取整；天赋 repair_bonus 可让一次修多几点。
        玩家可以反复点，一点一点把武器修满——不再强制一次修到满。
        非商人区域也可用（前端从随身面板对废料装备发起，走同一入口）。
        """
        st = self.state
        cfg = self.cfg
        tgt = self._repair_target(iid)
        if not tgt:
            return False, "没有需要修理的装备。"
        obj, wid, maxd, cur = tgt
        (scrap_per, cash_per), bonus = self._repair_rates()
        if pay == "scrap":
            per, cur_res, res_name = scrap_per, loot.count(st, "scrap"), "废料"
        else:
            per, cur_res, res_name = cash_per, loot.count(st, "cash"), "现金"

        # 这次修几点：基础 1 点 + 天赋奖励；不超过缺失量
        missing = maxd - cur
        points = max(1, 1 + int(bonus)) if bonus > 0 else 1
        points = min(points, missing)
        cost = math.ceil(points * per)
        if cur_res < cost:
            return False, (
                f"{res_name}不够——修 1 点耐久要 {math.ceil(per)} {res_name}"
                f"（你只有 {cur_res}）。"
            )
        loot.remove(st, "scrap" if pay == "scrap" else "cash", cost)
        obj["durability"] = cur + points
        if points > 1:
            return True, (
                f"你用 {cost} {res_name} 把{cfg.item(wid)['name']}修了 {points} 点耐久"
                f"（{cur}→{cur + points}）。"
            )
        return True, (
            f"你用 {cost} {res_name} 把{cfg.item(wid)['name']}修了 1 点耐久"
            f"（{cur}→{cur + 1}）。"
        )

    def _repair_options(self) -> list[dict]:
        """列出当前可修理的装备（近战武器 / 护甲），含废料与现金两种单价。

        每次修理只修一点（向上取整）。前端据此渲染修理按钮并展示换算比例，
        无需自己读配置算价。
        """
        cfg = self.cfg
        (scrap_per, cash_per), _bonus = self._repair_rates()
        scrap_tip = math.ceil(scrap_per)  # 1 废料能修多少点：向下兼容的展示口径
        out: list[dict] = []
        # 当前装备优先，再扫背包里的武器/护甲
        candidates = []
        if st_w := self.state.get("weapon"):
            candidates.append((st_w.get("id"), st_w.get("durability")))
        if st_a := self.state.get("armor"):
            candidates.append((st_a.get("id"), st_a.get("durability")))
        for e in self.state["inventory"]:
            if e["qty"] > 0 and cfg.item_kind(e["id"]) in ("weapon", "armor"):
                candidates.append((e["id"], e.get("durability")))
        for iid, dur in candidates:
            if iid is None or dur is None:
                continue
            maxd = int(cfg.item(iid).get("durability", 0) or 0)
            cur = int(dur)
            if cur < maxd:
                out.append({
                    "id": iid,
                    "name": cfg.item(iid)["name"],
                    "kind": cfg.item_kind(iid),
                    "max": maxd,
                    "cur": cur,
                    # 每次修 1 点的单价（向上取整）
                    "scrap_cost": math.ceil(scrap_per),
                    "cash_cost": math.ceil(cash_per),
                    # 换算提示：1 废料可修的耐久点数（向上取整口径下 ≥1）
                    "scrap_points": max(1, int(1 / scrap_per)) if scrap_per > 0 else 1,
                    "cash_points": max(1, int(1 / cash_per)) if cash_per > 0 else 1,
                })
        return out

    def _repair_rate_hints(self) -> dict:
        """修理换算的展示口径：修 1 点耐久各资源的单价（向上取整）。"""
        (scrap_per, cash_per), _bonus = self._repair_rates()
        return {
            "scrap_per_point": math.ceil(scrap_per),
            "cash_per_point": math.ceil(cash_per),
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
            st["room"]["merchant_left"] = True
            self._log("你冲商人点了点头，继续往前走。")
            return

        # 感染商人规则：
        #   血量 ≥ 50%（可配）最大生命 → 享受折扣价，首次成交时抽走「缺失的血量」；
        #   血量不过半 → 仍可交易，但按原价（无折扣）、不抽血。
        #   折扣只在 shop 生成时应用，因此原价 = entry["value"]，现价 = entry["cost"]。
        #   抽血每次进房只发生一次（hp_taken 标记），买多件不会重复被抽。
        full_price = False
        if m["type"] == "plagued":
            threshold = st["hp_max"] * float(
                cfg.balance.get("merchant", {}).get("plagued", {}).get("min_hp_pct", 0.5)
            )
            if st["hp"] < threshold:
                full_price = True
                self._log("它盯着你失血的手臂嘶笑：这个状态没资格讲价——按原价来。")
            elif not m.get("hp_taken"):
                hp_cost = max(1, st["hp_max"] - st["hp"])
                st["hp"] = max(1, st["hp"] - hp_cost)
                m["hp_taken"] = True
                self._log(f"它伸手按在你胸口，把你缺的血全抽走了。（HP −{hp_cost}）")
        m["_full_price"] = full_price

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
            # 感染商人血量不过半时按原价（value）；正常折扣价 = cost
            cost = int(entry["value"]) if m.get("_full_price") else int(entry["cost"])
            if loot.count(st, "cash") < cost:
                self._log(
                    f"现金不够——{cfg.item(iid)['name']} 要 {cost}，你只有 {loot.count(st, 'cash')}。"
                )
                return
            # 先拿后丢：直接成交入包（超容量由 bag_overflow 决策兜底）
            self._acquire(iid, 1)
            loot.remove(st, "cash", cost)
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
            value = int(cfg.item(iid).get("value", 1))
            ratio = float(cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
            price = max(1, int(round(value * ratio)))
            qty = loot.count(st, iid)
            loot.remove(st, iid, qty)
            loot.grant(cfg, st, "cash", price * qty)  # 现金独立计数，不进背包
            self._log(f"你把 {cfg.item(iid)['name']}×{qty} 卖了 {price * qty} 现金。")
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
            self.cfg.balance["player"]["stamina"], sta_before + int(tpl.get("stamina", 0))
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
            self._log("有东西挡在路上。")
            return
        self._log_many(await self._descend())

    async def _act_evac(self, payload: dict) -> None:
        st = self.state
        if st["depth"] != self.cfg.max_level:
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
        st["status"] = "escaped"
        self._log("* 你抓住起落架，被拉进了机舱。城市在下面越来越小。 *")
        self._log(f"** 撤离成功。最终得分 {st['score']} **")
        st["pending_decision"] = "legacy"
        st["legacy_choices"] = self._legacy_candidates()
        if st["legacy_choices"]:
            self._log("你能带走的只有一样。选一个：")
        raise RunEnded("escaped")

    async def _act_lure(self, payload: dict) -> None:
        """制造噪音引开 Boss。"""
        st = self.state
        if not st.get("boss_alive"):
            self._log("它已经不在了。")
            return
        if not st.get("boss_seen"):
            self._log("你还不知道它在哪。")
            return
        threshold = int(self.cfg.levels_cfg["boss"]["lure"]["noise_threshold"])
        turns = int(self.cfg.levels_cfg["boss"]["lure"]["lure_turns"])
        need = max(0, threshold - noise.value(st))
        if need > 0:
            st["noise"] = float(noise.add(self.cfg, st, need))
            self._log(f"你砸碎了身边的玻璃，用力敲打栏杆。（噪音 +{need}）")
        else:
            self._log("噪音已经够了。")
        if noise.value(st) >= threshold:
            st["boss_lured"] = turns
            st["boss_alive"] = False
            st["in_combat"] = False
            st["combat"] = {"enemies": [], "round": 0}
            self._log("它循着声音转过身，慢慢走开了。撤离点空出来了。")

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
        t = talents.summary(st)
        if t:
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
    def _legacy_blocked(self) -> list[str]:
        """身上有、但因为品质太高而不能带走的物品名。"""
        st = self.state
        names: list[str] = []
        ids = []
        if st.get("weapon"):
            ids.append(st["weapon"]["id"])
        if st.get("armor"):
            ids.append(st["armor"]["id"])
        ids += [e["id"] for e in st["inventory"]]
        for iid in ids:
            if self.cfg.item_kind(iid) in ("weapon", "armor") and not self.cfg.legacy_allowed(iid):
                nm = self.cfg.item(iid)["name"]
                if nm not in names:
                    names.append(nm)
        return names

    def _legacy_candidates(self) -> list[dict]:
        """可选作遗物的物品。重火力（tier 3）与消耗品不在其中。"""
        st = self.state
        out: list[dict] = []
        seen: set[str] = set()

        def _entry(iid: str, durability: int | None, passes: int) -> None:
            if iid in seen or not self.cfg.legacy_allowed(iid):
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
        st["legacy_choices"] = self._legacy_candidates()
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
            {"name": e["name"], "hp": max(0, e["hp"]), "hp_max": e["hp_max"]}
            for e in st.get("combat", {}).get("enemies", [])
            if e["hp"] > 0
        ]
        wcfg = combat.equipped_weapon(self.cfg, st)
        ammo = {
            a["id"]: loot.count(st, a["id"]) for a in self.cfg.items_cfg["ammo"]
        }
        st["ammo_total"] = sum(ammo.values())

        inventory = []
        _sell_ratio = float(self.cfg.balance.get("merchant", {}).get("sell_ratio", 0.7))
        for e in st["inventory"]:
            item = self.cfg.item(e["id"])
            kind = self.cfg.item_kind(e["id"])
            sellable = kind not in ("ammo", "consumable") and e["id"] != "cash"
            inventory.append({
                "id": e["id"],
                "name": item["name"],
                "qty": e["qty"],
                "kind": kind,
                "wearable": kind in ("weapon", "armor", "backpack"),
                "usable": kind == "consumable",
                "durability": e.get("durability"),
                "desc": _item_desc(item, kind),
                # 可出售类道具的预估回收价，前端直接展示，不必自己读配置
                "sell": max(1, int(round(int(item.get("value", 1)) * _sell_ratio))) if sellable else None,
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
                "stamina_max": int(self.cfg.balance["player"]["stamina"]),
                "infection": st["infection"],
                "infection_band": band,
                "noise": round(noise.value(st), 1),
                "horde": bool(st.get("horde")),
                "flashlight": st.get("flashlight"),
                "depth": st["depth"],
                "max_depth": self.cfg.max_level,
                "turn": st["turn"],
                "kills": st["kills"],
                "score": st["score"],
                "evac_countdown": st.get("evac_countdown"),
                "zombified": bool(st.get("zombified")),
                "weapon": {
                    "name": wcfg["name"] if wcfg else "空手",
                    "id": wcfg["id"] if wcfg else None,
                    "durability": (st.get("weapon") or {}).get("durability"),
                    "ranged": bool(wcfg and wcfg.get("kind") == "ranged"),
                    "desc": _item_desc(wcfg, "weapon") if wcfg else None,
                },
                "armor": self.cfg.item(st["armor"]["id"])["name"] if st.get("armor") else None,
                "armor_desc": (
                    _item_desc(self.cfg.item(st["armor"]["id"]), "armor", self._armor_absorb_pct())
                    if st.get("armor") else None
                ),
                "backpack": (
                    {
                        "id": st["backpack"]["id"],
                        "name": self.cfg.item(st["backpack"]["id"])["name"],
                        "slots": int(self.cfg.item(st["backpack"]["id"]).get("slots", 0)),
                        "desc": _item_desc(self.cfg.item(st["backpack"]["id"]), "backpack"),
                    }
                    if st.get("backpack") else None
                ),
                "bag_cap": self._bag_cap(),
                "bag_used": len(st["inventory"]),
                "ammo": ammo,
                # 现金独立计数：不进背包；旧局背包里的现金条目向下兼容并入显示
                "cash": loot.count(st, "cash"),
                "scrap": loot.count(st, "scrap"),
                # 修理换算提示：每次修 1 点耐久的单价（向上取整）
                "repair_rates": self._repair_rate_hints(),
                "inventory": inventory,
                "repair_options": self._repair_options(),
                "merchant": (
                    {
                        "type": m["type"],
                        "discount": round(m.get("discount", 1.0), 2),
                        "authors_mercy": bool(m.get("authors_mercy")),
                        "mercy_taken": bool(m.get("mercy_taken")),
                        # 感染商人血量不过半：可交易但按原价（无折扣、不抽血）
                        "full_price": bool(m.get("_full_price")),
                        "shop": [
                            {
                                "id": s["id"],
                                "name": self.cfg.item(s["id"])["name"],
                                "kind": s["kind"],
                                "cost": int(s["cost"]),
                                "value": int(s["value"]),
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
            "talent": talents.summary(st),
            "legacy_choices": [
                {"name": c["name"], "index": i, "tier": c.get("tier", 1),
                 "passes": c.get("passes", 0)}
                for i, c in enumerate(st.get("legacy_choices") or [])
            ],
            # 重火力带不走，得让玩家知道原因，否则会以为掷弹枪被系统吞了
            "legacy_blocked": self._legacy_blocked(),
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

        if st.get("pending_decision") == "bag_overflow":
            # 背包超载（先拿后丢 / 换装缩水）：必须丢到容量以内，无"放弃"选项
            return [
                {"id": "discard", "label": f"丢掉：{self.cfg.item(it['id'])['name']}",
                 "choice": "drop", "item": it["id"], "kind": "danger"}
                for it in st["inventory"]
            ]

        # 没有待决策、且本局已结束 —— 这才是真正的"无事可做"
        if st["status"] != "active":
            return acts

        room = mapgen.current_room(st["level_map"])
        r = st["room"]

        # Boss 的"引开"必须**即使正在交战**也可用。
        # 这一条放在 in_combat 的提前返回之前——否则玩家一旦被拖进 Boss 战，
        # 就只能硬拼到死，而"制造噪音引开绕行"这个设计意图永远用不上。
        if (
            st.get("boss_alive")
            and st.get("boss_seen")          # 得先真的碰上它，不能隔空引开
            and st.get("boss_lured", 0) <= 0
        ):
            acts.append({"id": "lure", "label": "制造噪音引开它", "kind": "danger"})

        if st.get("in_combat"):
            acts.append({"id": "attack", "label": "攻击", "kind": "danger"})
            if (combat.equipped_weapon(self.cfg, st) or {}).get("kind") == "ranged":
                acts.append({"id": "shoot", "label": "射击", "kind": "danger"})
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
            if st["depth"] == self.cfg.max_level:
                if not st.get("boss_alive") or st.get("boss_lured", 0) > 0:
                    acts.append({"id": "evac", "label": "登上直升机", "kind": "primary"})
            else:
                acts.append({"id": "descend", "label": "下一层", "kind": "primary"})

        if room.get("special_kind") == "campfire" and not st.get("campfire_used"):
            acts.append({"id": "campfire", "label": "在火边休息", "kind": "safe"})

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
        searchable = room["type"] in ("loot", "combat", "empty", "grave", "event", "special")
        if searchable and not r.get("searched") and level_rules.can_search(self.cfg, st)[0]:
            acts.append({"id": "search", "label": "搜刮这里", "kind": "primary"})

        for e in room.get("exits") or []:
            acts.append({"id": "move", "label": e["label"], "to": e["to"], "kind": "move"})

        # 同上：物品操作交给「随身」面板，命令区只保留本回合的决策
        acts.append({"id": "status", "label": "查看状态", "kind": "ghost"})
        acts.append({"id": "give_up", "label": "放弃这一局", "kind": "ghost"})
        return acts


__all__ = ["RunEngine", "RunEnded"]
