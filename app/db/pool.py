"""SQLite 连接管理。

关键决策（都是踩坑换来的）：
  * **同步 sqlite3 + FastAPI 的 run_in_threadpool**，不用 aiosqlite。
    回合制文字游戏 QPS 个位数，同步驱动的成熟度与可调试性高得多；
    而 aiosqlite 不支持事务跨 await，很容易写出半提交状态。
  * **每请求开连接、请求结束关**，绝不跨线程共享 connection。
    这是 sqlite3 最常见的误用坑，代价是每请求几十微秒的连接开销，完全可接受。
  * WAL + busy_timeout，解决绝大部分 "database is locked"。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import ROOT, settings

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-16000",
)


def db_path() -> Path:
    p = Path(settings.db_path)
    return p if p.is_absolute() else ROOT / p


def llm_cache_path() -> Path:
    p = Path(settings.llm_cache_path)
    return p if p.is_absolute() else ROOT / p


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """主库连接。退出时关闭（不回滚已提交内容）。"""
    conn = _connect(path or db_path())
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AI 缓存库：长连接 + 互斥锁
#
# 为什么这里不用"每请求短连接"：
#   实测 Windows + WAL 下 sqlite3 的 close() 要 ~22ms，比查询本身贵一个数量级。
#   AI 缓存是每次渲染都要查一次的高频只读路径，短连接会直接拖慢游戏。
#   它是独立 DB 文件、访问入口只有 cache.py / limiter.py 两处，
#   用一把全局锁保护单连接是安全且简单的选择。
# ---------------------------------------------------------------------------
_llm_conn: sqlite3.Connection | None = None
# 必须用可重入锁：缓存命中路径会在已持锁的情况下再去记一次命中数
# （cache.get → _bump_hits），非重入锁会当场死锁，而命中恰恰是稳态下的常态路径。
_llm_lock = threading.RLock()


@contextmanager
def llm_db() -> Iterator[sqlite3.Connection]:
    """AI 缓存库的长连接。所有对它的访问都必须走这里（自带锁）。"""
    global _llm_conn
    with _llm_lock:
        if _llm_conn is None:
            path = llm_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(path), timeout=5.0, isolation_level="DEFERRED",
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            for pragma in PRAGMAS:
                conn.execute(pragma)
            _llm_conn = conn
        yield _llm_conn


@contextmanager
def connect_llm_cache() -> Iterator[sqlite3.Connection]:
    """兼容旧调用点。等价于 llm_db()。"""
    with llm_db() as conn:
        yield conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务。事务内**绝不**做网络或 AI 调用。"""
    with conn:  # 自动 commit / rollback
        yield conn
