"""管理后台接口。

全部端点都要求已登录（依赖 require_admin 校验会话 cookie）。
功能：
  - 登录 / 登出
  - 配置：列出并读写 configs/ 下所有 YAML（写后触发热重载；非法配置自动回滚到上一版）
  - 玩家：列出所有"存活在玩"（runs.status='active'）的玩家，查看并修改其完整状态与道具
  - 统计：死亡分布（地区×层）与各层通过率（排除主动放弃，P8）
"""
from __future__ import annotations

import yaml
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel

from ...admin_auth import login, logout, valid_session
from ...data.loader import CONFIG_DIR, get_config, reload_config, reload_status
from ...db import repo
from ...deps import db_call

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------
def require_admin(tt_admin: str | None = Cookie(default=None)) -> None:
    if not valid_session(tt_admin or ""):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")


# ---------------------------------------------------------------------------
# 登录 / 登出
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    token: str


@router.post("/login")
async def admin_login(body: LoginIn, response: Response) -> dict:
    sid = login(body.token)
    if not sid:
        raise HTTPException(status_code=401, detail="令牌错误，或尚未配置管理令牌（禁止默认弱口令）")
    response.set_cookie(
        "tt_admin", sid,
        httponly=True, samesite="lax", max_age=8 * 3600,
    )
    return {"ok": True}


@router.post("/logout")
async def admin_logout(response: Response, tt_admin: str | None = Cookie(default=None)) -> dict:
    if tt_admin:
        logout(tt_admin)
    response.delete_cookie("tt_admin")
    return {"ok": True}


@router.get("/whoami")
async def whoami(_: None = Depends(require_admin)) -> dict:
    return {"ok": True, "authenticated": True}


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------
@router.get("/configs")
async def list_configs(_: None = Depends(require_admin)) -> dict:
    files = sorted(
        p.name for p in CONFIG_DIR.glob("*.yaml")
        if p.is_file() and p.name != "admin.yaml"  # 令牌文件不在可编辑列表里
    )
    return {"files": files}


@router.get("/configs/{file}")
async def get_config_file(file: str, _: None = Depends(require_admin)) -> dict:
    _check_name(file)
    path = CONFIG_DIR / file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="配置文件不存在")
    return {"file": file, "content": path.read_text(encoding="utf-8")}


class ConfigIn(BaseModel):
    content: str


@router.put("/configs/{file}")
async def put_config_file(file: str, body: ConfigIn, _: None = Depends(require_admin)) -> dict:
    _check_name(file)
    path = CONFIG_DIR / file
    # 1. 先校验 YAML 能解析
    try:
        yaml.safe_load(body.content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败：{exc}")

    # 2. 备份旧内容，写新内容，触发热重载；若校验不过则回滚磁盘，避免留下坏配置
    old = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        path.write_text(body.content, encoding="utf-8")
        reload_config()
        st = reload_status()
        if st.get("error"):
            if old is not None:
                path.write_text(old, encoding="utf-8")
                reload_config()
            return {"ok": False, "applied": False, "error": st["error"]}
        return {"ok": True, "applied": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        if old is not None:
            path.write_text(old, encoding="utf-8")
        raise HTTPException(status_code=500, detail=f"保存失败：{exc}") from exc


def _check_name(file: str) -> None:
    if ".." in file or not file.endswith(".yaml") or "/" in file:
        raise HTTPException(status_code=400, detail="非法文件名")


# ---------------------------------------------------------------------------
# 玩家管理
# ---------------------------------------------------------------------------
@router.get("/players")
async def list_players(_: None = Depends(require_admin)) -> dict:
    """列出所有存活在玩的玩家（runs.status='active'）。"""
    rows = await db_call(repo.runs.list_active)
    out = []
    for r in rows:
        st = repo.runs.load_state(r)
        out.append({
            "run_id": r["id"],
            "player_id": r["player_id"],
            "name": st.get("player_name"),
            "depth": st.get("depth"),
            "hp": st.get("hp"),
            "hp_max": st.get("hp_max"),
            "stamina": st.get("stamina"),
            "infection": st.get("infection"),
            "cash": st.get("cash", 0) + sum(
                e["qty"] for e in st.get("inventory", []) if e.get("id") == "cash"
            ),
            "scrap": sum(
                e["qty"] for e in st.get("inventory", []) if e.get("id") == "scrap"
            ),
            "in_combat": bool(st.get("in_combat")),
            "score": st.get("score"),
            "started_at": r["started_at"],
        })
    return {"players": out}


@router.get("/items")
async def list_items(_: None = Depends(require_admin)) -> dict:
    """全物品目录（玩家编辑器的背包下拉数据源），按段分组。"""
    cfg = get_config()
    groups = []
    for sec in ("weapons", "armor", "backpacks", "consumables", "ammo", "materials", "trinkets"):
        names = {
            "weapons": "武器", "armor": "护甲", "backpacks": "背包",
            "consumables": "消耗品", "ammo": "弹药", "materials": "材料", "trinkets": "纪念品",
        }
        items = [
            {"id": it["id"], "name": it["name"],
             "durability": int(it.get("durability", 0) or 0)}
            for it in cfg.items_cfg.get(sec) or []
        ]
        if items:
            groups.append({"section": sec, "label": names[sec], "items": items})
    return {"groups": groups}


@router.get("/players/{run_id}")
async def get_player(run_id: str, _: None = Depends(require_admin)) -> dict:
    row = await db_call(repo.runs.get, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run 不存在")
    state = await db_call(repo.runs.load_state, row)
    # 背包容量（空位展示用）：基础 + 背包 slots + 护甲 pockets（与 _bag_cap 同口径）
    cfg = get_config()
    cap = int(cfg.balance["player"].get("bag_slots", 8))
    bp = state.get("backpack") or {}
    if bp.get("id"):
        cap += int(cfg.item(bp["id"]).get("slots", 0) or 0)
    ar = state.get("armor") or {}
    if ar.get("id"):
        cap += int(cfg.item(ar["id"]).get("pockets", 0) or 0)
    return {
        "run_id": run_id,
        "status": row["status"],
        "player_id": row["player_id"],
        "state": state,
        "bag_cap": max(1, cap),
    }


class PlayerStateIn(BaseModel):
    state: dict


@router.put("/players/{run_id}")
async def update_player(run_id: str, body: PlayerStateIn, _: None = Depends(require_admin)) -> dict:
    """修改玩家完整状态（含背包/装备/货币/战斗）。

    写入即生效：玩家下一次行动时会从 DB 重新加载这份状态。
    身份字段（run_id/seed/status）强制沿用原值，防止误改导致 run 错乱或绕过收尾。
    """
    if not isinstance(body.state, dict):
        raise HTTPException(status_code=400, detail="state 必须是对象")

    row = await db_call(repo.runs.get, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run 不存在")
    old = await db_call(repo.runs.load_state, row)

    new_state = dict(body.state)
    # 保护字段：不让后台把身份/局状态改坏
    new_state["run_id"] = old.get("run_id", run_id)
    new_state["seed"] = old.get("seed")
    new_state["status"] = old.get("status", "active")

    await db_call(repo.runs.save, run_id, new_state)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 数据统计（P8）：死亡分布 + 各层通过率
# ---------------------------------------------------------------------------
@router.get("/stats")
async def admin_stats(_: None = Depends(require_admin)) -> dict:
    """死亡分布（地区×层）与各层通过率。

    口径（用户拍板）：
      - 只统计**已完结**的局：进行中（active）不算，主动放弃（fled）不算
      - 通过率与 scripts/sim.py 同口径：到达该层的局中，层数超过它
        （或从该层撤离成功）的比例
    """
    from ...data.loader import get_config

    cfg = get_config()
    max_level = cfg.max_level
    rows = await db_call(repo.runs.run_summaries)

    fled = sum(1 for r in rows if r["status"] == "fled")
    finished = [r for r in rows if r["status"] != "fled" and r["status"] != "active"]

    reached = [0] * (max_level + 1)
    passed = [0] * (max_level + 1)
    deaths: dict[tuple[int, int], int] = {}
    for r in finished:
        d = min(max(1, int(r["depth"] or 1)), max_level)
        for lv in range(1, d + 1):
            reached[lv] += 1
        if r["status"] == "escaped":
            passed[max_level] += 1
        for lv in range(1, d):
            passed[lv] += 1
        if r["status"] in ("dead", "zombified"):
            rid = cfg.region_id_for_level(d)
            key = (rid, d)
            deaths[key] = deaths.get(key, 0) + 1

    deaths_list = [
        {
            "region": cfg.regions[rid]["name"],
            "depth": d,
            "count": n,
        }
        for (rid, d), n in sorted(deaths.items(), key=lambda kv: (-kv[1], kv[0][1]))
    ]
    pass_rates = [
        {
            "level": lv,
            "region": cfg.region_for_level(lv)["name"],
            "reached": reached[lv],
            "passed": passed[lv],
            "rate": round(passed[lv] / reached[lv], 4) if reached[lv] else None,
        }
        for lv in range(1, max_level + 1)
    ]
    return {
        "total_finished": len(finished),
        "abandoned_excluded": fled,
        "active_now": sum(1 for r in rows if r["status"] == "active"),
        "deaths": deaths_list,
        "pass_rates": pass_rates,
    }
