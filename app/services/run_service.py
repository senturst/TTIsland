"""run 状态机——游戏的全部规则都在这里。

一个 run 的完整生命周期：
    起局 → 进入房间（氛围/遭遇/搜刮/事件）→ 战斗 → 找楼梯下行
    → 第 5 层撤离倒计时 → 撤离成功 / 死亡（留遗物）→ 结算

所有随机都走 RNG 实例（状态随存档持久化），
避免玩家靠刷新页面反复抽同一个掉落。
"""
from __future__ import annotations

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


class RunEngine:
    # 供 scripts/sim.py 在测量基线时关掉天赋（不然测不出天赋本身的贡献）
    talents_enabled: bool = True

    def __init__(self, cfg: GameConfig, state: dict) -> None:
        self.cfg = cfg
        self.state = state
        self.rng = RNG(state=state["rng"])
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

    # ==================================================================
    # 起局
    # ==================================================================
    @classmethod
    async def new_run(cls, cfg: GameConfig, legacy: dict | None = None) -> "RunEngine":
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

        eng = cls(cfg, state)
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
            st["armor"] = {"id": iid, "armor_delta": armor_delta, "passes": passes}
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
        """墓碑/遗骸房间。

        P4 会替换成真正的玩家墓碑注入。首批先用「无名遗骸」占位，
        保证空服务器时这个房间类型也有内容（而不是空房）。
        """
        st = self.state
        if room.get("resolved"):
            self._log("这具遗体你已经处理过了。")
            return
        st["room"]["grave"] = {"name": "无名遗骸", "looted": False}
        desc = "前一具遗体靠在墙边，装备已经被翻过一次，但似乎还剩点东西。"
        st["room"]["grave"]["desc"] = desc
        self._log(desc)

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

        if room["type"] == "loot":
            tpl = self.cfg.room_template("loot", room["tpl"])
            rolls = self.rng.rand_range_int(tpl.get("rolls", [1, 1]))
            rolls = max(1, int(round(rolls * level_rules.loot_rolls_mult(self.cfg, st))))
            if room.get("hazmat"):
                rolls *= level_rules.medical_bonus(self.cfg, st)
            quality = level_rules.loot_quality_bonus(self.cfg, st)
            tables = tpl.get("tables") or ["ammo"]

            if tpl.get("pry_required"):
                if not (self.cfg.item(st["weapon"]["id"]).get("pry") if st["weapon"] else False):
                    self._log("你得用能撬的东西才打得开。")
                    return
                noise.add(self.cfg, st, tpl.get("noise_on_pry", 2))
                self._log("你用撬棍别开了它，响声不小。")
                self._damage_weapon(1)

            # 拾荒直觉：额外一次判定的机会
            extra = float(talents.mod(st, "loot_extra_roll_chance", 0.0))
            if extra and self.rng.chance(extra):
                rolls += 1
                self._log("你的手比眼睛先找到了东西。")

            found: list[str] = []
            for _ in range(rolls):
                cat = self.rng.choice(tables)
                if cat == "weapons":
                    self._roll_weapon(found)
                else:
                    for iid, qty in loot.roll_loot(
                        self.cfg, self.rng, st, cat, 1, quality
                    ):
                        loot.grant(self.cfg, st, iid, qty)
                        found.append(loot.describe(self.cfg, iid, qty))
            if found:
                self._log("你找到了：" + "、".join(found))
            else:
                self._log("空的。什么都没剩下。")
        else:
            # 非物资房：小概率摸到东西
            if self.rng.chance(0.45):
                cat = loot.roll_category(self.cfg, self.rng)
                for iid, qty in loot.roll_loot(self.cfg, self.rng, st, cat, 1):
                    loot.grant(self.cfg, st, iid, qty)
                    self._log(f"你在角落里摸到了 {loot.describe(self.cfg, iid, qty)}。")
            else:
                self._log("你翻遍了每个角落，一无所获。")

        self._check_horde()

    def _roll_weapon(self, found: list[str]) -> None:
        """枪店类房间的武器掉落。"""
        pool = [w for w in self.cfg.items_cfg["weapons"] if w["id"] != "crowbar"]
        if not pool:
            return
        w = self.rng.weighted_choice(pool, [x.get("weight", 10) for x in pool])
        dur = int(w.get("durability", 0)) or None
        loot.grant(self.cfg, self.state, w["id"], 1, dur)
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

        weapon = st.get("weapon")
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

        target = payload.get("target")
        enemy = enemies[0]
        if isinstance(target, int) and 0 <= target < len(st["combat"]["enemies"]):
            cand = st["combat"]["enemies"][target]
            enemy = cand if cand["hp"] > 0 else enemy

        pp = combat.player_profile(self.cfg, st)
        ep = combat.enemy_profile(self.cfg, enemy)
        res = combat.resolve_attack(self.cfg, self.rng, pp, ep)

        if not res["hit"]:
            self._log(f"你扑了个空。{enemy['name']}擦着你的手滑了过去。")
        else:
            enemy["hp"] -= res["dmg"]
            verb = "开枪命中" if ranged else "砸中"
            if res["crit"]:
                self._log(f"** 暴击 ** 你{verb}{enemy['name']}，造成 {res['dmg']} 点伤害。")
            else:
                self._log(f"你{verb}{enemy['name']}，造成 {res['dmg']} 点伤害。")

            if enemy["hp"] <= 0:
                await self._kill_enemy(enemy, ranged)

        await self._enemy_round()

    def _damage_weapon(self, amount: int) -> None:
        st = self.state
        w = st.get("weapon")
        if not w or w.get("durability") is None:
            return
        w["durability"] = max(0, int(w["durability"]) - amount)
        if w["durability"] == 0:
            self._log(f"{self.cfg.item(w['id'])['name']}快断了，挥起来没多少力道。")

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
                loot.grant(self.cfg, st, iid, qty)
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
                st["hp"] -= dmg
                noise.add(self.cfg, st, ab.get("noise", 0))
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

            st["hp"] -= res["dmg"]
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
        if combat.try_flee(self.cfg, self.rng, agi, fastest):
            st["in_combat"] = False
            noise.add(self.cfg, st, "sprint")
            self._log("你转身就跑，把它们甩在了身后。（噪音 +2）")
            self._check_horde()
        else:
            self._log("你没能甩掉它们。")
            await self._enemy_round()

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
            st["stamina"] = min(
                self.cfg.balance["player"]["stamina"],
                st["stamina"] + int(item["heal_stamina"]),
            )
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
            st["armor"] = {"id": iid}
            loot.remove(st, iid, 1)
            if old:
                loot.grant(self.cfg, st, old["id"], 1)
            self._log(f"你穿上了{self.cfg.item(iid)['name']}。")
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
            loot.grant(self.cfg, st, outcome["item"], 1)
            self._log(f"获得 {self.cfg.item(outcome['item'])['name']}。")
        if outcome.get("loot_category"):
            for iid, qty in loot.roll_loot(
                self.cfg, self.rng, st, outcome["loot_category"], 1
            ):
                loot.grant(self.cfg, st, iid, qty)
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
        """遗骸交互：搜刮 / 掩埋 / 离开。"""
        st = self.state
        room = mapgen.current_room(st["level_map"])
        grave = st["room"].get("grave")
        if not grave:
            self._log("这里没有遗体。")
            return
        act = payload.get("choice")

        if act == "loot":
            room["resolved"] = True
            st["room"].pop("grave", None)
            noise.add(self.cfg, st, 2)
            if self.rng.chance(0.6):
                cat = self.rng.choice(["ammo", "medical", "trinket"])
                for iid, qty in loot.roll_loot(self.cfg, self.rng, st, cat, 1):
                    loot.grant(self.cfg, st, iid, qty)
                    self._log(f"你从尸体上取走了 {loot.describe(self.cfg, iid, qty)}。")
                self._log("（噪音 +2）")
            else:
                self._log("口袋是空的。你白冒了一次险。（噪音 +2）")
            # 守尸者
            if self.rng.chance(0.6):
                enemies = [combat.make_enemy(self.cfg, "ghoul", st["depth"])]
                st["combat"] = {"enemies": enemies, "round": 0}
                st["in_combat"] = True
                self._log("身后传来低吼。有东西一直守着这具尸体。")
        elif act == "bury":
            room["resolved"] = True
            st["room"].pop("grave", None)
            old, new = self._add_infection(-5)
            st["humanity"] += 5
            self._log("你用碎石和碎布盖住了他。（感染 −5，人道 +5）")
        else:
            self._log("你退了出去。")

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
        st["stamina"] = min(
            self.cfg.balance["player"]["stamina"], st["stamina"] + int(tpl.get("stamina", 0))
        )
        self._log(f"你在火边坐了一会。（HP +{st['hp'] - before}，感染 {new - old}）")

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
        for e in st["inventory"]:
            item = self.cfg.item(e["id"])
            inventory.append({
                "id": e["id"],
                "name": item["name"],
                "qty": e["qty"],
                "kind": self.cfg.item_kind(e["id"]),
                "wearable": self.cfg.item_kind(e["id"]) in ("weapon", "armor"),
                "usable": self.cfg.item_kind(e["id"]) == "consumable",
                "durability": e.get("durability"),
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
                    "durability": (st.get("weapon") or {}).get("durability"),
                    "ranged": bool(wcfg and wcfg.get("kind") == "ranged"),
                },
                "armor": self.cfg.item(st["armor"]["id"])["name"] if st.get("armor") else None,
                "ammo": ammo,
                "inventory": inventory,
                "room": {
                    "name": st.get("room", {}).get("name", ""),
                    "type": st.get("room", {}).get("type", ""),
                    "idx": st.get("room", {}).get("idx", 0),
                    "total": lmap.get("total", 0),
                    "searched": st.get("room", {}).get("searched", False),
                },
                "exits": exits,
                "in_combat": bool(st.get("in_combat")),
                "enemies": enemies,
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
            for e in st["inventory"]:
                if self.cfg.item_kind(e["id"]) == "consumable":
                    acts.append({
                        "id": "use", "label": f"用{self.cfg.item(e['id'])['name']}",
                        "item": e["id"], "kind": "ghost",
                    })
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

        # 手电耗尽时搜刮必然失败——那就不要给这个按钮。
        # 否则玩家（和自动模拟）会陷入"反复点击无效的搜刮"的死循环。
        if not r.get("searched") and level_rules.can_search(self.cfg, st)[0]:
            acts.append({"id": "search", "label": "搜刮这里", "kind": "primary"})

        for e in room.get("exits") or []:
            acts.append({"id": "move", "label": e["label"], "to": e["to"], "kind": "move"})

        for e in st["inventory"]:
            kind = self.cfg.item_kind(e["id"])
            if kind == "consumable":
                acts.append({
                    "id": "use", "label": f"用{self.cfg.item(e['id'])['name']}",
                    "item": e["id"], "kind": "ghost",
                })
            elif kind in ("weapon", "armor"):
                acts.append({
                    "id": "equip", "label": f"换上{self.cfg.item(e['id'])['name']}",
                    "item": e["id"], "kind": "ghost",
                })

        acts.append({"id": "status", "label": "查看状态", "kind": "ghost"})
        acts.append({"id": "give_up", "label": "放弃这一局", "kind": "ghost"})
        return acts


__all__ = ["RunEngine", "RunEnded"]
