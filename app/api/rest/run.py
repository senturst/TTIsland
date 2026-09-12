"""run 驱动接口。

`POST /api/run/action` 是唯一的核心驱动端点——
前端只发意图（action + payload），全部规则在服务端算，
按钮可用性也由服务端下发的 available_actions 决定，前端不猜。
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...ai.service import flavor
from ...data.loader import get_config
from ...db import repo
from ...deps import client_id_from, db_call, rate_action, rate_start
from ...schemas.models import ActionIn, StartOut
from ...services.run_service import RunEnded, RunEngine
from ...api.eventbus import bus

router = APIRouter(prefix="/api/run", tags=["run"])


class WorldProvider:
    """把"跨局世界数据"注入纯逻辑的 RunEngine。

    RunEngine 本身不碰数据库（保持可被 sim.py 直接驱动做上千局模拟）。
    需要墓碑这类跨局数据时，由 API 层注入一个 provider，
    它内部走同步 repo——单条 sqlite 查询亚毫秒，不会拖垮事件循环。
    """

    def __init__(self, player_id: str) -> None:
        self.player_id = player_id

    def pick_grave(self, depth: int) -> dict | None:
        try:
            return repo.graves.pick_candidate(self.player_id, depth)
        except Exception:  # 数据库异常不应让一局游戏崩掉
            return None


def _cfg() -> Any:
    return get_config()


async def _load_engine(client_id: str, run_row: dict) -> RunEngine:
    state = await db_call(repo.runs.load_state, run_row)
    return RunEngine(_cfg(), state)


def _emit_event(
    kind: str, body: str, *, player: str | None = None,
    depth: int | None = None, score: int | None = None,
) -> None:
    """世界事件：写一条事件记录（不入聊天流）+ 推给所有在线客户端。

    只广播死亡/撤离/破纪录这类"重大事件"，不做点对点聊天。
    """
    try:
        repo.chat.add(
            player_id=player or "system", player_name=player or "系统",
            body=body, channel="system", kind=kind,
        )
    except Exception:
        pass  # 事件记录失败不能影响游戏主流程
    bus.publish({
        "kind": kind, "body": body, "player": player,
        "depth": depth, "score": score,
    })


def _build_grave_gear(cfg, st: dict) -> list[dict]:
    """死亡时把装备打成"可拾取的墓碑遗物"。

    约束与遗物继承一致：tier > max_tier 的重火力带不走（消防斧/霰弹枪），
    且尸体上的装备要经一次耐久衰减——你捡到的是别人用残的，不是新的。
    被选作遗物的那件已随主人离场，不进池子（否则同一件能拿两次）。
    """
    import uuid as _uuid

    rules = cfg.legacy_rules()
    dur_mult = float(rules.get("durability_mult", 0.6))
    taken = (st.get("chosen_legacy") or {}).get("id")
    gear: list[dict] = []

    def _add(iid: str, durability):
        if iid == taken:
            return
        if not cfg.legacy_allowed(iid):  # tier 超限 → 不可进墓碑池
            return
        item = cfg.item(iid)
        kind = cfg.item_kind(iid)
        dur = durability
        if dur is not None:
            dur = max(1, int(round(dur * dur_mult)))
        gear.append({
            "id": iid, "name": item["name"], "kind": kind,
            "tier": int(item.get("tier", 1)), "durability": dur,
            "uid": _uuid.uuid4().hex[:8],
        })

    w = st.get("weapon")
    if w:
        _add(w["id"], w.get("durability"))
    a = st.get("armor")
    if a:
        _add(a["id"], None)
    for e in st.get("inventory", []):
        if cfg.item_kind(e["id"]) in ("weapon", "armor"):
            _add(e["id"], e.get("durability"))
    return gear


async def _grant_region_clear(client_id: str, engine: RunEngine, cleared_rid: int) -> None:
    """P8：中间地区撤离成功——发继承码 + 记录地区进度 + 广播解锁下一地区。

    对局继续（engine.status 仍 active），不走 _finalize（那是终局收尾）。
    继承码以引擎日志写进主文本区（run.py 无法改前端 DOM，走 _out 最顺）。
    """
    cfg = _cfg()
    player = engine.state.get("player_name", "无名者")
    region = cfg.regions.get(int(cleared_rid)) or {}
    pre = await db_call(repo.players.get, client_id) or {}
    prev_region = int(pre.get("region_progress", 0) or 0)
    if int(cleared_rid) > prev_region:
        await db_call(repo.players.record_region_clear, client_id, int(cleared_rid))
        if int(cleared_rid) + 1 in cfg.regions:
            nxt = cfg.regions[int(cleared_rid) + 1]
            _emit_event(
                "record",
                f"{player} 突破了{region.get('name', '未知地区')}——"
                f"新的地区「{nxt['name']}」已经解锁！",
                player=player,
            )
    code = await db_call(
        repo.inherit.create_for_player, client_id, engine.state.get("run_id")
    )
    await db_call(
        repo.notifications.add, client_id,
        "system",
        f"你的继承码：{code}（在新设备输入可接回本档案，仅可使用一次）",
        {"code": code},
    )
    engine._log(f"继承码：{code}（在新设备输入可接回本档案，仅可使用一次）")


async def _finalize(client_id: str, run_id: str, engine: RunEngine) -> None:
    """run 结束后的收尾：广播事件、写墓碑、更新统计、保存遗物。

    一个函数里做完，避免"统计更新了但墓碑没写"这类半完成状态。
    """
    st = engine.state
    status = st["status"]
    if status == "active":
        return

    cfg = _cfg()
    escaped = status == "escaped"
    player = st.get("player_name", "无名者")
    depth = st.get("depth", 1)

    # 破纪录检测：先取"更新前"的纪录，record_run_end 内部用 MAX 覆盖
    pre = await db_call(repo.players.get, client_id) or {}
    prev_depth = int(pre.get("best_depth", 0) or 0)
    prev_score = int(pre.get("best_score", 0) or 0)

    await db_call(
        repo.runs.finish, run_id, status, st.get("score", 0), st.get("death_cause")
    )
    await db_call(
        repo.players.record_run_end,
        client_id,
        depth=depth,
        score=st.get("score", 0),
        kills=st.get("kills", 0),
        escaped=escaped,
        humanity_delta=int(st.get("humanity", 0)),
    )

    # 地区进度（P6.2.1）：从地区 N 的撤离点撤离成功 → 解锁地区 N+1
    if escaped:
        region = cfg.region_for_level(depth)
        rid = int(region["id"])
        prev_region = int(pre.get("region_progress", 0) or 0)
        if rid > prev_region:
            await db_call(repo.players.record_region_clear, client_id, rid)
            if rid + 1 in cfg.regions:
                nxt = cfg.regions[rid + 1]
                _emit_event(
                    "record",
                    f"{player} 突破了{region['name']}——新的地区「{nxt['name']}」已经解锁！",
                    player=player,
                )

    # 遗物继承（软 Roguelite）
    # earned_by 决定下一局拿到的是"保养过的"还是"从尸体上扒下来的"
    legacy = st.get("chosen_legacy")
    if legacy:
        legacy = dict(legacy)
        legacy["earned_by"] = "escaped" if escaped else "death"
    await db_call(repo.players.set_legacy, client_id, legacy)

    # ---- 世界事件广播（P3）----
    if escaped:
        _emit_event("escape", f"{player} 登上直升机，撤离成功。",
                    player=player, depth=depth, score=st.get("score", 0))
    else:
        cause = st.get("death_cause") or "未知"
        _emit_event("death", f"{player} 在第 {depth} 层倒下了（{cause}）。",
                    player=player, depth=depth, score=st.get("score", 0))
    if depth > prev_depth:
        _emit_event("record", f"{player} 刷新了最深的抵达记录：第 {depth} 层！",
                    player=player, depth=depth)
    elif st.get("score", 0) > prev_score:
        _emit_event("record", f"{player} 刷新了最高得分：{st.get('score', 0)}！",
                    player=player, score=st.get("score", 0))

    # 墓碑：死亡才会留下尸体，装备散给后来的人。
    # 撤离成功不产生墓碑——装备跟着你回家了，这也是"通关优于死亡"的一半理由。
    if status in ("dead", "zombified", "fled"):
        gear = _build_grave_gear(cfg, st)
        await db_call(
            repo.graves.create,
            run_id=run_id,
            player_id=client_id,
            player_name=player,
            level=depth,
            killer_id=None,
            gear=gear,
            infection=int(st.get("infection", 0)),
            epitaph=st.get("epitaph"),
            is_plagued=bool(st.get("zombified")),
        )

    # 继承码（v7）：撤离成功发一次性恢复码。玩家身份是 localStorage UUID，
    # 清缓存/换设备 = 进度丢失；码可在新身份上接回档案，只能核销一次。
    if escaped:
        code = await db_call(repo.inherit.create_for_player, client_id, run_id)
        await db_call(
            repo.notifications.add, client_id,
            "system",
            f"你的继承码：{code}（在新设备输入可接回本档案，仅可使用一次）",
            {"code": code},
        )


async def _persist_grave_effects(client_id: str, engine: RunEngine) -> None:
    """墓碑互动的持久化：摸走一件道具 / 掩埋。

    RunEngine 只负责把意图写进 state 的标记位（_grave_claim / _grave_bury），
    真正的数据库改动由这里做——保持引擎纯逻辑、可被模拟器直接驱动。
    """
    st = engine.state
    claim = st.pop("_grave_claim", None)
    if claim:
        await db_call(repo.graves.claim, claim["grave_id"], claim["uid"])
        # 私人回执：原主人下次上线能看到"谁动了我"
        try:
            await db_call(
                repo.notifications.add, claim["owner_id"], "grave_looted",
                f"{st.get('player_name', '某人')} 在废墟里发现了你的遗体，带走了 {claim['item_name']}。",
                {"grave_id": claim["grave_id"], "item": claim["item_name"]},
            )
        except Exception:
            pass
    bury = st.pop("_grave_bury", None)
    if bury:
        await db_call(repo.graves.bury, bury)


def _degraded(resp: dict) -> dict:
    """AI 不可用时给前端打标记，用于显示"信号衰减"。"""
    resp["ai_degraded"] = not flavor.stats()["ai_active"]
    return resp


def _is_settled(state: dict) -> bool:
    """这一局是否已经彻底结束、可以收尾了。

    关键：**有待决策时不能收尾**。
    死亡后会停在"留一件遗物"这一步，此时 status 已经变成 dead，
    但如果立刻把 run 标记为 finished，get_active() 就再也找不到它，
    玩家提交遗物选择时会拿到 404——遗物继承链路会整条断掉。
    """
    return state.get("status") != "active" and not state.get("pending_decision")


async def _maybe_finalize(client_id: str, run_id: str, engine: RunEngine) -> None:
    if _is_settled(engine.state):
        await _finalize(client_id, run_id, engine)


# ----------------------------------------------------------------------
@router.post("/start", response_model=StartOut)
async def start(client_id: str = Depends(client_id_from)) -> Any:
    rate_start(client_id)
    cfg = get_config()

    # 懒式挂机清理：开新局前先把全服超时 run 收掉（本玩家的旧局也会被一并处理）
    await db_call(repo.runs.expire_stale)

    existing = await db_call(repo.runs.get_active, client_id)
    if existing:
        est = await db_call(repo.runs.load_state, existing)
        if est.get("status") == "active":
            raise HTTPException(status_code=409, detail="你还有一局没结束。")
        # 玩家死在"留一件遗物"那一步就直接关了页面。
        # 这里替他收尾（不留遗物），否则这一局会永久占位，他再也开不了新局。
        stale = RunEngine(cfg, est)
        stale.state["pending_decision"] = None
        await db_call(repo.runs.save, existing["id"], stale.state)
        await _finalize(client_id, existing["id"], stale)

    player = await db_call(repo.players.get_or_create, client_id)
    # 取出并清空遗物槽（一次性）
    legacy = await db_call(repo.players.get_legacy, client_id)

    engine = await RunEngine.new_run(cfg, legacy, world=WorldProvider(client_id))
    engine.state["player_name"] = player["name"]

    run_id = await db_call(
        repo.runs.create, client_id, engine.state["seed"], engine.state
    )
    engine.state["run_id"] = run_id
    await db_call(repo.runs.save, run_id, engine.state)

    # 注意：_apply_legacy 已经写过"带上了什么"的日志，这里不要再插一条，
    # 否则开局会出现两句几乎一样的提示。
    resp = engine._response()

    # 注意：必须把 pending_decision 与 talent_options 一起返回。
    # 首局会停在"三选一天赋"这一步，前端要靠这些字段才能渲染出可点的选项。
    return {
        "run_id": run_id,
        "narrative": resp["narrative"],
        "state": resp["state"],
        "available_actions": resp["available_actions"],
        "patches": resp["patches"],
        "pending_decision": resp["pending_decision"],
        "talent_options": resp["talent_options"],
        "talent": resp["talent"],
        "icons": resp["icons"],
        "ai_degraded": not flavor.stats()["ai_active"],
    }


@router.get("/active")
async def active(client_id: str = Depends(client_id_from)) -> Any:
    """续玩：拉回当前 run 的完整画面。"""
    # 懒式挂机清理：若这个 run 已超时被收掉，get_active 就拿不到了——前端自然走"开新局"
    await db_call(repo.runs.expire_stale)
    row = await db_call(repo.runs.get_active, client_id)
    if not row:
        return {"active": False}

    engine = await _load_engine(client_id, row)
    resp = engine._response()
    return {
        "active": True,
        "run_id": row["id"],
        "state": resp["state"],
        "available_actions": resp["available_actions"],
        "epitaph": resp["epitaph"],
        "death_cause": resp["death_cause"],
        "legacy_choices": resp["legacy_choices"],
        "pending_decision": resp["pending_decision"],
        # 刷新时若正好停在天赋三选一，选项也得能恢复出来
        "talent_options": resp["talent_options"],
        "talent": resp["talent"],
        "xp": resp["xp"],
        "icons": resp["icons"],
        # 续玩时回放最近 40 行日志，让玩家接上上下文
        "history": engine.state.get("log", [])[-40:],
        "ai_degraded": not flavor.stats()["ai_active"],
    }


@router.post("/action")
async def action(
    body: ActionIn, client_id: str = Depends(client_id_from)
) -> Any:
    rate_action(client_id)
    row = await db_call(repo.runs.get_active, client_id)
    if not row:
        raise HTTPException(status_code=404, detail="没有进行中的 run。")

    engine = await _load_engine(client_id, row)
    resp = await engine.act(body.action, body.payload)

    # P8：中间地区撤离成功 → 立刻发继承码 + 记录地区进度 + 广播解锁。
    # 对局继续（engine.status 仍 active），不走 _finalize（那是终局收尾）。
    cleared = engine.state.pop("region_clear_pending", None)
    if cleared:
        await _grant_region_clear(client_id, engine, int(cleared))
        resp["narrative"] = list(engine._out)

    # 墓碑互动（摸走一件/掩埋）的 DB 落盘：在保存状态之前清掉标记位
    await _persist_grave_effects(client_id, engine)

    # 先存状态再判断收尾——顺序反了会在遗物决策那一步丢掉数据
    await db_call(repo.runs.save, row["id"], engine.state)
    await _maybe_finalize(client_id, row["id"], engine)

    return _degraded(resp)


@router.post("/flee")
async def flee(client_id: str = Depends(client_id_from)) -> Any:
    """主动放弃。记为 fled，同样产生墓碑。"""
    row = await db_call(repo.runs.get_active, client_id)
    if not row:
        raise HTTPException(status_code=404, detail="没有进行中的 run。")

    engine = await _load_engine(client_id, row)
    engine._out = []
    engine.state["death_cause"] = "放弃了"
    try:
        # _die() 用异常表示"本局结束"，状态已由它自己写好，这里只需吞掉异常。
        await engine._die("放弃了")
    except RunEnded:
        pass

    await _persist_grave_effects(client_id, engine)
    await db_call(repo.runs.save, row["id"], engine.state)
    await _maybe_finalize(client_id, row["id"], engine)
    return _degraded(engine._response())


@router.get("/{run_id}/log")
async def run_log(run_id: str, limit: int = 100) -> Any:
    row = await db_call(repo.runs.get, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run 不存在。")
    state = await db_call(repo.runs.load_state, row)
    log = state.get("log", [])
    return {
        "run_id": run_id,
        "status": row["status"],
        "score": row["score"],
        "total": len(log),
        "log": log[-max(1, min(limit, 300)):],
    }


@router.get("/graves/recent")
async def recent_graves(limit: int = 20) -> Any:
    return {"graves": await db_call(repo.graves.recent, min(limit, 50))}


@router.get("/leaderboard")
async def leaderboard(by: str = "score", limit: int = 20) -> Any:
    """三榜之一：score / depth / humanity。"""
    if by not in ("score", "depth", "humanity"):
        by = "score"
    return {"by": by, "entries": await db_call(repo.runs.leaderboard, by, min(limit, 50))}
