# 孤岛残响

末世僵尸题材的文字 Roguelike 网页游戏。

玩家扮演幸存者，逐层下潜探索随机生成的地图，与感染者回合制战斗、搜刮物资、
管理感染与噪音，最终在第五层登上直升机——或者倒在半路上。

**这不是"通关一次就毕业"的游戏。** 这五层是**地区 1**，整局撤离率约 10%，
设计目标是反复游玩：刷装备、攒实力，然后去挑战后续地区。

---

## 快速开始

```bash
# 1. 安装依赖（首次）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux

# 2. 配置（可选）
cp .env.example .env      # 填 LLM_API_KEY 可启用 AI 风味文本；不填也能玩

# 3. 启动
python scripts/restart.py          # 后台启动，日志写 logs/server.log
python scripts/restart.py --fg     # 前台启动，直接看日志
python scripts/restart.py --stop   # 停止
```

打开 http://127.0.0.1:8000 即可游玩。

> **为什么用 `scripts/restart.py` 而不是直接跑 uvicorn**
> 直接跑 uvicorn 时，上一轮进程经常没被真正杀掉、端口仍被占用，
> 新进程启动失败而你以为"改动没生效"——实际跑的还是旧代码。
> 这个脚本会按端口精确清理、校验配置、再启动，省掉这类十分钟起步的排查。

---

## 推送到 GitHub

```bash
python scripts/push_github.py --check   # 先验权限，比推失败再排查快
python scripts/push_github.py          # 推送当前分支
```

token 放在 `.env` 的 `GITHUB_TOKEN=`（该文件已被 gitignore），
脚本只在本次推送时拼接 URL，不会把凭据写进 `.git/config`，输出里也会屏蔽它。

> **403 Permission denied 的坑**：fine-grained token 创建时默认是
> `Public repositories (read-only)`——即使仓库是你自己的，push 也会被拒。
> 需要改成 `Only select repositories → 勾选本仓库` +
> `Contents: Read and write`。或直接用 classic token 勾 `repo` 范围。

---

## 玩法要点

| 系统 | 说明 |
|---|---|
| **感染度 0-100** | 75+ 给的是**增益**而非惩罚（伤害 +20%），高感染是一种可玩的策略位置 |
| **噪音 0-10** | 枪强但吵，近战弱但安静。越过阈值触发尸潮，本层僵尸密度翻倍 |
| **五层主题** | 日光 → 手电电量 → 高危污染区 → 警报系统 → 撤离倒计时。数值只涨 8%/层，难度靠机制 |
| **天赋三选一** | **复活进场**时抽三个选一个（上一局撤离成功则不触发）。定位是死亡补偿，只在本局生效 |
| **遗物继承** | 死亡或撤离时留一件给下一个角色。撤离带回的是**满耐久、不涨传承次数**的；死亡的是**衰减过的** |
| **重火力带不走** | tier 3（消防斧 / 霰弹枪）不可继承，且传承满 3 代报废——这是防滚雪球的核心 |

### 为什么撤离比死亡划算

如果两者代价一样，最优解会变成"反正装备都要丢，不如早点死"，
难度曲线会被这个策略架空。所以：

- **撤离** = 装备保养好带回家（耐久全满、传承次数不变）+ 撤离津贴，且**不产生墓碑**
- **死亡** = 从尸体上扒回来的（耐久衰减、传承次数 +1），其余装备散落进墓碑池**给别人摸**

---

## 操作

界面底部是命令区，点按钮即可；也支持命令行输入：

| 输入 | 作用 |
|---|---|
| `攻击` / `a` | 近战攻击 |
| `射击` / `s` | 枪械射击 |
| `逃跑` / `f` | 尝试脱离战斗 |
| `搜刮` / `search` | 搜索当前房间 |
| `下一层` / `d` | 走楼梯下行 |
| `状态` | 查看当前数值 |
| `1` `2` `3`… | 执行第 N 个按钮 |
| `用 绷带` | 使用指定物品 |

---

## 项目结构

```
app/
├── core/            纯逻辑，不碰数据库（可被 scripts/sim.py 直接调用做上千局模拟）
│   ├── combat.py      命中/伤害/暴击结算
│   ├── infection.py   感染度分档与修正
│   ├── noise.py       噪音与尸潮
│   ├── mapgen.py      房间有向图生成
│   ├── level_rules.py 五层主题钩子
│   └── talents.py     天赋
├── services/run_service.py   run 状态机（全部游戏规则）
├── ai/                AI 风味文本层（缓存 / 限流 / 降级）
├── data/loader.py     配置加载与校验（支持热重载）
├── db/                SQLite 访问
├── api/               REST 路由
└── static/            原生 JS 前端（无构建步骤）

configs/               全部数值与内容，改这里不用动代码
├── balance.yaml         数值总闸 + 平衡目标
├── monsters.yaml  items.yaml  rooms.yaml  events.yaml
├── level_themes.yaml    五层主题
├── talents.yaml         天赋池
└── prompts/*.txt        AI 提示词模板

scripts/
├── restart.py       启动/停止服务（推荐用这个）
├── sim.py           自动模拟对局：冒烟 + 调平衡 + 验证天赋/雪球
└── dev.bat          Windows 一键启动

tests/               回归测试
```

---

## 调平衡

数值全在 `configs/`，改完**不用重启**（配置热重载）。

```bash
python scripts/sim.py -n 300              # 各层通过率 vs 目标
python scripts/sim.py --talents -n 660    # 逐条测量每个天赋的强度
python scripts/sim.py --chain 4           # 验证遗物继承没有滚雪球
python tests/test_cache_deadlock.py       # 回归测试
```

目标值写在 `configs/balance.yaml` 的 `targets` 段：

| 层 | 目标通过率 |
|---|---|
| 1 | 88% |
| 2 | 76% |
| 3 | 65% |
| 4 | 55% |
| 5 | 45% |

连乘 ≈ 10.8%，即**平均十局撤离一次**。

### 热重载的边界

- **纯数值**（`balance.yaml`）—— 随时改，即时生效
- **提示词模板** —— 版本号写在缓存键里，改了自动失效
- **结构配置**（增删物品/怪物/天赋 ID）—— 需要重启。删除 ID 会让正在进行的 run 崩溃，
  系统会**拒绝**这类热重载并提示原因
- **Python 代码** —— 用 `scripts/restart.py --reload`

---

## AI 风味文本（可选）

不配 Key 也能完整游玩，只是文字用内置模板。配置后 AI 会生成：
房间氛围、遭遇开场白、物品描述、墓志铭、死亡旁白。

设计上保证 AI **永不阻塞操作**：

- 装饰性文本先返回模板立即渲染，AI 生成后随下一次响应作为 patch 原地替换
- 缓存键用粗粒度变量（房间氛围只用"房间模板 + 层数"），稳态命中率 >95%
- 四级降级：缓存命中 → 模板兜底 → 熔断 → 日预算耗尽
- 输出强校验：超长 / 含换行 / 含 Markdown / 非中文一律判非法并降级

成本上限在 `.env` 的 `LLM_DAILY_BUDGET_CENTS`。

---

## 后续阶段

首批已完成 P0（骨架）+ P1（单机闭环）+ P1.5（AI 风味层）。以下设计已就绪、尚未实现：

- **P3 世界频道**：SSE 实时聊天 + 死亡/撤离播报
- **P4 墓碑交织**：死亡玩家的遗体会出现在**别人**的地图里，可搜刮/掩埋，
  原主人会收到"你的遗体被谁发现了"回执
- **P5 排行榜**：分数 / 深度 / 人道三榜
- **P7 部署**：systemd + Nginx 脚本（需要一台 Linux 服务器）
