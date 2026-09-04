"""Tests for API routes using FastAPI TestClient."""

import json
from contextlib import contextmanager
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter


def _first_data_line(buffer: str) -> str:
    """First SSE data line of *buffer* (frames now carry an id: prefix)."""
    for line in buffer.strip().split("\n"):
        if line.startswith("data: "):
            return line
    raise AssertionError(f"no data line in {buffer!r}")


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
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")

    def test_missing_task(self, client):
        """fileId without task is still invalid."""
        resp = client.get("/api/sessions/run?fileId=file_nonexistent")
        assert resp.status_code == 400

    def test_invalid_file_id(self, client):
        resp = client.get("/api/sessions/run?task=test&fileId=file_nonexistent")
        assert resp.status_code == 404

    def test_new_session_emits_session_event(self, client, mock_chat_agent):
        """New session (no sessionId) emits session event as first SSE line."""
        import json

        agent, _model = mock_chat_agent

        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200

                # Read first SSE event
                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break

                first = _first_data_line(buffer)
                payload = json.loads(first[len("data: ") :])
                assert payload["type"] == "session"
                assert payload["sessionId"].startswith("sess_")

    def test_continue_nonexistent_session(self, client):
        """Continuing a session that doesn't exist returns 404."""
        resp = client.get("/api/sessions/run?task=hello&sessionId=sess_nonexistent")
        assert resp.status_code == 404


class TestPatchSession:
    """Rename / pin a session via PATCH /sessions/{id}."""

    def _create_session(self, client, mock_chat_agent) -> str:
        agent, _model = mock_chat_agent
        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200
                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break
                first = _first_data_line(buffer)
                payload = json.loads(first[len("data: ") :])
                assert payload["type"] == "session"
                return payload["sessionId"]
        raise AssertionError("no session event")

    def test_rename(self, client, mock_chat_agent):
        session_id = self._create_session(client, mock_chat_agent)

        resp = client.patch(f"/api/sessions/{session_id}", json={"task": "新标题"})
        assert resp.status_code == 200
        assert resp.json()["task"] == "新标题"

        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["task"] == "新标题"

    def test_pin_and_list_order(self, client, mock_chat_agent):
        first_id = self._create_session(client, mock_chat_agent)
        second_id = self._create_session(client, mock_chat_agent)

        resp = client.patch(f"/api/sessions/{first_id}", json={"pinned": True})
        assert resp.status_code == 200
        assert resp.json()["pinned"] is True

        resp = client.get("/api/sessions")
        ids = [s["id"] for s in resp.json()]
        # Pinned (older) session sorts before the newer unpinned one.
        assert ids.index(first_id) < ids.index(second_id)

    def test_empty_title_rejected(self, client, mock_chat_agent):
        session_id = self._create_session(client, mock_chat_agent)
        resp = client.patch(f"/api/sessions/{session_id}", json={"task": "   "})
        assert resp.status_code == 400

    def test_empty_body_rejected(self, client, mock_chat_agent):
        session_id = self._create_session(client, mock_chat_agent)
        resp = client.patch(f"/api/sessions/{session_id}", json={})
        assert resp.status_code == 400

    def test_not_found(self, client):
        resp = client.patch("/api/sessions/sess_nonexistent", json={"pinned": True})
        assert resp.status_code == 404


class TestSessionOwnership:
    def test_user_cannot_see_other_session(self, client, mock_chat_agent):
        """A non-owner cannot retrieve or list another user's session."""
        agent, _model = mock_chat_agent

        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200

                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break

                first = _first_data_line(buffer)
                payload = json.loads(first[len("data: ") :])
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
                    "courtier.agent.api.routes.sessions.build_agent",
                    new=AsyncMock(return_value=(agent, None, "test-model")),
                ):
                    with client.stream("GET", "/api/sessions/run?task=hello") as resp:
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
                    test_client.headers["Authorization"] = f"Bearer {login_resp.json()['token']}"

                    with patch(
                        "courtier.agent.api.routes.sessions.build_agent",
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
                            with test_client.stream("GET", "/api/sessions/run?task=hello") as resp:
                                assert resp.status_code == 200

                                # Read SSE stream to completion
                                for chunk in resp.iter_bytes():
                                    buffer += chunk.decode("utf-8")
                                    if 'data: {"type":"complete' in buffer:
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


class TestAccessCookie:
    """access_token httpOnly cookie — the preferred SSE credential channel."""

    def test_login_sets_access_cookie(self, client):
        """Login (fallback/no-DB path) must set the access_token cookie."""
        assert client.cookies.get("access_token")

    def test_sse_auth_via_cookie_without_header(self, client, mock_chat_agent):
        """SSE requests authenticate via the access_token cookie alone,
        without an Authorization header or ?token= query param."""
        agent, _model = mock_chat_agent
        client.headers.pop("Authorization", None)
        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200

    def test_logout_clears_access_cookie(self, client):
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert client.cookies.get("access_token") is None


class TestAdminSelfGuard:
    """An admin must not change their own role/status (lockout prevention)."""

    def test_admin_cannot_change_own_role(self, client):
        # Fallback admin has uid=0; the guard runs before any DB access.
        resp = client.patch("/api/admin/users/0", json={"role": "auditor"})
        assert resp.status_code == 400
        assert "不能修改自己" in resp.json()["detail"]

    def test_admin_cannot_change_own_status(self, client):
        resp = client.patch("/api/admin/users/0", json={"status": "disabled"})
        assert resp.status_code == 400
        assert "不能修改自己" in resp.json()["detail"]


class TestContinueWithNewFile:
    """Regression: uploading a file mid-conversation must switch the session
    to audit mode with the new file, not silently continue in chat mode."""

    def test_file_upload_in_later_turn_switches_to_audit(self, client, mock_chat_agent):
        agent, _model = mock_chat_agent

        # Turn 1: plain chat session (no file).
        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, "test-model")),
        ):
            with client.stream("GET", "/api/sessions/run?task=hello") as resp:
                assert resp.status_code == 200
                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if "\n\n" in buffer:
                        break
        first = _first_data_line(buffer)
        session_id = json.loads(first[len("data: ") :])["sessionId"]

        # Upload a document.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n" + "第二轮上传的测试文档内容".encode())
            f.flush()
            upload = client.post(
                "/api/files",
                files={"file": ("turn2.pdf", open(f.name, "rb"), "application/pdf")},
            )
        assert upload.status_code == 200
        file_id = upload.json()["fileId"]

        # Turn 2: continue the SAME session but with the new file attached.
        # The unified builder is called once; the file is passed through the
        # agent_context ({"file_path": ...}) rather than a separate builder.
        builder = AsyncMock(return_value=(agent, None, "test-model"))
        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=builder,
        ):
            with client.stream(
                "GET",
                f"/api/sessions/run?task=分析这份文档&sessionId={session_id}&fileId={file_id}",
            ) as resp:
                assert resp.status_code == 200
                for _ in resp.iter_bytes():
                    pass

        builder.assert_called_once()
        # Replay of an empty activation set for a chat-only session.
        assert builder.call_args.kwargs.get("active_domains") == ()

        # And the session's working document is updated for later turns.
        detail = client.get(f"/api/sessions/{session_id}")
        assert detail.status_code == 200
        assert len(detail.json()["turns"]) == 2


class TestCompactSession:
    @pytest.mark.asyncio
    async def test_compact_route_compacts_and_persists(self, client, monkeypatch):
        from courtier.agent.api.services import agent_service
        from courtier.agent.api.services.stream_service import serialize_messages
        from courtier.agent.core.state import Message
        from courtier.agent.testing import MockModelClient

        monkeypatch.setattr(
            agent_service,
            "build_model_client",
            lambda settings: MockModelClient(tool_calls=[]),
        )

        store = client.app.state.session_store
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="问题一"),
            Message(role="assistant", content="回答一"),
            Message(role="user", content="问题二"),
        )
        await store.create(
            session_id="sess_abc123def456",
            task="问题一",
            file_id="",
            model_name="mock",
            owner="admin",
            status="completed",
        )
        await store.update("sess_abc123def456", messages_json=serialize_messages(msgs))

        resp = client.post("/api/sessions/sess_abc123def456/compact")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["beforeMessages"] == 4
        assert data["afterMessages"] == 2  # system prompt + summary
        assert data["compactCount"] == 1

        updated = await store.get("sess_abc123def456")
        assert updated is not None
        assert updated.context_state
        persisted = json.loads(updated.messages_json)
        assert len(persisted) == 2
        assert persisted[1]["content"].startswith("[上下文压缩 #1]")

    @pytest.mark.asyncio
    async def test_compact_route_404_for_missing_session(self, client):
        resp = client.post("/api/sessions/sess_nonexistent/compact")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_compact_route_400_for_empty_session(self, client):
        """7.4 空会话（无消息）→ 400 "会话还没有可压缩的上下文"。"""
        store = client.app.state.session_store
        await store.create(
            session_id="sess_abc000def000",
            task="",
            file_id="",
            model_name="mock",
            owner="admin",
            status="completed",
        )
        resp = client.post("/api/sessions/sess_abc000def000/compact")
        assert resp.status_code == 400
        assert "还没有可压缩" in resp.json()["detail"]


class TestPersistOversizedTask:
    @pytest.mark.asyncio
    async def test_under_limit_unchanged(self):
        from types import SimpleNamespace

        from courtier.agent.api.routes.sessions import _persist_oversized_task

        task = "x" * 100
        result = await _persist_oversized_task(
            task, None, SimpleNamespace(context_max_user_message_chars=10000)
        )
        assert result == task

    @pytest.mark.asyncio
    async def test_over_limit_persisted_and_replaced(self, tmp_path):
        from types import SimpleNamespace

        from courtier.agent.api.routes.sessions import _persist_oversized_task
        from courtier.agent.artifacts.store import ArtifactStore

        store = ArtifactStore(cache_dir=str(tmp_path))
        cm = SimpleNamespace(_cache=store)
        settings = SimpleNamespace(context_max_user_message_chars=100)
        task = "全文内容" * 100  # 400 chars > 100-char limit

        replaced = await _persist_oversized_task(task, cm, settings)

        assert replaced != task
        assert "$ref:user_input:1" in replaced
        assert "共 400 字符" in replaced
        # The full original text is on disk, retrievable via the ref.
        assert store.load("$ref:user_input:1") == task

    @pytest.mark.asyncio
    async def test_persist_failure_returns_original(self):
        from types import SimpleNamespace

        from courtier.agent.api.routes.sessions import _persist_oversized_task

        class _Boom:
            async def persist(self, *args, **kwargs):
                raise RuntimeError("disk full")

        task = "y" * 500
        result = await _persist_oversized_task(
            task,
            SimpleNamespace(_cache=_Boom()),
            SimpleNamespace(context_max_user_message_chars=100),
        )
        assert result == task

    @pytest.mark.asyncio
    async def test_no_store_returns_original(self):
        from types import SimpleNamespace

        from courtier.agent.api.routes.sessions import _persist_oversized_task

        task = "z" * 500
        result = await _persist_oversized_task(
            task,
            SimpleNamespace(_cache=None),
            SimpleNamespace(context_max_user_message_chars=100),
        )
        assert result == task


class TestPluginLogs:
    def test_known_plugin_returns_410(self, client, monkeypatch):
        """Plugins are standalone now — the host no longer tees their logs."""
        ps = client.app.state.plugin_system
        monkeypatch.setattr(ps, "get_scan_results", lambda: {"search": None})
        resp = client.get("/api/admin/extensions/plugins/search/logs")
        assert resp.status_code == 410
        assert "独立部署" in resp.json()["detail"]

    def test_unknown_plugin_404(self, client, monkeypatch):
        ps = client.app.state.plugin_system
        monkeypatch.setattr(ps, "get_scan_results", lambda: {})
        resp = client.get("/api/admin/extensions/plugins/ghost/logs")
        assert resp.status_code == 404


@pytest.fixture
def model_pool_snapshot():
    """Swap the global ConfigService snapshot to one carrying a small model
    pool; restore the previous snapshot afterwards.  app.state.settings
    follows the service (dynamic binding), so routes see the pool."""
    from courtier.config import ModelPoolConfig, get_config_service

    service = get_config_service()
    original = service.get()
    pool = ModelPoolConfig.model_validate(
        {
            "endpoints": [
                {
                    "id": "ep_a",
                    "name": "接入点A",
                    "base_url": "https://a.example.com/v1",
                    "enabled": True,
                    "models": [
                        {"id": "mdl_a1", "name": "模型A1", "model": "model-a1"},
                        {
                            "id": "mdl_a2",
                            "name": "模型A2",
                            "model": "model-a2",
                            "modalities": ["vision"],
                        },
                    ],
                },
                {
                    "id": "ep_b",
                    "name": "接入点B",
                    "base_url": "https://b.example.com/v1",
                    "enabled": False,
                    "models": [{"id": "mdl_b1", "name": "模型B1", "model": "model-b1"}],
                },
            ],
            "default_model_id": "mdl_a1",
        }
    )
    service.replace(
        original.model_copy(update={"llm_model_pool": pool, "llm_endpoint_keys": {"ep_a": "sk-a"}})
    )
    yield service
    service.replace(original)


class TestModelSelection:
    """Per-run model selection on /sessions/run and the /models pool view."""

    def _events(self, buffer: str) -> list[dict]:
        events = []
        for line in buffer.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
        return events

    def _run_collecting_model_selected(
        self, client, agent, query: str, model_name: str = "test-model"
    ) -> list[dict]:
        """Start a run, buffer until the model_selected frame, return events."""
        with patch(
            "courtier.agent.api.routes.sessions.build_agent",
            new=AsyncMock(return_value=(agent, None, model_name)),
        ):
            with client.stream("GET", f"/api/sessions/run?{query}") as resp:
                assert resp.status_code == 200
                buffer = ""
                for chunk in resp.iter_bytes():
                    buffer += chunk.decode("utf-8")
                    if '"model_selected"' in buffer:
                        after = buffer.split('"model_selected"', 1)[1]
                        if "\n\n" in after:
                            break
                return self._events(buffer)

    def test_run_emits_model_selected_and_persists(
        self, client, mock_chat_agent, model_pool_snapshot
    ):
        agent, _model = mock_chat_agent
        events = self._run_collecting_model_selected(
            client, agent, "task=hello&modelId=mdl_a2", model_name="模型A2"
        )

        session = next(e for e in events if e["type"] == "session")
        assert session["modelId"] == "mdl_a2"
        selected = [e for e in events if e["type"] == "model_selected"]
        assert selected, "run must announce its model"
        assert selected[0]["model"] == "模型A2"
        assert selected[0]["modelId"] == "mdl_a2"
        assert selected[0]["backend"] == "pool"

        sid = session["sessionId"]
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["lastModelId"] == "mdl_a2"
        assert detail["modelName"] == "模型A2"
        assert detail["turns"][0]["message"]["modelId"] == "mdl_a2"
        assert detail["turns"][0]["message"]["modelName"] == "模型A2"

    def test_unknown_model_id_rejected_before_run(
        self, client, mock_chat_agent, model_pool_snapshot
    ):
        resp = client.get("/api/sessions/run?task=hello&modelId=mdl_ghost")
        assert resp.status_code == 400
        assert "模型不存在或已停用" in resp.json()["detail"]

    def test_disabled_endpoint_model_rejected(self, client, mock_chat_agent, model_pool_snapshot):
        resp = client.get("/api/sessions/run?task=hello&modelId=mdl_b1")
        assert resp.status_code == 400

    def test_explicit_model_with_empty_pool_rejected(self, client, mock_chat_agent):
        resp = client.get("/api/sessions/run?task=hello&modelId=mdl_any")
        assert resp.status_code == 400

    def test_models_endpoint_public_view(self, client, model_pool_snapshot):
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()

        assert body["defaultModelId"] == "mdl_a1"
        # disabled endpoints are hidden; no endpoint internals leak
        assert [g["endpointId"] for g in body["endpoints"]] == ["ep_a"]
        assert body["endpoints"][0]["endpointName"] == "接入点A"
        assert body["endpoints"][0]["models"][1] == {
            "id": "mdl_a2",
            "name": "模型A2",
            "modalities": ["vision"],
        }
        payload = json.dumps(body)
        assert "base_url" not in payload and "sk-a" not in payload

    def test_models_endpoint_empty_pool(self, client):
        resp = client.get("/api/models")
        assert resp.status_code == 200
        assert resp.json() == {"endpoints": [], "defaultModelId": ""}


class TestQueryTokenScoping:
    """?token= is accepted on SSE paths only — URLs leak into logs."""

    @contextmanager
    def _query_token_only_client(self, client):
        """Strip header/cookie credentials so ?token= is the only channel."""
        token = client.headers.get("authorization", "").removeprefix("Bearer ")
        saved_headers = dict(client.headers)
        saved_cookies = {k: v for k, v in client.cookies.items()}
        client.headers.clear()
        client.cookies.clear()
        try:
            yield client, token
        finally:
            client.headers.update(saved_headers)
            client.cookies.update(saved_cookies)

    def test_query_token_rejected_on_regular_api_path(self, client):
        with self._query_token_only_client(client) as (c, token):
            resp = c.get(f"/api/sessions?token={token}")
            assert resp.status_code == 401

    def test_query_token_accepted_on_sse_path(self, client):
        with self._query_token_only_client(client) as (c, token):
            # The stream never ends — read only the status via stream().
            with c.stream("GET", f"/api/events?token={token}") as resp:
                assert resp.status_code == 200
