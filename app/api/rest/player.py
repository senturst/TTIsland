"""玩家身份接口。

MVP 无注册无密码：client_id 即身份（前端 localStorage 里的 UUID）。
这是明确取舍——防误操作，不防恶意伪造。真要公开上线时再加鉴权。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...db import repo
from ...deps import client_id_from, db_call
from ...schemas.models import HelloIn, HelloOut

router = APIRouter(prefix="/api/player", tags=["player"])


@router.post("/hello", response_model=HelloOut)
async def hello(body: HelloIn, client_id: str = Depends(client_id_from)) -> HelloOut:
    """认领身份。已有则续用，没有则分配随机幸存者名。"""
    player = await db_call(repo.players.get_or_create, client_id)

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
        },
        active_run=active_run,
        has_legacy=bool(legacy_row),
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
        "legacy": player["legacy_item"],
    }


@router.post("/rename")
async def rename(name: str, client_id: str = Depends(client_id_from)) -> dict:
    player = await db_call(repo.players.rename, client_id, name)
    if not player:
        return {"ok": False, "reason": "玩家不存在"}
    return {"ok": True, "name": player["name"]}
