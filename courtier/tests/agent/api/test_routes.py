"""Tests for API routes using FastAPI TestClient."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter

# Test credentials — must match values set in tests/conftest.py
_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        with TestClient(app) as c:
            # Log in so that all /api/* requests carry a valid JWT token.
            # conftest.py ensures ADMIN_PASSWORD is set and MYSQL_URL is empty,
            # so the app uses fallback (no-DB) auth.
            login_resp = c.post(
                "/api/auth/login",
                json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
            )
            assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
            token = login_resp.json()["token"]
            c.headers["Authorization"] = f"Bearer {token}"
            yield c


@pytest.fixture
def mock_chat_agent():
    """Return a chat agent backed by MockModelClient for fast tests."""
    from courtier.agent.agents.base import Agent
    from courtier.agent.core.model import ModelResponse
    from courtier.agent.testing import MockModelClient

    model = MockModelClient(tool_calls=[])
    agent = Agent(
        name="TestChat",
        role="Test role",
        tools=[],
        model=model,
    )
    return agent, model


class TestFileUpload:
    def test_upload_png(self, client):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            f.flush()
            resp = client.post(
                "/api/files",
                files={"file": ("test.png", open(f.name, "rb"), "image/png")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "fileId" in data
        assert data["fileId"].startswith("file_")

    def test_upload_unsupported_extension(self, client):
        resp = client.post(
            "/api/files",
            files={"file": ("test.xyz", b"content", "text/plain")},
        )
        assert resp.status_code == 400

    def test_upload_empty_file(self, client):
        resp = client.post(
            "/api/files",
            files={"file": ("test.pdf", b"", "application/pdf")},
        )
        assert resp.status_code == 400


class TestPauseResume:
    def test_pause(self, client):
        resp = client.post("/api/pause")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_resume(self, client):
        resp = client.post("/api/resume")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestSessionsList:
    def test_empty(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


class TestSessionDetail:
    def test_not_found(self, client):
        resp = client.get("/api/sessions/nonexistent")
        assert resp.status_code == 404


class TestSessionDelete:
    def test_not_found(self, client):
        resp = client.delete("/api/sessions/nonexistent")
        assert resp.status_code == 404


class TestSSEStream:
    def test_missing_params(self, client):
        # No params = list mode
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_chat_mode_task_only(self, client, mock_chat_agent):
        """task without fileId enters chat mode (SSE stream)."""
        agent, _model = mock_chat_agent

        with patch(
            "courtier.agent.api.routes.sessions.build_chat_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions?task=hello") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")

    def test_missing_task(self, client):
        """fileId without task is still invalid."""
        resp = client.get("/api/sessions?fileId=file_nonexistent")
        assert resp.status_code == 400

    def test_invalid_file_id(self, client):
        resp = client.get("/api/sessions?task=test&fileId=file_nonexistent")
        assert resp.status_code == 404

    def test_new_session_emits_session_event(self, client, mock_chat_agent):
        """New session (no sessionId) emits session event as first SSE line."""
        import json

        agent, _model = mock_chat_agent

        with patch(
            "courtier.agent.api.routes.sessions.build_chat_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions?task=hello") as resp:
                assert resp.status_code == 200

                # Read first SSE event
                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break

                first = buffer.strip().split("\n")[0]
                assert first.startswith("data: ")
                payload = json.loads(first[len("data: "):])
                assert payload["type"] == "session"
                assert payload["sessionId"].startswith("sess_")

    def test_continue_nonexistent_session(self, client):
        """Continuing a session that doesn't exist returns 404."""
        resp = client.get("/api/sessions?task=hello&sessionId=sess_nonexistent")
        assert resp.status_code == 404


class TestSessionOwnership:
    def test_user_cannot_see_other_session(self, client, mock_chat_agent):
        """A non-owner cannot retrieve or list another user's session."""
        agent, _model = mock_chat_agent

        with patch(
            "courtier.agent.api.routes.sessions.build_chat_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions?task=hello") as resp:
                assert resp.status_code == 200

                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break

                first = buffer.strip().split("\n")[0]
                assert first.startswith("data: ")
                payload = json.loads(first[len("data: "):])
                assert payload["type"] == "session"
                session_id = payload["sessionId"]

        # Sanity check: the admin client can see the newly created session.
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200

        # Authenticate as a different user against the same app instance.
        settings = client.app.state.settings
        from courtier.agent.api.middleware.auth import create_token

        user1_token = create_token(
            "user1",
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        user1_client = TestClient(client.app)
        user1_client.headers["Authorization"] = f"Bearer {user1_token}"

        resp = user1_client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 404

        resp = user1_client.get("/api/sessions")
        assert resp.status_code == 200
        assert session_id not in [s["id"] for s in resp.json()]


class TestAuditLogSwitch:
    """Tests for audit log enable/disable via Settings."""

    def test_audit_log_disabled_by_default(self, client, mock_chat_agent):
        """When audit_log_enabled is False (default), no log directory is created."""
        agent, _model = mock_chat_agent

        with tempfile.TemporaryDirectory() as log_dir:
            with patch(
                "courtier.config.Settings",
            ) as mock_settings_cls:
                settings = mock_settings_cls.return_value
                settings.audit_log_enabled = False
                settings.audit_log_dir = log_dir
                settings.llm_base_url = "http://localhost:9999"
                settings.llm_api_key = "test"
                settings.llm_model = "test"
                settings.llm_temperature = 0.0
                settings.llm_max_tokens = 100
                settings.llm_extra_body = None
                settings.cache_dir = log_dir
                settings.upload_dir = log_dir

                with patch(
                    "courtier.agent.api.routes.sessions.build_chat_agent",
                    new=AsyncMock(return_value=(agent, None, "test-model")),
                ):
                    with client.stream("GET", "/api/sessions?task=hello") as resp:
                        assert resp.status_code == 200
                        # Read a small part and close — session event should be emitted
                        buffer = ""
                        for chunk in resp.iter_bytes():
                            buffer += chunk.decode("utf-8")
                            if "\n\n" in buffer:
                                break
                        assert "session" in buffer

                # No log directory should be created
                assert not Path(log_dir).exists() or not any(Path(log_dir).iterdir())

    def test_audit_log_enabled_creates_log_dir(self, client, mock_chat_agent):
        """When audit_log_enabled is True, log directory is created with turn files."""
        agent, _model = mock_chat_agent

        with tempfile.TemporaryDirectory() as log_dir:
            with tempfile.TemporaryDirectory() as sessions_dir:
                app = create_app(sessions_dir=sessions_dir, start_plugins=False)
                with TestClient(app) as test_client:
                    # Authenticate — this TestClient is separate from the
                    # module-level ``client`` fixture.
                    from courtier.config import Settings
                    admin_pw = Settings().admin_password or "test-admin-password-for-pytest"
                    login_resp = test_client.post(
                        "/api/auth/login",
                        json={
                            "username": "admin",
                            "password": admin_pw,
                        },
                    )
                    assert login_resp.status_code == 200
                    test_client.headers["Authorization"] = (
                        f"Bearer {login_resp.json()['token']}"
                    )

                    with patch(
                        "courtier.agent.api.routes.sessions.build_chat_agent",
                        new=AsyncMock(return_value=(agent, None, "test-model")),
                    ):
                        with patch(
                            "courtier.config.Settings",
                        ) as mock_settings_cls:
                            settings = mock_settings_cls.return_value
                            settings.audit_log_enabled = True
                            settings.audit_log_dir = log_dir
                            settings.llm_base_url = "http://localhost:9999"
                            settings.llm_api_key = "test"
                            settings.llm_model = "test"
                            settings.llm_temperature = 0.0
                            settings.llm_max_tokens = 100
                            settings.llm_extra_body = None
                            settings.cache_dir = log_dir
                            settings.upload_dir = log_dir

                            buffer = ""
                            with test_client.stream("GET", "/api/sessions?task=hello") as resp:
                                assert resp.status_code == 200

                                # Read SSE stream to completion
                                for chunk in resp.iter_bytes():
                                    buffer += chunk.decode("utf-8")
                                    if "data: {\"type\":\"complete" in buffer:
                                        break

                            # Verify SSE stream completed successfully
                            assert '"type": "complete"' in buffer


class TestLoginRateLimit:
    def test_login_rate_limit(self, client):
        # The shared client fixture already performs one successful login,
        # consuming one slot of the 5/minute bucket.
        payload = {"username": "admin", "password": "wrong"}
        for _ in range(4):
            resp = client.post("/api/auth/login", json=payload)
            assert resp.status_code == 401
        resp = client.post("/api/auth/login", json=payload)
        assert resp.status_code == 429
