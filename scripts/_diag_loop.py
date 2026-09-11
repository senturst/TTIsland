"""诊断：找出模拟器里卡死的 run 到底卡在哪个循环。

跑一批局，对达到 max_steps 仍未结束的，打印最后 N 步的
(depth, room_idx, room_type, action, in_combat, hp, hp_max)。
同时统计所有 stuck run 里最常见的 (room_type, action) 组合。
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core import loot  # noqa: E402
from app.data.loader import get_config  # noqa: E402
from app.services.run_service import RunEngine  # noqa: E402

# 复用 sim.py 的策略
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sim  # noqa: E402


async def play_one_trace(cfg, max_steps: int = 3000, tail: int = 120):
    engine = await RunEngine.new_run(cfg, None)
    st = engine.state
    history = []
    steps = 0
    while (st["status"] == "active" or st.get("pending_decision")) and steps < max_steps:
        # 记录当前房间
        rm = st.get("room") or {}
        action, payload = sim.choose(engine)
        history.append((
            st["depth"], rm.get("idx"), rm.get("type"), action,
            bool(st.get("in_combat")), st["hp"], st["hp_max"],
            st.get("noisy", ""),
        ))
        await engine.act(action, payload)
        steps += 1
    return {
        "status": st["status"], "depth": st["depth"], "steps": steps,
        "tail": history[-tail:],
        "history_len": len(history),
        "hp": st["hp"],
    }


async def main():
    cfg = get_config()
    N = 60
    stuck = []
    results = []
    combo = Counter()
    for i in range(N):
        r = await play_one_trace(cfg)
        results.append(r)
        if r["status"] == "active":
            stuck.append(r)
            for (d, idx, rt, act, ic, hp, hpm, _) in r["tail"]:
                combo[(rt, act, ic)] += 1

    print(f"总 {N} 局，active(卡死) {len(stuck)} 局\n")
    if not stuck:
        print("没有卡死的局，sim 已健康。")
        return

    print("卡死 run 中最常见的 (room_type, action, in_combat) 组合：")
    for (rt, act, ic), c in combo.most_common(12):
        print(f"  {rt:<10} {act:<10} in_combat={ic!s:<5}  {c} 次")

    print("\n前 3 个卡死 run 的尾部轨迹：")
    for k, r in enumerate(stuck[:3]):
        print(f"\n--- stuck #{k+1}  深度 {r['depth']}  步数 {r['steps']}  HP {r['hp']} ---")
        for (d, idx, rt, act, ic, hp, hpm, _) in r["tail"][-60:]:
            print(f"  D{d} idx={idx:<3} {rt:<10} act={act:<10} ic={ic!s:<5} hp={hp}/{hpm}")


if __name__ == "__main__":
    asyncio.run(main())
