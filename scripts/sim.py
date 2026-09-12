"""自动模拟对局——调平衡的主工具。

三件事：
  1. 冒烟：把运行时异常在写前端之前全炸出来
  2. 校准：输出各层实际通过率，对比 configs/balance.yaml 里的 targets
  3. 验证雪球：--chain 模式连打多代，确认遗物继承没有让游戏越玩越简单

数值基准：targets.pass_rate_by_level 是按「无遗物、不摸墓碑」的裸开局定的。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from collections import Counter
from pathlib import Path

# 调平衡时不该消耗 API 额度，也不该被网络延迟拖慢
os.environ.setdefault("LLM_ENABLED", "false")

forced_talent: str | None = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import loot  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402


def choose(engine: RunEngine) -> tuple[str, dict]:
    """一个还算合理的策略：优先清场、见楼梯就下、血少就治、快死就跑。"""
    st = engine.state
    acts = engine._available_actions()

    if st.get("pending_decision") == "legacy":
        return "legacy", {"index": 0}
    if st.get("pending_decision") == "zombify":
        return "end", {}
    if st.get("pending_decision") == "talent":
        # 默认选第一个；--talent <id> 时强制选指定天赋（用于逐条测强度）
        opts = st.get("talent_options") or []
        if forced_talent:
            for i, o in enumerate(opts):
                if o["id"] == forced_talent:
                    return "talent", {"index": i}
        return "talent", {"index": 0}

    if st.get("pending_decision") == "grave_pick":
        # 模拟一个会把第一件带走的正常玩家（墓碑每次只取一件）
        choices = st.get("grave_choices") or []
        if choices:
            return "grave", {"choice": "take", "uid": choices[0]["uid"]}
        return "grave", {"choice": "skip"}

    if st.get("pending_decision") == "bag_overflow":
        # 背包超载（先拿后丢 / 换装缩水）：丢到装得下为止（必然有可丢项，否则不会溢出）
        inv = st.get("inventory") or []
        if inv:
            return "discard", {"choice": "drop", "item": inv[0]["id"]}
        return "status", {}

    hp_ratio = st["hp"] / max(1, st["hp_max"])

    if st.get("in_combat"):
        # 打不过 Boss 就引开它——这是设计好的解法，不是作弊。
        # 但 lure 失败一次后（boss_lure_spent）引擎会关闭 Boss 战中的 lure，
        # 此时只能硬拼或逃跑。
        if (
            st.get("boss_alive") and st.get("boss_seen")
            and hp_ratio < 0.75 and not st.get("boss_lure_spent")
        ):
            return "lure", {}
        if hp_ratio < 0.25 and not any(
            e.get("elite") for e in st["combat"]["enemies"] if e["hp"] > 0
        ):
            return "flee", {}  # 守门精英不可逃跑，只能硬拼
        # 精英战（不可逃）：残血先用绷带顶住，别干挨打
        if hp_ratio < 0.55 and any(
            e.get("elite") for e in st["combat"]["enemies"] if e["hp"] > 0
        ):
            for e in st["inventory"]:
                if engine.cfg.item_kind(e["id"]) == "consumable" and e["id"] in (
                    "bandage", "canned", "medkit",
                ):
                    return "use", {"item": e["id"]}
        if hp_ratio < 0.45:
            for e in st["inventory"]:
                if engine.cfg.item_kind(e["id"]) == "consumable" and e["id"] in (
                    "bandage", "canned",
                ):
                    return "use", {"item": e["id"]}
        for a in acts:
            if a["id"] == "shoot":
                w = engine.cfg.item(st["weapon"]["id"])
                # burst 武器弹药不足时可部分射击（至少 1 发），单发必须够 ammo_per_shot
                if w.get("burst"):
                    if loot.count(st, w["ammo_type"]) >= 1:
                        return "shoot", {}
                elif loot.count(st, w["ammo_type"]) >= w.get("ammo_per_shot", 1):
                    return "shoot", {}
        return "attack", {}

    # 撤离层：倒计时一响就直奔撤离点，不再搜刮、不再绕路。
    # 真实玩家一定会这么做；用"沿途全搜"的策略去校准第五层会得出错误结论。
    if st.get("evac_countdown") is not None:
        for a in acts:
            if a["id"] == "evac":
                return "evac", {}
        if st.get("boss_alive"):
            for a in acts:
                if a["id"] == "lure":
                    return "lure", {}
        for a in acts:
            if a["id"] == "descend":
                return "descend", {}
        for a in acts:
            if a["id"] == "move" and a["label"] == "继续深入":
                return "move", {"to": a["to"]}
        for a in acts:
            if a["id"] == "move":
                return "move", {"to": a["to"]}

    for a in acts:
        if a["id"] == "descend":
            return "descend", {}
    if hp_ratio < 0.5:
        for e in st["inventory"]:
            if e["id"] == "bandage":
                return "use", {"item": "bandage"}
    for a in acts:
        if a["id"] == "event":
            return "event", {"choice": a["choice"]}
    # 灾害房（P6.2.2）：血量健康时赌一把翻找，残血时贴边通过——像真实玩家
    for a in acts:
        if a["id"] == "hazard":
            return "hazard", {"choice": a["choice"] if hp_ratio > 0.6 else (
                "press_on" if a["choice"] == "press_on" else a["choice"]
            )}
    # 幸存者（P6.2.2）：模拟器只交换不施舍（施舍策略另测），拿到换购就走
    for a in acts:
        if a["id"] == "npc" and a.get("choice") == "leave":
            return "npc", {"choice": "leave"}
    grave_acts = [a for a in acts if a["id"] == "grave"]
    if grave_acts:
        g = st["room"].get("grave") or {}
        if g.get("gear"):
            return "grave", {"choice": "loot"}
        # 空墓碑：没有可拿的东西。
        # 注意：引擎的「离开」只是"不碰遗体"，是个 no-op，不会移动房间，
        # 如果这里反复返回 leave 就会原地空转（实测 56% 的局卡死在这）。
        # 正确做法是直接 fall through 到下面的 move，离开这个房间。
    for a in acts:
        if a["id"] == "campfire" and hp_ratio < 0.7:
            return "campfire", {}
    for a in acts:
        if a["id"] == "search":
            return "search", {}
    for a in acts:
        if a["id"] == "move":
            return "move", {"to": a["to"]}
    return "status", {}


async def play_one(cfg, legacy=None, max_steps: int = 3000) -> dict:
    engine = await RunEngine.new_run(cfg, legacy)
    steps = 0
    while (
        engine.state["status"] == "active" or engine.state.get("pending_decision")
    ) and steps < max_steps:
        action, payload = choose(engine)
        await engine.act(action, payload)
        steps += 1

    st = engine.state
    return {
        "status": st["status"],
        "depth": st["depth"],
        "turns": st["turn"],
        "kills": st["kills"],
        "score": st["score"],
        "cause": st.get("death_cause"),
        "steps": steps,
        "epitaph": st.get("epitaph", ""),
        "legacy": st.get("chosen_legacy"),
        "legacy_taken": st.get("legacy_taken"),
        "legacy_lost": st.get("legacy_lost"),
    }


def pass_rates(results: list[dict], max_level: int) -> dict[int, float]:
    """每层通过率 = 活着进该层的玩家中，活着走出去的比例。"""
    out = {}
    for lv in range(1, max_level + 1):
        reached = [r for r in results if r["depth"] >= lv]
        if not reached:
            out[lv] = 0.0
            continue
        passed = [
            r for r in reached
            if (r["status"] == "escaped" and lv <= max_level) or r["depth"] > lv
        ]
        out[lv] = len(passed) / len(reached)
    return out


def report(results: list[dict], cfg, title: str) -> dict:
    n = len(results)
    print("=" * 66)
    print(title)
    print("=" * 66)

    status = Counter(r["status"] for r in results)
    rates = pass_rates(results, cfg.max_level)
    targets = {int(k): float(v) for k, v in cfg.balance["targets"]["pass_rate_by_level"].items()}
    tol = float(cfg.balance["targets"]["tolerance"])

    print("\n每层通过率（实际 vs 目标）：")
    print(f"  {'层':<4} {'实际':>7} {'目标':>7} {'偏差':>8}   判定")
    all_ok = True
    for lv in range(1, cfg.max_level + 1):
        a, t = rates[lv], targets.get(lv, 0.0)
        d = a - t
        ok = abs(d) <= tol
        all_ok &= ok
        flag = "OK" if ok else ("偏难 ↓" if d < 0 else "偏易 ↑")
        print(f"  L{lv:<3} {a * 100:6.1f}% {t * 100:6.1f}% {d * 100:+7.1f}%   {flag}")

    escape = status.get("escaped", 0) / n
    expect = 1.0
    for lv in range(1, cfg.max_level + 1):
        expect *= targets.get(lv, 1.0)
    print(f"\n整局撤离率 {escape * 100:.1f}%  （目标连乘值 {expect * 100:.1f}%）")
    print("整体判定：" + ("达标" if all_ok else "需要回调"))

    print("\n结局分布：")
    for k, v in status.most_common():
        print(f"  {k:<12} {v:>5}  ({v / n * 100:.1f}%)")

    causes = Counter(r["cause"] for r in results if r["cause"])
    print("\n死因 TOP 6：")
    for k, v in causes.most_common(6):
        print(f"  {k or '(未知)':<16} {v:>5}")

    def avg(key: str) -> float:
        return sum(r[key] for r in results) / max(1, len(results))

    print(
        f"\n平均：回合 {avg('turns'):.1f} · 击杀 {avg('kills'):.1f} · "
        f"得分 {avg('score'):.1f} · 操作次数 {avg('steps'):.1f}"
    )
    return {"escape": escape, "rates": rates, "all_ok": all_ok}


async def run_batch(cfg, n: int, legacy) -> dict:
    results = [await play_one(cfg, legacy) for _ in range(n)]
    esc = sum(1 for r in results if r["status"] == "escaped") / max(1, n)
    depth = sum(r["depth"] for r in results) / max(1, n)
    return {"escape": esc, "depth": depth, "results": results}


async def talent_audit(cfg, n: int) -> None:
    """逐条测量每个天赋的强度。

    主指标用**平均到达层数**，不用撤离率：
    撤离率基数只有 ~12%，n=220 时标准误就有 ±2.3%，分不清 ±2% 的差异；
    平均层数是连续量，标准误只有 ±0.08 层，信号强一个数量级。

    强度纪律（以层为单位，满值 5 层）：
        达标 +0.05 ~ +0.30     偏强 > +0.30     鸡肋 < +0.05
    """
    import app.core.talents as T

    print("\n")
    print("=" * 78)
    print(f"天赋强度验证：逐条测量（每组 {n} 局，主指标＝平均到达层数）")
    print("=" * 78)

    orig_draw = T.draw

    RunEngine.talents_enabled = False
    base = await run_batch(cfg, n, None)
    RunEngine.talents_enabled = True

    print(
        f"\n无天赋基线：平均 {base['depth']:.2f} 层 · 撤离率 {base['escape'] * 100:.1f}%\n"
    )

    rows = []
    for t in T.pool(cfg):
        tid = t["id"]
        T.draw = lambda c, r, n_=None, _tid=tid: [T.get_by_id(c, _tid)]  # noqa: E731
        res = await run_batch(cfg, n, None)
        rows.append((
            t["name"], tid, t["desc"],
            res["depth"], res["depth"] - base["depth"],
            res["escape"], res["escape"] - base["escape"],
        ))
    T.draw = orig_draw

    rows.sort(key=lambda x: -x[4])
    print(f"  {'天赋':<10}{'均层':>7}{'Δ层':>8}{'撤离率':>9}   说明")
    print("  " + "-" * 74)
    for name, _tid, desc, depth, d_delta, esc, _e_delta in rows:
        flag = ""
        if d_delta > 0.30:
            flag = "  ← 偏强"
        elif d_delta < 0.05:
            flag = "  ← 鸡肋"
        print(
            f"  {name:<10}{depth:6.2f} {d_delta:+7.2f} {esc * 100:8.1f}%   {desc}{flag}"
        )

    too_strong = [r for r in rows if r[4] > 0.30]
    useless = [r for r in rows if r[4] < 0.05]
    print(
        f"\n偏强 {len(too_strong)} 个 · 鸡肋 {len(useless)} 个"
        + ("  → 全部达标" if not too_strong and not useless else "  → 需要调整")
    )


async def main(args) -> None:
    cfg = get_config()
    results: list[dict] = []
    errors: list[str] = []

    for i in range(args.n):
        try:
            results.append(await play_one(cfg, None))
        except Exception:  # noqa: BLE001
            errors.append(f"第 {i} 局异常:\n{traceback.format_exc()}")

    if errors:
        print(f"!! 运行时异常 {len(errors)}/{args.n} 次，前 3 条：\n")
        for e in errors[:3]:
            print(e)
        return

    base = report(results, cfg, f"裸开局模拟 {args.n} 局（无遗物、不摸墓碑）")

    if args.verbose:
        print("\n样例墓志铭：")
        for r in results[:5]:
            if r["epitaph"]:
                print(f"  [L{r['depth']}/{r['cause']}] {r['epitaph']}")

    # ---- 雪球验证：连打多代，看遗物是否让游戏越玩越简单 ----
    if args.chain:
        print("\n")
        gens = []
        legacy = None
        per_gen = max(20, args.n // 2)
        for g in range(args.chain):
            gen_results = []
            # 同一代内每一局都从"上一代传下来的那件"起步，代内不串联——
            # 否则跑的就不是"世代"而是"连续死了 125 次"。
            for _ in range(per_gen):
                gen_results.append(await play_one(cfg, legacy))
            # 代末取这一代能留下的**最强**遗物传给下一代（雪球的最坏情况）
            cands = [c for r in gen_results for c in (r["legacy"],) if c]
            if cands:
                cands.sort(key=lambda c: int(cfg.item(c["id"]).get("tier", 1)))
                legacy = cands[-1]
            esc = sum(1 for r in gen_results if r["status"] == "escaped") / per_gen
            taken = gen_results[0].get("legacy_taken")
            gens.append({
                "gen": g + 1,
                "escape": esc,
                "item": (taken or {}).get("name", "—"),
                "passes": (taken or {}).get("passes", 0),
                "lost": sum(1 for r in gen_results if r.get("legacy_lost")),
            })

        print("=" * 66)
        print(f"雪球验证：连续传承 {args.chain} 代")
        print("=" * 66)
        max_gain = float(cfg.balance["targets"]["max_legacy_gain"])
        print(f"\n  {'代':<4} {'携带遗物':<14} {'传承次数':>8} {'撤离率':>8} {'vs 裸开局':>10}   报废")
        worst = 0.0
        for g in gens:
            delta = g["escape"] - base["escape"]
            worst = max(worst, delta)
            print(
                f"  第{g['gen']}代  {g['item']:<14} {g['passes']:>8} "
                f"{g['escape'] * 100:7.1f}% {delta * 100:+9.1f}%   {g['lost']}"
            )
        print(
            f"\n最大提升 {worst * 100:+.1f}%（容忍上限 {max_gain * 100:+.1f}%）→ "
            + ("雪球已封住" if worst <= max_gain else "!! 仍在滚雪球，需要收紧遗物规则")
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200, help="模拟局数")
    ap.add_argument("--chain", type=int, default=0, help="额外做几代传承的雪球验证")
    ap.add_argument("--talents", action="store_true", help="逐条测量每个天赋的强度")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.talents:
        asyncio.run(talent_audit(get_config(), max(60, args.n // 3)))
    else:
        asyncio.run(main(args))
