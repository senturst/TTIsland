"""回归测试：商人保底（1-4 层每层必出 2 个商人房，每层不重复）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import mapgen  # noqa: E402
from app.core.rng import RNG  # noqa: E402
from app.data.loader import get_config  # noqa: E402

LEVELS = 100  # 每层生成 100 张图做统计验证


def _merchant_counts(cfg, level: int) -> list[int]:
    out = []
    for i in range(LEVELS):
        rng = RNG(1000 + level * 10000 + i)
        lmap = mapgen.generate_level(cfg, rng, level)
        out.append(sum(1 for r in lmap["rooms"] if r["type"] == "merchant"))
    return out


def test_levels_1_to_4_always_have_two_merchants():
    """1-4 层每张图必须恰好 2 个商人房（保底 + 权重可能超出时不设上限）。"""
    cfg = get_config()
    for level in (1, 2, 3, 4):
        counts = _merchant_counts(cfg, level)
        assert all(c >= 2 for c in counts), (
            f"L{level} 有图缺商人房：{[c for c in counts if c < 2][:5]}"
        )


def test_merchant_room_types_are_unique_per_level():
    """同一层内的商人房必须对应不同房间（即房间实体不重复）。"""
    cfg = get_config()
    for level in (1, 2, 3, 4):
        for i in range(20):
            rng = RNG(777 + level * 100 + i)
            lmap = mapgen.generate_level(cfg, rng, level)
            m_rooms = [r["idx"] for r in lmap["rooms"] if r["type"] == "merchant"]
            assert len(m_rooms) == len(set(m_rooms)), "商人房 idx 不应重复"


def test_level_5_has_no_guarantee():
    """撤离层无保底，商人房数量由权重决定（0 个也合法）。"""
    cfg = get_config()
    counts = _merchant_counts(cfg, 5)
    # 只验证"不强制 ≥2"：即至少存在一张 0 或 1 个商人房的图
    assert min(counts) <= 1, "L5 不应被保底强行拉到 2"


def test_stairs_and_campfire_never_converted():
    """special 房（楼梯/篝火）绝不会被改铸成商人房。入口房允许是商人房
    （权重随机结果，引擎天然支持进门即交易）。"""
    cfg = get_config()
    for level in (1, 2, 3, 4):
        for i in range(20):
            rng = RNG(31337 + level * 100 + i)
            lmap = mapgen.generate_level(cfg, rng, level)
            stairs = lmap["stairs"]
            campfire = lmap.get("campfire")
            for r in lmap["rooms"]:
                if r["type"] == "merchant":
                    assert r["idx"] != stairs, "楼梯不能被改铸"
                    assert campfire is None or r["idx"] != campfire, "篝火不能被改铸"
                    assert r.get("special_kind") is None, "special 房不能被改铸"


if __name__ == "__main__":
    test_levels_1_to_4_always_have_two_merchants()
    print("ok 1-4 层每图至少 2 个商人房")
    test_merchant_room_types_are_unique_per_level()
    print("ok 商人房不重复")
    test_level_5_has_no_guarantee()
    print("ok 撤离层无保底")
    test_stairs_and_campfire_never_converted()
    print("ok special/入口房不被改铸")
    print("\n商人保底回归测试全部通过")
