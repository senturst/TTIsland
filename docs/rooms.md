# configs/rooms.yaml — 房间模板

房间有向图 + 模板拼装。主干路径线性串联（入口→楼梯），主干节点上挂 1–2 房的死胡同分支放高价值奖励。分支占比随层数上升。结构/权重/数值写死；`atmosphere_prompt` 指向提示词文件名，描述由 AI 生成。

`combat.enemy_bonus`：在遭遇表 count 基础上额外加几只。`loot.tables`：引用 `balance.yaml` 的 `loot.category_tables` 键。`loot.rolls`：掉几次。`special` 房间由 `mapgen` 强制插入，不参与权重抽取（stairs 每层 1 个）。

## 顶层结构
`templates`（按类型分组的房间模板）与尾部 `lockable_room`。`templates` 含：`combat` / `loot` / `empty` / `event` / `grave` / `merchant` / `hazard` / `nest` / `special`。

## 模板公共字段
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 模板唯一 ID。 |
| `name` | string | 展示名。 |
| `level_min` / `level_max` | int | 该模板可出现的层区间（默认 1 / 99）。`mapgen._pick_template` 按此筛选；**不支持排除**，需要排除请加 `level_exclude`（当前代码未实现，见下）。 |
| `level_exclude` | list[int]（可选，代码未支持） | 应排除的层号。当前 `_pick_template` 只支持 `level_min/level_max` 区间，若要“医院层不出枪店”这类需求，需在 `mapgen._pick_template` 扩展支持此键。 |
| `enemy_bonus` | int | 遭遇表 count 额外加几只。 |
| `vibe` | string | 房间氛围（AI 提示词依赖，加载校验必填）。 |
| `tables` | list[str] | 引用 `balance.loot.category_tables` 的键（如 `medical`/`ammo`/`weapons`）。`weapons` 走独立掉落逻辑。 |
| `rolls` | [int,int] | 掉落次数区间。 |
| `pry_required` | bool | 是否需要撬棍才能开。 |
| `noise_on_pry` / `noise_on_enter` | int | 撬开 / 进入产生的噪音。 |
| `infection_on_enter` | [int,int] | 进入产生的感染区间。 |
| `light_cost` | int | 进入耗手电电量。 |

## 各类型要点

### `combat`（战斗房）
街道伏击点(1-2)、地下车库(2-4)、住院病房(3-5，进房+感染)、商场走廊(4-5，进房+噪音)、天台平台(5)、哨卡残址(6-8)、隔离军帐(6-8，+感染)、装甲车场(7-9，+噪音)、指挥楼层(9-10)。

### `loot`（物资房）
药房(medical)、枪店(ammo,weapons,需撬,+2噪音)、超市(food)、废弃车辆(material,ammo,需撬)、更衣室(trinket,medical)；地区 2：军械柜(weapons,ammo,需撬)、军需补给箱(ammo,food,medical)、军械库房(weapons,gear,需撬,+3噪音)。
> 注意：`armory_locker` / `field_armory` 当前无 `level_min/max`，会全局出现（含地区 1），如需“地区 1 不出军械库”应改 `level_min:6 level_max:10`。`gun_store` 无 `level_exclude`，医院层(3)也会出枪店。

### `empty` / `event` / `grave`
空房（大厅/楼梯间/办公室）、事件锚点、无名遗骸（墓碑占位）。

### `merchant`
流浪商人 `wandering_trader`：`level_min:1 level_max:10`（全局）。走 `mapgen` 的 `room_weights` 生成，正常参与权重抽取。

### `hazard`（灾害房，P6.2.2）
进房立刻吃“开场伤害”，之后每个行动推进 `countdown`，归零触发 `worsening`。`countdown`（回合数）、`onset`/`worsening`（效果，`infection`/`cut`/`noise`/`heal`）、`choices`（加权 outcomes）。毒气渗漏(1-10)、坍塌现场(2-10)。

### `nest`（变异巢穴，P6.2.2）
进房必遇敌（`enemy_bonus≥1`），清巢后自动“割巢”拿高价值掉落。`harvest_tables`/`harvest_rolls`/`harvest_quality`/`harvest_cash`。变异巢穴(1-3)、孵育腔(4-5)、军犬舍(6-9，地区 2)。只进分支权重。

### `special`
`stairs_down`（楼梯）、`campfire`（篝火：heal/infection/stamina，once_per_level，ambush_chance）、`survivor_npc`（幸存者：hostile_if_infection_ge、trade、scrap_rate、share_humanity、buy_humanity、gift_chance）。

## `lockable_room`（锁门躲避，第 4 层商场机制）
| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` / `name` | string | shutter_room / 卷帘门仓库 | — |
| `levels` | list[int] | [4] | 出现的层。 |
| `duration` | int | 3 | 躲避回合数。 |
| `pry_cost` | int | 1 | 消耗撬棍耐久。 |
| `noise_reduction` | int | 4 | 噪音削减量。 |

## 校验（`loader._validate`）
- 每个模板必须有 `vibe`（否则 AI 提示词缺料）。
- `tables` 引用的类别必须存在于 `balance.loot.category_tables`（`weapons` 例外，走独立逻辑）。
- `hazard` 的 `countdown>0`、choices 概率闭合、掉落类别合法。
- `nest` 的 `enemy_bonus≥1`、收获类别合法。
