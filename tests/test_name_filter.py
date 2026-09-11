"""开局命名 + DeepSeek 审核的回归测试。

关键点：
  - 本地兜底规则（空 / 超长 / 非法字符 / 黑名单）必须生效，且不依赖网络。
  - /api/player/set_name 审核通过后落库并标记 named=1，hello 不再 needs_name。
  - 离线（LLM 关闭）时本地已过即放行，不阻塞开局。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LLM_ENABLED", "false")  # 走本地兜底，避免网络抖动
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_set_name_flow_and_local_filter():
    from app.config import settings

    tmp = Path(tempfile.mkdtemp()) / "game.db"
    settings.db_path = str(tmp)

    from app.ai.name_filter import check_name
    from fastapi.testclient import TestClient
    from app.main import app

    async def chk(name):
        return await check_name(name)

    # 1) 本地规则：空名不过
    ok, reason = asyncio.run(chk(""))
    assert not ok and "空" in reason

    # 2) 本地规则：合法名放行（离线，无网络）
    ok2, _ = asyncio.run(chk("幸存者阿强"))
    assert ok2

    # 3) 端点：首登 needs_name，set_name 后不再 needs_name
    with TestClient(app) as client:
        headers = {"X-Client-Id": "unit-name-1"}
        r = client.post("/api/player/hello", json={}, headers=headers)
        body = r.json()
        assert body["needs_name"] is True, "默认随机名应 needs_name"

        bad = client.post("/api/player/set_name", params={"name": ""}, headers=headers)
        assert bad.status_code == 200 and bad.json()["ok"] is False, "空名应被拒"

        good = client.post("/api/player/set_name", params={"name": "孤岛老王"}, headers=headers)
        assert good.status_code == 200 and good.json()["ok"] is True
        assert good.json()["name"] == "孤岛老王"

        r2 = client.post("/api/player/hello", json={}, headers=headers)
        assert r2.json()["needs_name"] is False, "设名后不应再 needs_name"
        assert r2.json()["player"]["name"] == "孤岛老王"


if __name__ == "__main__":
    test_set_name_flow_and_local_filter()
    print("✓ 开局命名 + 审核流程通过")
