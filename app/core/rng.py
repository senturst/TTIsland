"""可复现随机数。

每个 run 持有一个 RNG 实例，其状态随 state_json 一起持久化。
这样断线重连、刷新页面后继续游戏，随机序列不会断裂或被复用——
否则玩家可以通过刷新页面反复抽同一个掉落。
"""
from __future__ import annotations

import random
from typing import Any, Sequence


class RNG:
    """包装 random.Random，状态可 JSON 序列化。"""

    def __init__(self, seed: int | None = None, state: dict | None = None) -> None:
        self._r = random.Random(seed)
        if state:
            self.set_state(state)

    # ---- 状态持久化 ----
    def get_state(self) -> dict:
        version, internal, gauss_next = self._r.getstate()
        return {
            "v": version,
            "s": list(internal),
            "g": gauss_next,
        }

    def set_state(self, state: dict) -> None:
        self._r.setstate((state["v"], tuple(state["s"]), state["g"]))

    # ---- 基础方法 ----
    def random(self) -> float:
        return self._r.random()

    def randint(self, a: int, b: int) -> int:
        return self._r.randint(a, b)

    def randrange(self, start: int, stop: int | None = None) -> int:
        return self._r.randrange(start, stop) if stop is not None else self._r.randrange(start)

    def rand_float(self, a: float, b: float) -> float:
        return a + (b - a) * self._r.random()

    def chance(self, p: float) -> bool:
        """概率 p 返回 True。"""
        return self._r.random() < p

    def choice(self, seq: Sequence[Any]) -> Any:
        return self._r.choice(seq)

    def weighted_choice(self, items: Sequence[Any], weights: Sequence[float]) -> Any:
        """按权重抽一个。items 与 weights 等长。"""
        if not items:
            raise ValueError("weighted_choice: 空序列")
        total = sum(weights)
        if total <= 0:
            return self._r.choice(items)
        r = self._r.random() * total
        acc = 0.0
        for item, w in zip(items, weights):
            acc += w
            if r <= acc:
                return item
        return items[-1]

    def shuffle(self, lst: list) -> None:
        self._r.shuffle(lst)

    def rand_range_int(self, pair: Sequence[int]) -> int:
        """配置里的 [min, max] 区间取一个整数。"""
        return self._r.randint(int(pair[0]), int(pair[1]))

    def rand_value(self, value: int | float | Sequence[int | float]) -> int:
        """配置值可能是定值（5）或区间（[2,4]），统一处理。

        事件表里两种写法都会出现——定值用于"确定扣 5 点感染"，
        区间用于"随机 2~4 点"。写配置的人不需要关心区别。
        """
        if isinstance(value, (list, tuple)):
            if len(value) == 1:
                return int(value[0])
            return self._r.randint(int(value[0]), int(value[1]))
        return int(value)

    def rand_range_float(self, pair: Sequence[float]) -> float:
        a, b = float(pair[0]), float(pair[1])
        return a if b <= a else self._r.randint(int(a), int(b))


def new_seed() -> int:
    return random.SystemRandom().randrange(1, 2**31)
