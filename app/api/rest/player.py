"""玩家身份接口。

MVP 无注册无密码：client_id 即身份（前端 localStorage 里的 UUID）。
这是明确取舍——防误操作，不防恶意伪造。真要公开上线时再加鉴权。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...ai.name_filter import check_name
from ...db import repo
from ...deps import client_id_from, db_call
from ...schemas.models import HelloIn, HelloOut, NameIn

router = APIRouter(prefix="/api/player", tags=["player"])


@router.post("/hello", response_model=HelloOut)
async def hello(body: HelloIn, client_id: str = Depends(client_id_from)) -> HelloOut:
    """认领身份。已有则续用，没有则分配随机幸存者名。"""
    player = await db_call(repo.players.get_or_create, client_id)

    # 懒式挂机清理：6 小时无活动的 run 在这里被收掉（全服一次性扫描）
    await db_call(repo.runs.expire_stale)

    active = await db_call(repo.runs.get_active, client_id)
    active_run = None
    if active:
        active_run = {
            "run_id": active["id"],
            "depth": active["depth"],
            "score": active["score"],
            "started_at": active["started_at"],
        }

    legacy_row = player.get("legacy_item")
    return HelloOut(
        client_id=client_id,
        player={
            "id": player["id"],
            "name": player["name"],
            "total_runs": player["total_runs"],
            "best_depth": player["best_depth"],
            "best_score": player["best_score"],
            "total_kills": player["total_kills"],
            "escapes": player["escapes"],
            "humanity": player["humanity"],
            "region_progress": player.get("region_progress", 0),
        },
        active_run=active_run,
        has_legacy=bool(legacy_row),
        # 默认随机名（named=0）需要在开局让玩家自定名字
        needs_name=not bool(player.get("named")),
    )


@router.get("/me")
async def me(client_id: str = Depends(client_id_from)) -> dict:
    player = await db_call(repo.players.get, client_id)
    if not player:
        return {"exists": False}
    return {
        "exists": True,
        "name": player["name"],
        "total_runs": player["total_runs"],
        "best_depth": player["best_depth"],
        "best_score": player["best_score"],
        "total_kills": player["total_kills"],
        "escapes": player["escapes"],
        "humanity": player["humanity"],
        "region_progress": player.get("region_progress", 0),
        "legacy": player["legacy_item"],
    }


@router.post("/rename")
async def rename(body: NameIn, client_id: str = Depends(client_id_from)) -> dict:
    """改名同样要走审核——否则玩家可用 /rename 绕过 /set_name 的过滤设任意名字。"""
    ok, reason = await check_name(body.name)
    if not ok:
        return {"ok": False, "reason": reason}
    player = await db_call(repo.players.rename, client_id, body.name)
    if not player:
        return {"ok": False, "reason": "玩家不存在"}
    return {"ok": True, "name": player["name"]}


@router.post("/set_name")
async def set_name(body: NameIn, client_id: str = Depends(client_id_from)) -> dict:
    """开局自定名字：先经 DeepSeek 审核过滤违法/不雅，再落库。

    审核不通过返回 {ok:false, reason}；通过则改名并标记 named=1。
    """
    ok, reason = await check_name(body.name)
    if not ok:
        return {"ok": False, "reason": reason}
    player = await db_call(repo.players.rename, client_id, body.name)
    if not player:
        return {"ok": False, "reason": "玩家不存在"}
    return {"ok": True, "name": player["name"]}
