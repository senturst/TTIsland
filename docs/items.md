# configs/items.yaml — 物品表

全部数值写死；只有 `desc_ai: true` 的条目其描述文本由 AI 生成。所有物品按种类分段：`weapons` / `ammo` / `consumables` / `materials` / `armor` / `trinkets` / `backpacks`，段尾有 `legacy_exclude_kinds`。

## 跨段通用字段
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一 ID（加载校验：开局武器/掉落表/事件引用都必须存在）。 |
| `name` / `icon` | string | 展示名 / emoji。 |
| `weight` | int | 重量（影响背包负重逻辑）。 |
| `tier` | int | 稀有度/强度档（1–3；遗物继承 `max_tier:2` 封顶）。 |
| `value` | int | 价值（商人回收/出售价、价值展示基准）。 |
| `desc_ai` | bool | 描述是否由 AI 生成。 |
| `min_region` | int（可选） | **产出绑定**：仅在 ≥ 该地区的层级产出（与掉落过滤同机制）。如 `ammo_t4`/`assault_rifle`/`military_vest`/`military_pack`/`ballistic_plate` 为 `2`（地区 2 专属）；`min_region: 99` 表示暂无产出渠道（预留档）。无此字段 = 所有地区均可出现。 |
| `rare` | bool（可选） | 稀有标记（如 `antibiotic`/`medkit` 只在医疗掉落表出现）。 |

> ⚠️ **区域限制约定**：本文件用 `min_region`（最低地区）做单向限区，不是 `regions:[...]`。地区 1 物品（无 `min_region`）在所有地区都可出现；地区 2 专属物品靠 `min_region: 2` 不外溢到地区 1。

## `weapons` — 武器
| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | string | `melee`（近战）或 `ranged`（远程）。 |
| `dmg` | [int,int] | 基础伤害区间（未算力量与护甲）。 |
| `acc_mod` | int | 命中修正，直接加进命中公式（如 `+10`/`-5`）。 |
| `crit` | float | 暴击率。 |
| `noise` / `noise_key` | — | 近战用 `noise: 0`（不产生噪音）；远程用 `noise_key`（引用 `balance.noise.sources` 的键，如 `gunshot`/`silenced`）。 |
| `durability` | int/null | 近战武器可用次数，归零后伤害 ×`balance.broken_weapon_mult`。`null`=无耐久概念（永不磨损，如拳头）。 |
| `pry` | bool（可选） | 是否可作撬棍（撬门/撬箱）。 |
| `ammo_type` / `ammo_per_shot` | string/int | 枪械消耗弹种（按 tier 统合 `ammo_tN`）与每发消耗。 |
| `burst` | [int,int]（可选） | 连射：一次攻击随机射出 [min,max] 发，每发独立结算伤害与命中。 |
| `aoe` | bool（可选） | 远程全体攻击（霰弹枪类）：一次消耗 `ammo_per_shot` 发，对每只怪单独结算。 |
| `mag_size` | int（可选） | 弹匣容量（展示/逻辑用）。 |

武器列表：拳头(fists,兜底近战)、撬棍(crowbar,开局武器,pry)、消防斧(fire_axe,tier3,pry)、砍刀(machete)、M9手枪(pistol_m9,tier2)、霰弹枪(shotgun,tier3,aoe)、消音冲锋枪(silenced_smg,tier3,消音+连射)、突击步枪(assault_rifle,**地区2**,tier3,gunshot)、冲锋枪(smg,tier2)、棒球棍/活动扳手(pry)/匕首/剁骨刀/长矛(tier2)、钉枪(nail_gun,tier1,消音)、双管霰弹(double_barrel,tier2,aoe)。

## `ammo` — 弹药
按 tier 统合（P9 重做）：t1–t3 通用，t4–t6 为预留档（`min_region: 99` 暂无渠道）。
| 字段 | 类型 | 说明 |
|------|------|------|
| `qty` | [int,int] | 单次拾取数量区间（统一由掉落表/商店出货）。 |
| `dmg_mult` | float | 每发子弹伤害百分比（射击时按弹匣内弹种乘算）。当前档位：t1 0.90 / t2 1.00 / t3 1.05 / t4 1.15 / t5 1.25 / t6 1.50。 |
| `min_region` | int | 产出绑定（见跨段说明）。t1–t3 = 1；t4 = 2；t5/t6 = 99。 |

## `consumables` — 消耗品
通用效果字段（按存在与否生效）：`heal`([int,int]回血)、`heal_stamina`(回体力)、`infection`(感染增减，可负)、`flashlight`(手电电量)、`buff`(临时增益 map，如 `{acc, taken_dmg, turns}`)、`armor_plate`(防弹插板加成，见下)。
- **`ballistic_plate`（防弹插板，地区 2 专属）**：`armor_plate: 20`、`min_region: 2`。`_act_use` 消费——只对 `plate_compatible` 护甲生效，给防具 +20 耐久（无维修惩罚，即不降耐久上限），仅限防弹衣类。
- 其他：`bandage`/`antibiotic`(rare)/`canned`/`clean_water`/`vodka`(buff+感染)/`battery`(手电)/`medkit`(rare)/`painkiller`/`disinfectant`/`energy_drink`。

## `materials` — 材料
`scrap`(废铁，修武器)、`duct_tape`(胶带，修甲)、`alcohol`(酒精)。
> ⚠️ 现金（`cash`）**不是物品**，是独立计数资源（`state["cash"]`），不要加回任何物品段/掉落表/商店池——历史上放进 materials 曾导致它混进背包且可被丢弃，`loader._validate` 会拦截。

## `armor` — 护甲
| 字段 | 类型 | 说明 |
|------|------|------|
| `armor` | int | 防御值（吸伤 tier 由 `tier` 决定，见 `balance.combat.armor_absorb`）。 |
| `eva` | int（可选） | 闪避修正（通常为负，重甲减机动）。 |
| `noise_mod` | int（可选） | 噪音修正（重甲 +1）。 |
| `pockets` | int（可选） | 口袋数（增加背包容量格）。 |
| `durability` | int | 吸伤转耐久损耗；耗尽后失去吸伤。 |
| `plate_compatible` | bool（可选） | 是否兼容防弹插板（`ballistic_plate`）。当前：`riot_gear`(防暴服)、`tactical_vest`(战术背心)、`military_vest`(军用防弹服,**地区2**) 为 `true`。 |

护甲列表：皮夹克(tier1)、防暴服(tier2,plate_compatible)、战术背心(tier2,plate_compatible)、自行车头盔(tier1)、军用防弹服(military_vest,**地区2**,tier3,plate_compatible)。
> 防弹衣维修：可用胶带维修，但每次修耐久上限 −1（`run_service._do_repair`）；防弹插板 +20 耐久不降上限。

## `trinkets` — 纪念品
无属性，纯计分物件：`wedding_ring`(score15)/`dog_tag`(score12)/`photo`(score10)。可一直传承（不衰减，情感物件）。

## `backpacks` — 背包
`slots` = 装入后**额外**增加的容量格数（基础 10 格由 `balance.player.bag_slots` 提供）。背包是装备：可点击装备、商人购买、遗物继承。
列表：帆布背包(small_pack,slots4,tier1)、登山包(hiking_pack,slots6,tier1)、战术背包(large_pack,slots8,tier2)、军用背囊(military_pack,**地区2**,slots10,tier3)。

## `legacy_exclude_kinds`
| 字段 | 类型 | 说明 |
|------|------|------|
| `legacy_exclude_kinds` | list[str] | 不可作为遗物继承的种类：`[consumables, ammo, cash]`。消耗品/弹药不可继承；现金是局内货币同样不继承。 |

## 校验（`loader._validate`）
- 开局武器（`balance.player.start_weapon`）与 `start_items` 引用的物品必须存在。
- `loot.category_tables` 引用的物品必须存在，且不得含 `cash`。
- `merchant.other_pool` 等引用池不得含 `cash`。
- 事件/房间模板引用的物品、怪物、掉落类别必须存在。
