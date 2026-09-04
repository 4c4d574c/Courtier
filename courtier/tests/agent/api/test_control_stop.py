"""Route-level tests for POST /api/stop — permission and
not-running branches (the running-session path lives in RunManager tests).
"""

import pytest

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        import os

        os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password-for-pytest")
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            login = c.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": os.environ["ADMIN_PASSWORD"],
                },
            )
            assert login.status_code == 200
            c.headers["Authorization"] = f"Bearer {login.json()['token']}"
            yield c


class TestControlStop:
    def test_stop_unknown_session_404(self, client):
        resp = client.post(
            "/api/stop", params={"sessionId": "sess_doesnotexist"}
        )
        assert resp.status_code == 404

    def test_stop_all_without_session_id_admin_ok(self, client):
        """Fallback admin (no session running) gets the zero-stop answer."""
        resp = client.post("/api/stop")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["stopped"] == 0
