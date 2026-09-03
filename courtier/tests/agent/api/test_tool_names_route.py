"""GET /api/admin/settings/tool-names — 已知工具名（含插件工具）。"""

import os

import pytest
from fastapi.testclient import TestClient

from courtier.agent.api.app import create_app

_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from courtier.agent.api.rate_limiter import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        with TestClient(app) as c:
            login = c.post(
                "/api/auth/login",
                json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
            )
            assert login.status_code == 200
            c.headers["Authorization"] = f"Bearer {login.json()['token']}"
            yield c


def test_tool_names_sorted_and_contains_baseline(client):
    resp = client.get("/api/admin/settings/tool-names")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    # 内置三件套始终在名单内（平台基线）；插件工具经注册表并入
    assert {"read", "edit", "write"} <= set(tools)
    assert tools == sorted(tools)
