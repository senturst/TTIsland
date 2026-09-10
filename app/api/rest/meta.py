"""元信息接口：配置版本号、前端需要的常量、健康检查。"""
from __future__ import annotations

from fastapi import APIRouter

from ...ai import cache, limiter
from ...ai.service import flavor
from ...config import settings
from ...data.loader import get_config, reload_config, reload_status
from ...db import migrate

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/meta/config")
async def meta_config() -> dict:
    cfg = get_config()
    return {
        "config_version": cfg.balance.get("config_version", 1),
        "max_level": cfg.max_level,
        "levels": {
            lv: {
                "name": t["name"],
                "subtitle": t["subtitle"],
                "mechanism": t["mechanism"],
                "mechanism_desc": t["mechanism_desc"],
                "brief": t.get("brief", ""),
                "icon": t.get("icon", ""),
            }
            for lv, t in cfg.levels.items()
        },
        "items": {
            iid: {"name": it["name"], "icon": it.get("icon", "")}
            for iid, it in cfg.items.items()
        },
        "monsters": {
            mid: {"name": m["name"], "icon": m.get("icon", "")}
            for mid, m in cfg.monsters.items()
        },
        "ai_enabled": settings.ai_active,
    }


@router.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "db": migrate.db_info(),
        "ai": flavor.stats(),
        "llm_cache": cache.stats(),
        "limiter": limiter.status(),
        "config": reload_status(),
    }


@router.post("/admin/reload")
async def reload() -> dict:
    """强制重载配置。

    改数值不想重启服务时用。若新配置不合法或含破坏性变更，
    会保留旧配置继续跑，并在 error 字段说明原因。
    """
    cfg = reload_config()
    return {
        "ok": True,
        "config_version": cfg.balance.get("config_version", 1),
        "items": len(cfg.items),
        "monsters": len(cfg.monsters),
        "talents": len(cfg.talents_cfg.get("talents") or []),
        **reload_status(),
    }
