# monsters.yaml 怪物参数说明

> 全部参数按引擎实际消费情况标注（core/combat.py / run_service.py）。
> 含两个「文档声称有、实际未实现」的字段，已如实标注。

## 基础战斗数值

| 参数 | 含义 | 引擎行为 |
|---|---|---|
| `hp` | 生命值 | 进层缩放：`hp × (1 + 0.08 × (层−1))`——**唯一随深度成长的属性**（只加血不加伤害，难度靠新怪种+数量） |
| `dmg` | 伤害区间 `[min, max]` | 每次命中随机取值，再 −玩家护甲固定值（最低 1）、−吸伤百分比；burst 怪按每发独立算 |
| `acc` | 命中率（%） | 与玩家闪避（5+装备修正）相减后结算命中；普通怪 50–68、Boss 72–75 |
| `eva` | 闪避（%） | 玩家攻击时从命中率里扣——军犬 20 很难打中，肿尸 0 必中 |
| `crit` | 暴击率 | 命中后按 `crit_mult 1.8` 加伤 |
| `armor` | 固定减伤 | 玩家每次攻击先减这个值（最低 1）——甲 3 的防暴兵/葬列需要高伤武器或扫射磨 |
| `speed` | 速度 | **只用于逃跑判定**：敌人最快速度 vs 玩家敏捷，越高越难逃 |

## 行为与特效

| 参数 | 含义 |
|---|---|
| `archetype` | 行为原型（shambler 迟钝直冲 / rusher 突进 / grabber 抓握 / spitter 喷吐 / brute 重击 / boss 技能循环）。**注意：目前只透传存档，引擎还没有按原型分支的 AI 逻辑**——预留字段 |
| `bite_chance` | 命中后额外触发「咬」的概率（普通怪 0.15–0.28，食尸鬼 0.40，军犬 0.45） |
| `bite_infection` | 咬中的感染增量区间（一次 volley 合并结算一次） |
| `burst` | 扫射：`[min, max]` 发/回合，每发独立命中/伤害/护甲吸收（地区 2 远程怪专属：士兵 [2,3]、葬列 [4,6]）；单发伤害同步下调——防弹衣的百分比吸收逐发生效 |
| `on_death.explode` | 死亡爆炸：`dmg`（伤害）+ `infection`（感染）+ `noise`（噪音）——肿尸/燃烧兵 |
| `on_hit.noise_add` | 被打时制造噪音——尖啸者 +3（全层都听得见） |
| `ambush` | 伏击标记。**目前未实现**（只透传，无先手逻辑）——潜伏者的「等你走近才动手」是氛围描述 |
| `elite` | 楼梯口守门精英：不可逃跑、必须消灭才能下楼；按地区取（地区 1 守门者 / 地区 2 变异军士，见 balance.yaml noise.horde.elite.monsters） |
| `boss` | 地区 Boss：不可逃跑、击杀后撤离点解锁；死亡触发 `boss_kill_bonus` 加分；按地区配置（regions.yaml 的 boss 字段） |
| `abilities` | Boss 技能列表：`{id, name, cooldown, dmg, noise}`——冷却好了自动释放（无视命中直接结算，吃护甲）；葬列双技能：装甲冲撞 + 尸潮践踏 |
| `xp` / `score` | 击杀经验（攒条升级）与得分 |

## 身份与生成

| 参数 | 含义 |
|---|---|
| `id` / `name` / `icon` / `desc` | 引用锚点与展示（id 全游戏引用锚点，不可改） |
| `ai_fields: [flavor_short]` | 声明交给 AI 生成的字段（`LLM_ENABLED=false` 时用占位文本） |

## 表级配置

| 段 | 含义 |
|---|---|
| `templates.base` | 所有怪的公共默认值（`<<: *base` YAML 锚点继承，覆盖即改） |
| `encounter_tables.per_level` | 每层遭遇：`count` 数量区间 + `weights` 怪物权重——**地区过滤靠这里**，loader 强制 1–max_level 每层必须有表 |
| `horde` | 尸潮追加怪：`monster` 种类 + `count` 数量（打上 `horde` 标记，清场削噪判定用） |

## 调难度建议

顺序：先动 `encounter_tables` 的 count/weights 和 level_themes 6-10 层的
`zombie_density`（影响「遇到多少」），再动单怪数值（影响「单场多疼」）。
改完跑 `python scripts/sim.py -n 300` 看通过率（L5+ 的 sim Boss 战力
不代表人类玩家，见 2026-09-12 记忆）。
