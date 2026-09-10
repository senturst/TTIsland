"""回归测试：缓存命中路径不得死锁。

曾经的真实故障：cache.get() 在持有 llm_db() 全局锁的情况下调用 _bump_hits()，
而后者又去申请同一把非重入锁 → 死锁。
表现是 /api/meta/config 正常但 /api/health 永久挂起，且只在缓存命中后复现——
而命中恰恰是稳态下的常态路径，所以服务跑一会就整体卡死。

这个测试直接跑缓存命中路径，卡住就会超时失败。
"""
from __future__ import annotations

import threading
import time

from app.ai import cache


def _run_with_timeout(fn, seconds: float = 10.0):
    """在子线程跑，超时即判定为死锁。"""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError("缓存命中路径发生死锁（超过 %.1fs 未返回）" % seconds)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_cache_hit_does_not_deadlock():
    """连续两次 get：第二次走 SQLite 命中路径，正是当初死锁的地方。"""
    key = cache.make_key("room_atmosphere", "vtest", "test-model", "prompt-abc")
    cache.set_value(key, "测试文本", "test-model", "hash")

    first = _run_with_timeout(lambda: cache.get(key))
    assert first == "测试文本", f"首次读取失败: {first!r}"

    # 清空内存层，强制走 SQLite 命中分支（原死锁点）
    cache._memory._data.clear()
    second = _run_with_timeout(lambda: cache.get(key))
    assert second == "测试文本", f"SQLite 命中路径失败: {second!r}"

    # 内存命中分支也要跑一遍（_bump_hits 在锁外）
    third = _run_with_timeout(lambda: cache.get(key))
    assert third == "测试文本"


def test_repeated_hits_stay_fast():
    """连续命中不应因锁竞争而变慢。"""
    key = cache.make_key("encounter_open", "vtest", "test-model", "prompt-xyz")
    cache.set_value(key, "文本", "test-model", "hash")
    cache.get(key)  # 预热

    start = time.perf_counter()
    for _ in range(200):
        cache.get(key)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"200 次命中耗时 {elapsed:.2f}s，疑似锁竞争"


if __name__ == "__main__":
    test_cache_hit_does_not_deadlock()
    test_repeated_hits_stay_fast()
    print("缓存死锁回归测试通过")
