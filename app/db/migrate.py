"""版本化迁移：用 PRAGMA user_version 记录当前版本，启动即对齐。

schema.sql 是幂等的（全 CREATE TABLE IF NOT EXISTS），
所以真正需要写迁移脚本的只有"改表结构"的情况，届时在此列表追加。
"""
from __future__ import annotations

from pathlib import Path

from .pool import connect, db_path

SCHEMA_VERSION = 1

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# 未来结构变更在此追加：
#   (目标版本, ["ALTER TABLE ...", ...])
MIGRATIONS: list[tuple[int, list[str]]] = []


def apply_migrations() -> int:
    """把数据库对齐到 SCHEMA_VERSION。返回最终版本号。"""
    with connect() as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]

        if current == 0:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            return SCHEMA_VERSION

        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {current} 高于程序支持的 {SCHEMA_VERSION}，"
                "请升级程序或使用新的数据库文件。"
            )

        for target, statements in MIGRATIONS:
            if current >= target:
                continue
            with conn:
                for sql in statements:
                    conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {target}")
            current = target

    return SCHEMA_VERSION


def init_llm_cache_db() -> None:
    """AI 缓存库（独立文件，避免写放大污染主库）。"""
    from .pool import llm_db

    with llm_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key   TEXT PRIMARY KEY,
                prompt_hash TEXT NOT NULL,
                output      TEXT NOT NULL,
                model       TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                hit_count   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_llm_cache_time ON llm_cache(created_at);

            CREATE TABLE IF NOT EXISTS llm_usage (
                day         TEXT PRIMARY KEY,
                tokens_in   INTEGER NOT NULL DEFAULT 0,
                tokens_out  INTEGER NOT NULL DEFAULT 0,
                cost_cents  INTEGER NOT NULL DEFAULT 0,
                calls       INTEGER NOT NULL DEFAULT 0,
                cache_hits  INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()


def db_info() -> dict:
    p = db_path()
    return {"path": str(p), "exists": p.exists(), "version": SCHEMA_VERSION}
