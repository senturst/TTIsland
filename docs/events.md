# configs/events.yaml — 随机事件

结构/选项/概率/数值全部写死。`{ai_desc}` 是唯一的 AI 插槽（房间氛围文本）。`outcomes` 为加权结果表，`p` 之和应为 1.0（加载时校验）。

## 顶层结构
`events`：事件数组，每个事件一个对象。

## `events[]` 公共字段
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 事件唯一 ID。 |
| `name` | string（可选） | 展示名（部分事件有）。 |
| `weight` | int | 抽取权重。 |
| `levels` | [int,int] | 该事件出现的层区间（扁平区间，如 `[1,10]`/`[3,5]`/`[6,10]`）。如需地区化收紧，可改为对应地区层区间。 |
| `text` | string | 事件描述（`{ai_desc}` 占位）。 |
| `choices` | list | 选项数组，见下。 |

### `choices[]` 字段
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 选项 ID。 |
| `label` | string | 选项展示文本。 |
| `noise` | int（可选） | 选择该选项产生的噪音。 |
| `require_pry` | bool（可选） | 是否需要撬棍（如无撬棍不可选）。 |
| `outcomes` | list | 加权结果表，见下。 |

### `outcomes[]` 字段（每项是一个可能结果）
| 字段 | 类型 | 说明 |
|------|------|------|
| `p` | float | 概率，同一 choice 内 `p` 之和须 = 1.0。 |
| `text` | string | 结果描述。 |
| `loot_category` | string（可选） | 按掉落表给物品（引用 `balance.loot.category_tables`）。 |
| `quality` | int（可选） | 掉落品质档加成（优先给稀有物）。 |
| `item` | string（可选） | 直接给指定物品（必须在 `items.yaml` 存在，加载校验）。 |
| `infection` | [int,int] / int（可选） | 感染增量（区间或固定）。 |
| `heal` | [int,int]（可选） | 回血区间。 |
| `cut` | [int,int]（可选） | 割伤（直接掉血）区间。 |
| `noise` | int（可选） | 噪音增量。 |
| `spawn` | string（可选） | 生成怪物 ID（须在 `monsters.yaml` 存在）。 |
| `count` | int（可选） | 生成怪物数量（默认 1）。 |
| `info` | bool（可选） | 是否揭示信息（如撤离点位置）。 |
| `humanity` | int（可选） | 人道值增减（埋尸等善意行为 +5）。 |

## 现有事件速查
| id | 层区间 | 主题 |
|----|--------|------|
| `blood_pool` | 1–10 | 血泊翻尸 |
| `locked_door` | 1–10 | 锁死的门（撬/敲/离开） |
| `broken_vending` | 1–10 | 自动售货机 |
| `radio_signal` | 2–10 | 收音机（听/砸） |
| `survivor_body` | 1–10 | 新鲜尸体（拿装备/埋/离开） |
| `antibiotic_cache` | 3–10 | 医药箱（必出抗生素或绷带） |
| `glass_field` | 1–3 | 碎玻璃地面 |
| `survivor_camp` | 1–10 | 幸存者营地（休息/翻物资） |
| `hospital_broadcast` | 3–5 | 医院广播（P8 补充） |
| `military_airdrop` | 6–10 | 军用空投（P8 补充，地区 2） |
| `mall_mannequin` | 4–6 | 商场人体模型（P8 补充） |

## 校验（`loader._validate`）
- `levels` 区间合法（1 ≤ lo ≤ hi ≤ max_level）。
- 每个 choice 的 `outcomes` 概率和 = 1.0。
- `item` 引用的物品、`spawn` 引用的怪物必须存在；`loot_category` 必须是合法掉落类别。
