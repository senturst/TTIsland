# configs/regions.yaml — 地区系统

P6.2.1 / P8 实装“地区”概念。每个地区 = 连续的一组层。层的主题/机制细节在 `level_themes.yaml`，这里定义“世界结构”：哪几层属于哪个地区、地区 Boss 是谁、噪音预算、展示信息。

**解锁规则**：玩家从某地区最后一层（直升机撤离点）撤离成功后，解锁下一地区。多周目循环的设计立场：已解锁地区可反复刷，后续地区难度不降——解锁是“进门资格”，不是“变强保证”。

`boss`：该地区撤离点（最后一层）驻守的地区 Boss，击杀后才能登机。从地区最后一层的 `level_theme.boss` 段迁移至此——Boss 是地区身份的一部分。

## 顶层结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `regions` | map[int → region] | 地区索引（key = 地区 ID，1、2…）。placeholder 地区只是结构占位，不进索引（玩家查不到、校验不碰）。 |

## `regions[N]` 字段

| 字段 | 类型 | 说明 | 示例（地区 1 / 2） |
|------|------|------|-------------------|
| `id` | int | 地区 ID（与 map key 一致）。 | 1 / 2 |
| `name` | string | 地区名（展示）。 | 疫城废墟 / 军事检疫营地 |
| `icon` | string | emoji 图标。 | 🏚️ / 🎖️ |
| `subtitle` | string | 副标题。 | 地区一 · 起点之城 / 地区二 · 封锁线之外 |
| `brief` | string | 地区简介（叙事）。 | — / — |
| `levels` | list[int] | 该地区包含的层号（连续）。引擎据此建立“层 → 地区”反查索引。 | [1,2,3,4,5] / [6,7,8,9,10] |
| `boss` | string | 该地区撤离点守军怪物 ID（击杀后才能登机）。 | tyrant_t03 / horde_marshal |
| `noise_max` | int（可选） | 该地区噪音上限。仅地区 2 配（30）；地区 1 用全局 `balance.noise.max`（10）。远程枪械主场：上限放大后尸潮触发线/平息线/自然衰减按上限同比例放大（×3），改的是“可以更吵”的预算，不是尸潮频率。 | 30（仅地区 2） |

## 消费方 / 查询辅助
- `loader.GameConfig.region_id_for_level(level)` → 层所属地区 ID（未归属视为 1）。
- `loader.GameConfig.region_for_level(level)` → 地区对象。
- `loader.GameConfig.last_level_of(level)` → 所在地区最后一层（撤离点 / 地区 Boss 层）。
- `loader.GameConfig.region_boss(rid)` / `region_last_level(rid)` / `region_unlocked(rid, progress)`。
- `mapgen.generate_level` 用 `last_level_of` 判定撤离层（每地区一个，而非全局末层）。
- `noise` 模块读 `noise_max` 决定该地区噪音预算。

## 现有地区
- **地区 1（疫城废墟）**：1–5 层，Boss `tyrant_t03`，噪音上限 10（全局默认）。
- **地区 2（军事检疫营地）**：6–10 层，Boss `horde_marshal`，噪音上限 30（远程枪械主场）。
