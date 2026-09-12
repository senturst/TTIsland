# configs/balance.yaml — 全局平衡常量

数值总闸。调手感优先改这里，不要散到代码里。`hit_floor / hit_ceiling` 钳制命中率上下限（永不出必中/必空）；`dot_per_room / dot_per_turn` 为每进一房/每回合末固定扣血。

## 顶层结构
`config_version` / `player` / `progression` / `combat` / `infection` / `noise` / `legacy` / `loot` / `loot_cash` / `merchant` / `survivor_npc` / `mapgen` / `growth` / `targets` / `scoring`

---

## `config_version` (int, 默认 1)
配置版本号，用于热重载/迁移判断。

## `player` — 玩家基础属性
数值经 `scripts/sim.py` 上千局模拟校准。全程期望承伤约 200，可用血池（初始 HP + 沿途治疗）约 120，差额靠“逃跑/绕路/用手电省遭遇”补齐。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `hp` | int | 40 | 初始生命上限。 |
| `stamina` | int | 20 | 初始体力。 |
| `strength` | int | 6 | 力量（伤害加成分母见 `combat.str_divisor`）。 |
| `agility` | int | 5 | 敏捷（影响先手/逃跑）。 |
| `acc` | int | 88 | 基础命中。 |
| `eva` | int | 12 | 基础闪避。 |
| `crit` | float | 0.05 | 基础暴击率。 |
| `crit_mult` | float | 1.8 | 暴击伤害倍率。 |
| `armor` | int | 0 | 基础防御（无护甲时）。 |
| `ammo_start` | int | 12 | 开局弹药量（可被天赋 `ammo_start` 叠加）。 |
| `start_weapon` | string | crowbar | 开局武器 ID（必须在 `items.yaml` 存在）。 |
| `start_items` | list[[id, qty]] | [[bandage,1]] | 开局消耗品。 |
| `bag_slots` | int | 8 | 背包基础容量（格）。总容量 = 此值 + 背包天赋 + 已装备背包 `slots` + 护甲 `pockets`。可堆叠物资只占 1 格（qty 累加），天然限制无限囤积。 |

## `progression` — 层间喘息
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `descend_heal_pct` | float | 0.12 | 下楼时按最大生命比例回血。让每层成为可独立校准的单元（否则前面少血后面全线崩）。 |

## `combat` — 战斗
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `hit_floor` / `hit_ceiling` | int | 5 / 95 | 命中率钳制上下限（%）。 |
| `str_divisor` | int | 2 | 力量加成分母：`floor(STR / str_divisor)` 加进伤害。 |
| `flee_base` / `flee_per_agi` / `flee_clamp` | int/int/[int,int] | 40 / 5 / [10,90] | 逃跑成功率：`base + per_agi*(玩家AGI-敌AGI)`，被 `flee_clamp` 钳制。 |
| `flee_fail_punish_chance` | float | 0.75 | 逃跑失败时敌人免费打一次的概率。 |
| `flee_stamina_cost` | int | 5 | 逃跑消耗体力（无论成败都扣）。 |
| `flee_per_enemy` | int | 2 | 每只额外敌人（超出 1 只的部分）−2% 逃跑率，被 `flee_clamp` 钳制。 |
| `flee_stamina_bonus_pct` | float | 0.2 | 每点当前体力额外 +0.5% 逃跑率（满体力多 50%），被 `flee_clamp` 钳制。 |
| `brace_stamina_cost` / `brace_acc_bonus` / `brace_turns` | int/int/int | 5 / 15 / 3 | 瞄准动作：消耗体力换临时命中加成（设计红线：绝不降低基础命中）。 |
| `armor_absorb` | map | — | 防具按“等级”百分比吸伤（取代固定减伤）。`tiers: {1:0.10, 2:0.18, 3:0.25}`（tier→吸伤%），`min_absorb: 1`（每次受击至少吸 1 点）。每只怪独立结算：先按 tier 比例吸伤（至少 `min_absorb`），吸掉的量转防具耐久损耗；耐久耗尽则不再吸伤。 |

## `infection` — 感染
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `max` | int | 100 | 感染上限（到 100 尸化）。 |
| `bands` | list[map] | — | 感染档。每项 `{min, name, hp_max_pct?, acc?, dot_per_room?, dot_per_turn?, dmg_pct?, npc_hostile?, zombify?}`。min=25 低烧(−10%血上限,−5命中)；min=50 溃烂(−25%血上限,−10命中,每房+1感染)；min=75 狂躁(+10命中,+20%伤,每回合+2感染, NPC 敌对)；min=100 尸化(zombify)。75+ 给增益而非纯惩罚，让高感染成为可玩策略位。 |
| `zombify` | map | `{max_rooms:3, stat_mult:1.3}` | 尸变模式：选“继续”后的存活房间数与能力加成。 |
| `sources` | map | `{bite:[3,6], blood_pool:[2,4], raw_food:5}` | 各类感染来源增量区间。 |
| `relief` | map | `{antibiotic:-25, campfire:-10, clean_water:-5, bandage:-5}` | 各缓解手段的感染减量。 |

## `noise` — 噪音 / 尸潮
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `max` | int | 10 | 噪音上限（地区 1；地区 2 由 `regions.N.noise_max` 覆盖）。 |
| `keep_ratio_on_enter` | float | 0.1 | 进入新层时噪音保留比例（0.1=降 90%）。防“来回走房间刷衰减洗白噪音”。 |
| `decay_per_room` | float | 0.0 | 每移动一个房间自然衰减量。 |
| `sources` | map | `{gunshot:4, silenced:1, gun_kill:3, melee_kill:0, pry:2, sprint:2, glass:1}` | 各类行为产生的噪音。 |
| `horde` | map | — | 尸潮参数：`threshold:8`（触发线）、`end:4`（平息线）、`extra_spawn:[3,6]`、`density_mult:2.0`、`clear_noise_cut:0.30`（打完削噪音比例）、`elite`（守门精英：`monster:gatekeeper`、`monsters:{1:gatekeeper,2:sergeant}`、`no_flee:true`、`skip_levels:[1]`、`normal_monster:ghoul`）。地区精英按 `monsters` 覆盖全局默认。 |

## `legacy` — 遗物继承（软 Roguelite）
三道闸防滚雪球：tier 上限、传承衰减、代次上限。`escape` vs `death` 留下质量不同（见注释）。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `max_tier` | int | 2 | 可继承装备 tier 上限（重火力不能带走）。 |
| `max_passes` | int | 3 | 传承代次上限（满代直接报废）。 |
| `weapon_dmg_mult` | float | 1.0 | 每传承一代的伤害倍率。 |
| `durability_mult` | float | 0.75 | 每传承一代的耐久倍率。 |
| `armor_penalty` | int | 1 | 每传承一代护甲值扣减。 |
| `escape` | map | — | 撤离收益：`keep_passes:true`（不增传承次数）、`restore_durability:true`（耐久回满）、`stipend:{ammo:[ammo_t2,6], item:bandage}`（撤离津贴）。 |

## `loot` — 掉落
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `category_weights` | map | `{ammo:20, food:20, medical:20, material:22, trinket:10, gear:8}` | 掉落大类权重。 |
| `ammo_per_drop` | [int,int] | [2,4] | 每次弹药掉落数量区间（每层期望 6–10 发）。 |
| `category_tables` | map[list] | 见文件 | 各类别对应的物品 ID 表（ammo/food/medical/material/trinket/gear + `military_supplies:[ballistic_plate]`）。`military_supplies` 为地区 2 军械补给，物品带 `min_region:2` 双重限区。 |
| `broken_weapon_mult` | float | 0.5 | 近战武器耐久归零后伤害倍率。 |
| `search` | map | `{max_items:3, extra_base_chance:0.3, extra_decay:0.2}` | 搜索多件：拿到第 1 件后按 `p·decay^(k-1)` 续 roll，直到失败或到顶 `max_items`（废铁/装备来源核心）。 |

## `loot_cash` — 现金掉落
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `chance` | float | 0.12 | 每次搜索独立判定给现金的概率。 |
| `min` / `max` | int | 1 / 5 | 现金数量区间。现金是独立计数资源（`state["cash"]`），不进背包不占格，不走类别权重。 |

## `merchant` — 商人
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `repair` | map | `{scrap_per_point:0.3, cash_per_point:0.5, tape_points:2, target:both}` | 修理：每 1 废料修 3 点（0.3/点），每 1 现金修 2 点，每 1 胶带修 2 点（固定点数）。`target: both`=修近战武器与护甲。`target` 出现在 `merchant.repair` 段。非商人区域也可用废料修。天赋 `repair_bonus` 让一次多修几点。 |
| `shop_slots` | map | `{weapon:1, gear:1, backpack:1, other:3}` | 铺货模板：1 武器位 / 1 装备位（护甲或背包）/ 1 背包位 / 3 其他位。 |
| `other_pool` | list | 见文件 | 其他位候选池（ammo/consumable/material/trinket，含 `crowbar`）。 |
| `sell_ratio` | float | 0.5 | 卖出价 = `value × sell_ratio`（向下取整，至少 1）。 |
| `min_durability_ratio` | float | 0.5 | 收购门槛：武器/护甲耐久低于总耐久该比例时拒收（0 耐久必拒）；无耐久概念物品不受限。 |
| `authors_mercy_chance` | float | 0.01 | 作者怜悯：极低概率触发，仅普通商人，玩家免费任选一件。 |
| `plagued` | map | `{spawn_chance:0.15, toll_hp:20, discount:0.5}` | 感染商人：进入后上交 `toll_hp` 生命换下一次购买半价（一次性，用掉可再交）。`spawn_chance`=普通商人房出现感染商人概率。 |

## `survivor_npc` — 幸存者 NPC（P6.2.2）
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `spawn_chance` | float | 0.18 | 每层主干房间替换为 NPC 的概率（每层至多 1 个，L2+）。 |
| `min_level` | int | 2 | 第 1 层不放（新手别一上来撞活人）。 |
| `stock_rolls` | int | 2 | 铺货件数（从 `merchant.other_pool` 抽，只收废料）。 |

## `mapgen` — 地图生成
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `rooms_base` / `rooms_rand` | int/int | 8 / 6 | 房间数 = `base + rand(0, rand)`。 |
| `main_path_ratio` | float | 0.6 | 主干长度占比。 |
| `branch_min` / `branch_max` | int/int | 1 / 2 | 每个分支挂 1–2 房（死胡同）。 |
| `branch_ratio_by_level` | map | `{1:0.20 … 10:0.40}` | 高层分支占比更高，探索收益随深度上升（含地区 2 的 6–10）。 |
| `room_weights` | map | `{main:…, branch:…}` | 主干/分支上的房间类型权重。`hazard/nest` 只进分支（绕路赌一把，不堵主干）。 |
| `merchant_guarantee` | map | `{1:0,2:1,3:0,4:1,6:1,7:0,8:1,9:0}` | 每层必出的商人房数量（key=层）。不足时从普通房随机改铸补足。撤离层不保底。 |

## `growth` — 升级系统（P7）
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `xp_base` | int | 60 | 升到第 1 级所需 XP。 |
| `xp_curve` | float | 1.4 | 每级需求 ×1.4（60→84→118→165）。击杀 XP 来自 `monsters.yaml` 的 `xp`；守门精英/Boss 击杀直接升 1 级。 |

## `targets` — 平衡目标
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `pass_rate_by_level` | map | 见文件 | 每层通过率目标（地区 1：0.88/0.76/0.65/0.55/0.45；地区 2 压一档：0.80/0.68/0.57/0.47/0.37）。连乘=整局撤离率（约 10%）。 |
| `tolerance` | float | 0.06 | 超过该偏差就认为需要回调。 |
| `max_legacy_gain` | float | 0.05 | 雪球容忍度：拿到遗物后整局撤离率提升不应超过此值（保证第一轮通关≠第二轮稳过）。 |

## `scoring` — 计分
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `per_kill` / `per_level` / `escape_bonus` / `boss_kill_bonus` | int | 8 / 50 / 200 / 150 | 击杀分 / 深度分 / 撤离奖励 / Boss 击杀奖励。 |
| `ammo_score` / `trinket_score` | int | 1 / 15 | 剩余弹药 / 纪念品折算分。分数 = 击杀分 + 深度分 + 撤离/Boss 奖励 + 剩余物资折算。 |
