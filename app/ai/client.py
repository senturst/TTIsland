"""OpenAI 兼容协议的 LLM 客户端（默认 DeepSeek）。

两件事比"调通 API"更重要：
  1. **输出强校验**——模型返回 Markdown、多段、英文、emoji 都会搞坏文字游戏的排版。
     一律判非法，直接降级到模板。
  2. **成本估算**——每次调用都记账，供日预算闸使用。
"""
from __future__ import annotations

import re
import unicodedata

import httpx

from ..config import settings

# 各类内容的最大字数（超出即判非法）
MAX_CHARS = {
    "room_atmosphere": 45,
    "encounter_open": 40,
    "item_desc": 30,
    "epitaph": 40,
    "death_narration": 40,
    "grave_desc": 45,
}

# DeepSeek 每百万 token 价格（元），用于估算成本
PRICE_PER_MTOK_IN = 2.0
PRICE_PER_MTOK_OUT = 8.0
CNY_TO_CENTS = 100 / 7.2  # 粗略汇率，够用于预算闸

_MD_PATTERN = re.compile(r"[*`#\[\]<>]|```|^[-+]|^\d+\.")
_EMOJI_PATTERN = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f000-\U0001f2ff" "]"
)


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def validate(text: str, content_type: str) -> tuple[bool, str]:
    """校验模型输出。返回 (是否合法, 清理后的文本)。"""
    if not text:
        return False, ""

    # 只取第一行，且去掉首尾空白与包裹引号
    text = text.strip().splitlines()[0].strip()
    text = text.strip("\"'“”‘’ 　")

    if not text:
        return False, ""

    # 换行 / Markdown / emoji
    if _MD_PATTERN.search(text):
        return False, text
    if _EMOJI_PATTERN.search(text):
        return False, text

    # 中文占比：文字游戏必须主要是中文
    cjk = sum(1 for c in text if _is_cjk(c))
    if cjk < len(text) * 0.4:
        return False, text

    # 长度
    if len(text) > MAX_CHARS.get(content_type, 45) * 2:  # 宽松上限，先砍明显的长尾
        return False, text
    if len(text) > MAX_CHARS.get(content_type, 45):
        return False, text

    # 全角标点统一，避免混排
    text = text.replace(",", "，").replace(";", "；").replace(":", "：")
    return True, text


def estimate_cost_cents(tokens_in: int, tokens_out: int) -> int:
    cost_cny = (
        tokens_in / 1_000_000 * PRICE_PER_MTOK_IN
        + tokens_out / 1_000_000 * PRICE_PER_MTOK_OUT
    )
    return int(cost_cny * CNY_TO_CENTS) + 1


def estimate_tokens(text: str) -> int:
    """中文按 1 字 ≈ 0.7 token 粗估。够用。"""
    cjk = sum(1 for c in text if _is_cjk(c))
    others = len(text) - cjk
    return int(cjk * 0.7 + others * 0.4) + 1


async def chat(
    system: str,
    user: str,
    *,
    max_tokens: int = 80,
    timeout: float | None = None,
) -> tuple[str, int, int]:
    """调用一次 chat completion。返回 (文本, tokens_in, tokens_out)。

    任何异常都向上传播，由 service 层降级处理。
    """
    timeout = timeout if timeout is not None else settings.llm_timeout

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.9,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or estimate_tokens(system + user))
    tokens_out = int(usage.get("completion_tokens") or estimate_tokens(text))
    return text, tokens_in, tokens_out


__all__ = ["chat", "validate", "estimate_cost_cents", "estimate_tokens", "MAX_CHARS"]
