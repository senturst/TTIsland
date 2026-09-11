"""私人回执接口。

墓碑被别人摸走时，原主人离线也能收到「谁动了我」的私信。
不走 SSE（SSE 是公开的世界播报），这张表只在玩家自己上线时拉取。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...db import repo
from ...deps import client_id_from, db_call

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
async def list_unread(client_id: str = Depends(client_id_from)) -> dict[str, Any]:
    items = await db_call(repo.notifications.unread, client_id, 20)
    return {"items": items}


@router.post("/read")
async def mark_read(ids: list[int], client_id: str = Depends(client_id_from)) -> dict[str, Any]:
    n = await db_call(repo.notifications.mark_read, ids)
    return {"ok": True, "marked": n}
