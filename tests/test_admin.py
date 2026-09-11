"""回归测试：管理后台。

覆盖：
  - 错误令牌被拒（401）
  - 受保护端点未登录被拒（401）
  - 正确令牌登录后拿到会话 cookie，可访问配置/玩家
  - 配置读取与回写（合法 YAML 通过，非法 YAML 被拒）
  - 玩家列表含存活在玩的 run，状态修改能落库且 status 受保护
"""
from __future__ import annotations

import os

os.environ.setdefault("LLM_ENABLED", "false")
os.environ["ADMIN_TOKEN"] = "test-admin-token-123"  # 必须在导入 app 前设好

from fastapi.testclient import TestClient  # noqa: E402

from app.db import repo  # noqa: E402
from app.main import app  # noqa: E402


def _client() -> TestClient:
    return TestClient(app)


def test_admin_login_rejects_wrong_token():
    with _client() as c:
        r = c.post("/api/admin/login", json={"token": "nope"})
        assert r.status_code == 401


def test_admin_protected_requires_auth():
    with _client() as c:
        assert c.get("/api/admin/configs").status_code == 401
        assert c.get("/api/admin/players").status_code == 401


def test_admin_login_and_config_flow():
    with _client() as c:
        r = c.post("/api/admin/login", json={"token": "test-admin-token-123"})
        assert r.status_code == 200
        assert "tt_admin" in c.cookies

        # 已登录：列配置
        r = c.get("/api/admin/configs")
        assert r.status_code == 200
        files = r.json()["files"]
        assert "balance.yaml" in files
        assert "admin.yaml" not in files  # 令牌文件不在可编辑列表

        # 读 + 回写 balance.yaml（应热重载成功）
        r = c.get("/api/admin/configs/balance.yaml")
        assert r.status_code == 200
        content = r.json()["content"]
        r = c.put("/api/admin/configs/balance.yaml", json={"content": content})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 非法 YAML 被拒
        r = c.put("/api/admin/configs/balance.yaml", json={"content": "::: broken : ["})
        assert r.status_code == 400


def test_admin_player_flow():
    repo.players.get_or_create("admin_test_player")
    st = {
        "player_name": "测试员", "depth": 2, "hp": 10, "hp_max": 20,
        "stamina": 5, "infection": 30, "cash": 0, "scrap": 0,
        "in_combat": False, "score": 7, "status": "active", "seed": 1,
        "inventory": [], "weapon": None, "armor": None,
        "log": [], "level_map": {}, "room": {}, "combat": {},
    }
    run_id = repo.runs.create("admin_test_player", 1, st)
    try:
        with _client() as c:
            c.post("/api/admin/login", json={"token": "test-admin-token-123"})
            r = c.get("/api/admin/players")
            assert r.status_code == 200
            ids = [p["run_id"] for p in r.json()["players"]]
            assert run_id in ids

            # 修改状态（hp / cash），应落库；status 必须保持 active（受保护）
            st2 = dict(st)
            st2["hp"] = 99
            st2["cash"] = 50
            r = c.put(f"/api/admin/players/{run_id}", json={"state": st2})
            assert r.status_code == 200 and r.json()["ok"]

            saved = repo.runs.load_state(repo.runs.get(run_id))
            assert saved["hp"] == 99 and saved["cash"] == 50
            assert saved["status"] == "active"
    finally:
        repo.runs.finish(run_id, "dead", 7)
