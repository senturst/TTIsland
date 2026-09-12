# configs/talents.yaml — 天赋池

P7 升级系统的天赋池。两个来源：
- **开局（复活进场）三选一**：死亡补偿；上一局撤离成功则不触发。
- **局内升级三选一**：击杀 XP 攒条 / 精英·Boss 直升；排除已拥有的。

都只在**单局生效**，死亡清零。多天赋叠加时数值型求和（再拿一次同类的会叠上去）。单天赋对撤离率的提升必须 ≤ +3%（`scripts/sim.py --talents` 逐条验证）。池子要够大（30+），否则一局最多 5 次抽取 ×3 选项 + 排除已有会不够。

`mods` 字段是纯数据，由 `core/talents.py` 聚合读取，各系统自行消费。**新增天赋只改这个文件，不用动代码。**

## 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `draw_count` | int | 3 | 每次抽取向玩家展示的选项数量（即“三选一”的 3）。 |
| `talents` | list | — | 天赋数组，见下方公共字段。 |

## `talents[]` 公共字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一 ID，抽取排除与消费去重都靠它。 |
| `name` | string | 展示名（给玩家看）。 |
| `desc` | string | 效果简述（给玩家看）。 |
| `weight` | int | 抽取权重，越大越常出现。 |
| `mods` | map | 数值修正集合，键见下方“可用 mods 键”。 |

## 可用 mods 键（消费方，来自文件头注释）

`core/talents.py` 聚合后，各系统按 key 读取：

| mods 键 | 含义 | 消费方 |
|---------|------|--------|
| `hp_max` | 生命上限 | `combat.player_profile` / `apply` 即时生效 |
| `start_items` | 开局物资 `[id, 数量]` | `apply` 即时生效 |
| `infection_taken_mult` | 感染乘数 | `run_service._add_infection` |
| `food_infection_bonus` | 吃抑制类物品感染多减 1（仅负感染生效） | `run_service._act_use` |
| `descend_heal_add` | 下楼回血加成（比例） | `_descend` |
| `melee_dmg_pct` / `ranged_dmg_pct` | 近战 / 枪械伤害 % | `combat.player_profile` |
| `crit` / `acc` / `eva` / `armor` | 暴击 / 命中 / 闪避 / 防御 | `combat.player_profile` |
| `low_hp_dmg_pct` / `low_hp_threshold` | 亡命之徒类（低血增伤） | 战斗结算 |
| `loot_extra_roll_chance` | 搜索多掷一次的概率 | 搜刮逻辑 |
| `bag_slots` / `ammo_start` / `flashlight_bonus` | 背包格 / 开局弹药 / 手电电量上限 | 对应系统 |
| `noise_decay_mult` / `noise_add_delta` / `horde_threshold_delta` | 噪音衰减倍率 / 噪音增量修正 / 尸潮阈值偏移 | `noise` 模块 |
| `flee_bonus` | 逃跑加成（/5 折算敏捷） | 逃跑结算 |
| `agility` | 敏捷 | 战斗 / 逃跑 |
| `repair_bonus` | 修理费率折扣（废料修理多修几点） | `run_service._do_repair` |
| `trinket_score_mult` | 纪念品得分乘数 | 计分 |
| `kill_heal` | 击杀回血 | 战斗结算 |
| `melee_lifesteal` | 近战吸血（击杀回血，P7 新增） | 战斗结算 |
| `ammo_scav_mult` | 弹药拾取乘数（P7 新增） | `loot.roll_loot` |
| `stamina_max` | 体力上限（P7 新增） | 状态 |
| `brace_acc_bonus_add` | 瞄准额外命中（P7 新增） | `run_service._act_brace` |
| `execute_dmg_pct` | 低血敌人额外伤害（处刑人等用） | 战斗结算 |

> 注：个别天赋用到上表未逐条列出的复合 mods（如 `execute_dmg_pct`、`kill_heal`），以 `items` 中实际 `mods` 为准；新增 key 需同步在 `core/talents.py` 聚合逻辑与各消费系统登记。
