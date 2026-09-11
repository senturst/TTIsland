"""玩家昵称审核：优先走 DeepSeek 判断违法 / 政治敏感 / 色情 / 辱骂 / 仇恨 / 明显不雅，
失败（未开 AI、超时、解析异常）则降级到本地规则，保证离线也能玩。

设计取舍：
  - 宁可漏放也不误杀——解析失败时保守放行，避免正常名字被卡死。
  - 本地兜底只挡「明显不行」的：空、超长、非法字符、极小黑名单。
    真正的语义审核交给模型，本地不维护大词表（维护即滞后）。
"""
from __future__ import annotations

import json
import logging
import re

from ..config import settings
from .client import chat

logger = logging.getLogger("ai.name_filter")

MAX_NAME = 16

# 本地兜底黑名单：仅作离线降级，放极少量明显违法/辱骂词，不做大词表
_LOCAL_BLOCK = [
    "fuck", "shit", "nazi", "色情", "习近平", "法轮", "胡锦涛", "江泽民", "李克强",
]

_NAME_RE = re.compile(r"^[A-Za-z0-9一-鿿 .\-_]{1,16}$")

_SYSTEM = (
    "你是末世生存文字游戏的昵称审核员。判断玩家想用的昵称是否含有违法、政治敏感、"
    "色情、辱骂、仇恨或明显不雅内容。只回答一行 JSON，不要任何解释："
    '审核通过输出 {"ok":true}；不通过输出 {"ok":false,"reason":"简短中文原因"}。'
)


def _local_check(name: str) -> tuple[bool, str]:
    if not name or not name.strip():
        return False, "名字不能为空"
    if len(name) > MAX_NAME:
        return False, f"名字不超过 {MAX_NAME} 个字"
    if not _NAME_RE.match(name):
        return False, "名字含不支持的字符（仅限中英数、空格与 - _ .）"
    low = name.lower()
    if any(w in low for w in _LOCAL_BLOCK):
        return False, "含有不雅或违规词汇"
    return True, ""


async def check_name(name: str) -> tuple[bool, str]:
    """返回 (是否通过, 不通过原因)。通过则原样返回。"""
    ok, reason = _local_check(name)
    if not ok:
        return False, reason

    # 离线降级：本地已过即放行，避免无 Key 时卡住开局
    if not settings.ai_active:
        return True, ""

    try:
        raw, _tin, _tout = await chat(
            _SYSTEM, f"昵称：{name}", max_tokens=48, timeout=6.0
        )
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            data = json.loads(m.group(0))
            if data.get("ok"):
                return True, ""
            return False, str(data.get("reason", "未通过审核"))
        # 解析失败：保守放行
        return True, ""
    except Exception as e:  # 任意异常都降级，绝不阻塞开局
        logger.warning("name filter LLM 失败，降级放行：%s", e)
        return True, ""


__all__ = ["check_name", "MAX_NAME"]
