"""应用设置：从 .env 读取，缺失即用安全默认值。

AI 是可选的——没有 Key 时游戏照常可玩，只是用模板文本。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True

    # ---- 数据库 ----
    db_path: str = "data/game.db"
    llm_cache_path: str = "data/llm_cache.db"

    # ---- AI（可选）----
    # 留空则全局降级到模板文本，游戏完全可玩。
    #
    # 密钥一律放 .env，**不要写回这里当默认值**——源码里的明文密钥迟早会被提交出去。
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_enabled: bool = True
    llm_timeout: float = 8.0
    # 强情感节点（墓志铭/死亡旁白）同步等待的超时，更短
    llm_sync_timeout: float = 3.0

    # ---- 成本控制硬上限 ----
    llm_daily_budget_cents: int = 100
    llm_max_calls_per_day: int = 3000
    # 连续失败多少次后熔断
    llm_circuit_threshold: int = 5
    llm_circuit_cooldown: int = 300
    # 并发上限
    llm_max_concurrency: int = 4
    # 令牌桶：每秒补充速率 / 桶容量
    llm_rate_per_sec: float = 0.5
    llm_rate_burst: int = 4

    # ---- 限流 ----
    rate_action_per_min: int = 60
    rate_start_per_min: int = 10

    @property
    def ai_active(self) -> bool:
        """AI 是否可用：开关打开且配置了 Key。"""
        return bool(self.llm_enabled and self.llm_api_key.strip())


settings = Settings()
