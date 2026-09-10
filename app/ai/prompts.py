"""提示词模板加载与渲染。

模板版本 = 文件内容的 hash，写进缓存键。
这样改了提示词就自动失效，不需要记得去清缓存——
忘记清缓存是这类系统最常见的"改了没效果"来源。
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from ..config import ROOT

PROMPT_DIR = ROOT / "configs" / "prompts"


class PromptTemplate:
    def __init__(self, name: str, system: str, user: str, version: str) -> None:
        self.name = name
        self.system = system
        self.user = user
        self.version = version

    def render(self, variables: dict) -> tuple[str, str]:
        return self._fill(self.system, variables), self._fill(self.user, variables)

    @staticmethod
    def _fill(text: str, variables: dict) -> str:
        for k, v in variables.items():
            text = text.replace("{" + k + "}", str(v))
        return text


def _parse(raw: str, name: str) -> PromptTemplate:
    system, user = "", ""
    current = None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[system]"):
            current = "system"
            continue
        if stripped.startswith("[user]"):
            current = "user"
            continue
        if current == "system":
            system += line + "\n"
        elif current == "user":
            user += line + "\n"
    if not system or not user:
        raise ValueError(f"提示词模板 {name} 缺少 [system] 或 [user] 段")
    version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return PromptTemplate(name, system.strip(), user.strip(), version)


@lru_cache(maxsize=64)
def _load(name: str, mtime: float) -> PromptTemplate:  # noqa: ARG001
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"缺少提示词模板: {path}")
    return _parse(path.read_text(encoding="utf-8"), name)


def get_template(name: str) -> PromptTemplate:
    path = PROMPT_DIR / f"{name}.txt"
    return _load(name, path.stat().st_mtime if path.exists() else 0.0)


def all_versions() -> dict[str, str]:
    return {
        p.stem: get_template(p.stem).version
        for p in sorted(PROMPT_DIR.glob("*.txt"))
    }
