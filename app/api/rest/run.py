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

router = APIRouter(prefix="/api/run", tags=["run"])


def _cfg() -> Any:
    return get_config()


async def _load_engine(client_id: str, run_row: dict) -> RunEngine:
    state = await db_call(repo.runs.load_state, run_row)
    return RunEngine(_cfg(), state)


async def _finalize(client_id: str, run_id: str, engine: RunEngine) -> None:
    """run 结束后的收尾：写墓碑、更新统计、保存遗物。

    一个函数里做完，避免"统计更新了但墓碑没写"这类半完成状态。
    """
    st = engine.state
    status = st["status"]
    if status == "active":
        return

    cfg = _cfg()
    escaped = status == "escaped"
    await db_call(
        repo.runs.finish, run_id, status, st.get("score", 0), st.get("death_cause")
    )
    await db_call(
        repo.players.record_run_end,
        client_id,
        depth=st.get("depth", 1),
        score=st.get("score", 0),
        kills=st.get("kills", 0),
        escaped=escaped,
        humanity_delta=int(st.get("humanity", 0)),
    )

    # 遗物继承（软 Roguelite）
    # earned_by 决定下一局拿到的是"保养过的"还是"从尸体上扒下来的"
    legacy = st.get("chosen_legacy")
    if legacy:
        legacy = dict(legacy)
        legacy["earned_by"] = "escaped" if escaped else "death"
    await db_call(repo.players.set_legacy, client_id, legacy)

    # 墓碑：死亡才会留下尸体，装备散给后来的人。
    # 撤离成功不产生墓碑——装备跟着你回家了，这也是"通关优于死亡"的一半理由。
    if status in ("dead", "zombified", "fled"):
        gear = []
        # 被选作遗物的那件已被带走，不进池子（否则同一件装备能拿两次）
        taken = (st.get("chosen_legacy") or {}).get("id")
        if st.get("weapon") and st["weapon"]["id"] != taken:
            gear.append(st["weapon"])
        if st.get("armor") and st["armor"]["id"] != taken:
            gear.append(st["armor"])
        for e in st.get("inventory", []):
            if e["id"] != taken and cfg.item_kind(e["id"]) in ("weapon", "armor"):
                gear.append({"id": e["id"], "durability": e.get("durability")})
        await db_call(
            repo.graves.create,
            run_id=run_id,
            player_id=client_id,
            player_name=st.get("player_name", "无名者"),
            level=st.get("depth", 1),
            killer_id=None,
            gear=gear,
            infection=int(st.get("infection", 0)),
            epitaph=st.get("epitaph"),
            is_plagued=bool(st.get("zombified")),
        )


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

    engine = await RunEngine.new_run(cfg, legacy)
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
async def leaderboard(limit: int = 20) -> Any:
    return {"entries": await db_call(repo.runs.leaderboard, min(limit, 50))}
