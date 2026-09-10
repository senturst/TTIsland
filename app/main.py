"""应用入口。

启动即做两件事：
  1. 加载并校验全部配置 —— 配置有问题就启动失败，绝不带着错误数据跑
  2. 对齐数据库版本
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api.rest import meta, player, run
from .config import ROOT, settings
from .data.loader import ConfigError, get_config
from .db import migrate

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("ttisland")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("加载配置…")
    cfg = get_config()
    log.info(
        "配置就绪：%d 物品 / %d 怪物 / %d 事件 / %d 层",
        len(cfg.items), len(cfg.monsters), len(cfg.events), cfg.max_level,
    )

    version = migrate.apply_migrations()
    migrate.init_llm_cache_db()
    log.info("数据库就绪（schema v%s）", version)

    if settings.ai_active:
        log.info("AI 风味文本已启用（%s）", settings.llm_model)
    else:
        log.info("AI 未配置，将使用内置模板文本（游戏功能不受影响）")

    yield
    log.info("服务关闭")


app = FastAPI(
    title="孤岛残响",
    description="末世僵尸题材的文字 Roguelike",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(player.router)
app.include_router(run.router)
app.include_router(meta.router)

app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 新版 Starlette 的签名是 (request, name, context)：
    # 写成 TemplateResponse("index.html", {...}) 会把模板名当成 request，
    # 导致 Jinja2 拿到一个 dict 当模板名，报 unhashable type: 'dict'。
    return templates.TemplateResponse(request=request, name="index.html")


@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError):  # noqa: ARG001
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=500, content={"detail": str(exc)})
