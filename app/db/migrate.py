"""版本化迁移：用 PRAGMA user_version 记录当前版本，启动即对齐。

schema.sql 是幂等的（全 CREATE TABLE IF NOT EXISTS），
所以真正需要写迁移脚本的只有"改表结构"的情况，届时在此列表追加。
"""
from __future__ import annotations

from pathlib import Path

from .pool import connect, db_path

SCHEMA_VERSION = 5

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# 未来结构变更在此追加：
#   (目标版本, ["ALTER TABLE ...", ...])
MIGRATIONS: list[tuple[int, list[str]]] = [
    # v2：P4 私人回执表
    (2, [
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            body        TEXT NOT NULL,
            data_json   TEXT NOT NULL DEFAULT '{}',
            created_at  INTEGER NOT NULL,
            read        INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_notif_player ON notifications(player_id, read, created_at DESC)",
    ]),
    # v3：世界事件播报表（P3 重大事件持久化，SSE 只是实时通道）
    (3, [
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id   TEXT NOT NULL,
            player_name TEXT NOT NULL,
            channel     TEXT NOT NULL DEFAULT 'world',
            kind        TEXT NOT NULL DEFAULT 'chat',
            body        TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_chat_time ON chat_messages(channel, created_at DESC)",
    ]),
    # v4：玩家自定名标记（开局让玩家输入名字；默认随机名需提示）
    (4, [
        "ALTER TABLE players ADD COLUMN named INTEGER NOT NULL DEFAULT 0",
    ]),
    # v5：runs.updated_at —— 挂机清理用「最后活动时间」判断，而非 started_at
    (5, [
        "ALTER TABLE runs ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0",
        "UPDATE runs SET updated_at = COALESCE(ended_at, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at) WHERE status = 'active'",
    ]),
]


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
