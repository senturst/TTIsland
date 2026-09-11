"""管理后台鉴权。

设计取舍（与游戏本身"无密码"的 MVP 定位区分开）：
  - 管理后台是**高权限**操作（改配置、改任何玩家的状态/道具），必须有口令。
  - 口令来源优先级：环境变量 ADMIN_TOKEN（写在 .env，已被 gitignore）>
    configs/admin.yaml 的 token 字段。
  - 登录成功签发一个随机会话 id，存进内存字典（单进程够用；进程重启会话清空，
    管理员重新登录即可）。会话 cookie 设为 httpOnly，JS 读不到，防 XSS 窃取。
  - 未配置口令、或仍在使用默认弱口令，一律拒绝登录并打印启动告警。
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import yaml

from .config import ROOT

CONFIG_PATH = ROOT / "configs" / "admin.yaml"
PLACEHOLDER = "change-me-please-set-a-strong-token"
SESSION_TTL = 8 * 3600  # 8 小时

# sid -> 过期时间戳
_SESSIONS: dict[str, float] = {}


def load_admin_token() -> str:
    """读取配置好的管理令牌；未配置返回空串。"""
    env = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if env:
        return env
    if CONFIG_PATH.exists():
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return ""
        tok = (data.get("token") or "").strip()
        if tok:
            return tok
    return ""


def token_configured() -> bool:
    tok = load_admin_token()
    return bool(tok) and tok != PLACEHOLDER


def login(token: str) -> str | None:
    """校验口令，成功返回会话 id，失败返回 None。"""
    configured = load_admin_token()
    if not configured or configured == PLACEHOLDER:
        return None
    if not token or token != configured:
        return None
    sid = secrets.token_urlsafe(32)
    _SESSIONS[sid] = time.time() + SESSION_TTL
    return sid


def logout(sid: str) -> None:
    _SESSIONS.pop(sid, None)


def valid_session(sid: str) -> bool:
    if not sid or sid not in _SESSIONS:
        return False
    if _SESSIONS[sid] < time.time():
        _SESSIONS.pop(sid, None)
        return False
    # 活跃会话顺延，免得管理员操作到一半被踢
    _SESSIONS[sid] = time.time() + SESSION_TTL
    return True


__all__ = ["load_admin_token", "token_configured", "login", "logout", "valid_session", "PLACEHOLDER"]
