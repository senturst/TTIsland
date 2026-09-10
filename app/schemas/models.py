"""请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HelloIn(BaseModel):
    name: str | None = None


class HelloOut(BaseModel):
    client_id: str
    player: dict[str, Any]
    active_run: dict[str, Any] | None = None
    has_legacy: bool = False


class ActionIn(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionOut(BaseModel):
    narrative: list[str] = Field(default_factory=list)
    patches: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any]
    available_actions: list[dict[str, Any]] = Field(default_factory=list)
    icons: dict[str, str] = Field(default_factory=dict)
    epitaph: str | None = None
    death_cause: str | None = None
    legacy_choices: list[dict[str, Any]] = Field(default_factory=list)
    legacy_blocked: list[str] = Field(default_factory=list)
    pending_decision: str | None = None
    talent_options: list[dict[str, Any]] = Field(default_factory=list)
    talent: dict[str, Any] | None = None
    ai_degraded: bool = False


class StartOut(BaseModel):
    """注意：FastAPI 会用 response_model **过滤**响应，模型里没声明的字段会被静默丢掉。
    天赋三选一就靠 talent_options / pending_decision，漏了它们前端就渲染不出选项。
    加字段时务必同步这里。"""

    run_id: str
    narrative: list[str]
    state: dict[str, Any]
    available_actions: list[dict[str, Any]]
    patches: list[dict[str, Any]] = Field(default_factory=list)
    icons: dict[str, str] = Field(default_factory=dict)
    pending_decision: str | None = None
    talent_options: list[dict[str, Any]] = Field(default_factory=list)
    talent: dict[str, Any] | None = None
    ai_degraded: bool = False


__all__ = ["HelloIn", "HelloOut", "ActionIn", "ActionOut", "StartOut"]
