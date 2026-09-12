# configs/level_themes.yaml — 层主题配置

定义每一层的主题、机制与叙事文本。设计立场：每层靠“唯一机制”区分，而不是把数值线性放大；数值只有小幅增长，改变的是玩家的决策结构。层的“世界结构”（哪几层属于哪个地区）在 `regions.yaml`，机制细节在此。

`hooks` 由 `core/level_rules.py` 消费：
- `on_enter_level` —— 进入该层时执行一次
- `before_room` —— 每进入一个房间前
- `on_turn` —— 战斗每回合
- `modifier` —— 常驻修正

## 顶层结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `levels` | map[int → level] | 第 1–10 层，每层一个对象，见下方字段。 |
| `boss` | map | 全局/地区 Boss 信息（见底部）。 |

## `levels[N]` 公共字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 层号（与 map key 一致）。 |
| `name` | string | 层名（展示）。 |
| `icon` | string | emoji 图标。 |
| `subtitle` | string | 副标题（如“地表 · 日光尚在”）。 |
| `brief` | string | 层简介（进入时叙事）。 |
| `mechanism` | string | 本层核心机制名（如“日光”“手电电量”）。 |
| `mechanism_desc` | string | 机制说明文本（给玩家看）。 |
| `entry_text` | string | 进入该层时的文本。 |
| `exit_text` | string | 离开该层（下楼）时的文本。 |
| `modifiers` | map | 该层常驻数值修正，键见下方“modifiers 可用键”。 |
| `on_enter_level` | map | 进入层时一次性效果（如 `flashlight: 100`、`evac_countdown: 42`、`infection_decay`）。 |
| `zombie_density` | float | 该层僵尸密度（影响遭遇率，约 0.7–0.97）。 |
| `hooks` | list[string] | 该层启用的机制钩子 ID（如 `flashlight_drain`、`hazmat_zone`、`alarm_system`、`lockable_room`、`evac_countdown`、`boss`）。 |

### `modifiers` 可用键（按层出现）

| 键 | 含义 | 出现在层 |
|----|------|----------|
| `loot_rolls_mult` | 搜刮次数倍率（新手/军械富集更慷慨） | 1（1.5）、8（1.4） |
| `search_extra_flat` | 搜索续 roll 概率额外加成 | 1（+0.2） |
| `flashlight_enabled` / `flashlight_start` / `flashlight_cost_per_room` / `flashlight_cost_per_turn` / `flashlight_dead_acc_penalty` / `flashlight_dead_encounter_mult` / `flashlight_dead_no_search` | 手电系统开关、初始电量、每房/每回合耗电、电量耗尽后的命中惩罚/遭遇倍率/禁搜刮 | 2 |
| `hazmat_room_chance` / `hazmat_infection` / `hazmat_medical_bonus` | 高危污染房概率、进入感染、医疗掉落加成 | 3 |
| `infection_per_turn` / `infection_turn_interval` | 每 N 回合自然 +感染 | 3（每 2 回合 +1） |
| `noise_floor` / `noise_decay_mult` / `loot_quality_bonus` / `lockable_rooms` | 噪音基线、衰减倍率、掉落品质档、可锁门 | 4（警报系统） |
| `evac_countdown` / `boss_room` | 撤离倒计时回合数、是否 Boss 房 | 5（42）、10（36） |

## 各层速查

| 层 | 名称 | 机制 | zombie_density | hooks |
|----|------|------|----------------|-------|
| 1 | 废弃街区 | 日光（搜刮 +50%） | 0.96 | [] |
| 2 | 地下车库 | 手电电量 | 0.97 | flashlight_drain |
| 3 | 中心医院 | 高危污染区 | 0.88 | hazmat_zone |
| 4 | 万达商场 | 警报系统 | 0.70 | alarm_system, lockable_room |
| 5 | 天台 | 撤离倒计时 + 暴君 | 0.69 | evac_countdown, boss |
| 6 | 前哨外围 | 戒严巡逻（密度高） | 0.95 | [] |
| 7 | 铁丝网隔离带 | 隔离带（无特殊机制） | 0.93 | [] |
| 8 | 军械库区 | 军械富集（搜刮 ×1.4） | 0.90 | [] |
| 9 | 指挥中心 | 指挥部卫队（精英多） | 0.88 | [] |
| 10 | 停机坪 | 撤离倒计时 + 葬列 | 0.72 | evac_countdown, boss |

## `boss` 段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | Boss 怪物 ID（地区 1：`tyrant_t03`）。 |
| `name` | string | 展示名。 |
| `room_name` | string | 撤离点房间名。 |
| `guards_evac` | bool | 是否守在撤离点。 |
| `lure` | map | 引开机制配置（已整体下线：`enabled: false`，`success_chance: 0.0`；引擎不再消费，仅保留以兼容）。含 `noise_threshold` / `lure_turns` / `success_chance` / `enabled`。 |
| `encounter_text` / `kill_text` | string | 遭遇 / 击杀文本。 |

> 地区 2 的 Boss（`horde_marshal` / 葬列）定义在 `regions.yaml` 的 `regions.2.boss`，不在本文件。
