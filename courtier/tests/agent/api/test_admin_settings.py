"""Admin settings API — schema view, partial updates, audit, LLM test."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import courtier.agent.api.routes.admin_settings as admin_settings
from courtier.agent.api.routes.admin_users import require_admin
from courtier.config import ConfigService
from courtier.db.db_manager import AsyncDatabase
from courtier.settings_store import FernetCodec, SettingsStore


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    """App with the settings router, an admin bypass, and a sqlite store.

    The global ConfigService is shielded: PUT swaps snapshots on a fresh
    per-test service instead of the process-wide one."""
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.state.settings_store = SettingsStore(db, FernetCodec("ab" * 32))
    app.dependency_overrides[require_admin] = lambda: {"sub": "tester", "role": "admin"}

    local_service = ConfigService()
    local_service.get()
    monkeypatch.setattr(admin_settings, "get_config_service", lambda: local_service)
    monkeypatch.setattr(admin_settings, "get_settings", lambda: local_service.get())

    with TestClient(app) as test_client:
        yield test_client


class TestGetSettingsView:
    def test_groups_and_masking(self, client):
        resp = client.get("/api/admin/settings")
        assert resp.status_code == 200
        body = resp.json()

        assert body["mode"] == "db"
        keys = [c["key"] for c in body["categories"]]
        assert "model" in keys and "retrieval" in keys and "guards" in keys

        by_name = {
            f["name"]: f for c in body["categories"] for f in c["fields"]
        }
        # secrets are masked: set/tail only, never the plaintext contract
        assert by_name["llm_api_key"]["is_secret"] is True
        assert set(by_name["llm_api_key"]["value"]) == {"set", "tail"}
        # metadata surface
        assert by_name["es_hosts"]["effect"] == "rebuild"
        assert by_name["cors_origins"]["effect"] == "restart"
        assert by_name["es_hosts"]["env_name"] == "ES_HOSTS"

    def test_source_becomes_db_after_save(self, client):
        client.put("/api/admin/settings/model", json={"llm_model": "db-model"})
        body = client.get("/api/admin/settings").json()
        by_name = {f["name"]: f for c in body["categories"] for f in c["fields"]}
        assert by_name["llm_model"]["source"] == "db"
        assert by_name["llm_model"]["value"] == "db-model"

    def test_env_only_mode_when_no_store(self, db, monkeypatch):
        app = FastAPI()
        app.include_router(admin_settings.router)
        app.state.settings_store = None
        app.dependency_overrides[require_admin] = lambda: {"sub": "t", "role": "admin"}
        with TestClient(app) as c:
            resp = c.get("/api/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "env"


class TestUpdateCategory:
    def test_set_and_clear(self, client, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "fixed-secret")
        resp = client.put(
            "/api/admin/settings/model", json={"llm_model": "db-model", "llm_temperature": 0.3}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] == ["llm_model", "llm_temperature"]
        assert body["restart_required"] == []

        cleared = client.put("/api/admin/settings/model", json={"llm_temperature": None})
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] == ["llm_temperature"]

    def test_restart_effect_reported(self, client):
        resp = client.put(
            "/api/admin/settings/web", json={"cors_origins": ["http://x:5173"]}
        )
        body = resp.json()
        assert body["restart_required"] == ["cors_origins"]

    def test_unknown_field_rejected(self, client):
        resp = client.put("/api/admin/settings/model", json={"not_a_field": 1})
        assert resp.status_code == 422

    def test_wrong_category_404(self, client):
        resp = client.put("/api/admin/settings/nope", json={"llm_model": "x"})
        assert resp.status_code == 404

    def test_validation_error_422(self, client):
        resp = client.put(
            "/api/admin/settings/model", json={"llm_extra_body": "not-json{"}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["errors"]

    def test_env_only_503(self, monkeypatch):
        app = FastAPI()
        app.include_router(admin_settings.router)
        app.state.settings_store = None
        app.dependency_overrides[require_admin] = lambda: {"sub": "t", "role": "admin"}
        with TestClient(app) as c:
            resp = c.put("/api/admin/settings/model", json={"llm_model": "x"})
        assert resp.status_code == 503


class TestAuditEndpoint:
    def test_changes_listed(self, client):
        client.put("/api/admin/settings/guards", json={"max_total_runs": 5})
        resp = client.get("/api/admin/settings/audit")
        assert resp.status_code == 200
        changes = resp.json()["changes"]
        assert changes and changes[0]["key"] == "max_total_runs"
        assert changes[0]["actor"] == "tester"


class TestLlmConnectivity:
    def test_success_and_overrides(self, client, monkeypatch):
        captured = {}

        class _FakeBackend:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def chat(self, request):
                return SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                    latency_ms=123.0,
                )

            async def close(self):
                pass

        # The handler imports the backend lazily — patch the source module.
        import courtier.agent.core.backends.openai_backend as backend_mod

        monkeypatch.setattr(backend_mod, "OpenAIModelBackend", _FakeBackend)

        resp = client.post("/api/admin/settings/test/llm", json={"llm_model": "candidate"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model"] == "candidate"
        assert captured["model"] == "candidate"

    def test_failure_returns_ok_false(self, client, monkeypatch):
        import courtier.agent.core.backends.openai_backend as backend_mod

        class _Boom:
            def __init__(self, **kwargs):
                pass

            async def chat(self, request):
                raise RuntimeError("endpoint down")

            async def close(self):
                pass

        monkeypatch.setattr(backend_mod, "OpenAIModelBackend", _Boom)
        resp = client.post("/api/admin/settings/test/llm", json={})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "endpoint down" in resp.json()["error"]


class TestDynamicSettingsBinding:
    """app.state.settings must follow ConfigService snapshot replacement.

    Regression: after the env→DB jwt_secret migration, a stale factory-time
    reference kept the empty env value and login failed with
    InvalidKeyError ('HMAC key must not be empty')."""

    def test_state_settings_rebound_on_replace(self):
        from fastapi import FastAPI

        from courtier.agent.api.app import _bind_dynamic_settings
        from courtier.config import ConfigService

        app = FastAPI()
        service = ConfigService()
        stale = service.get()
        app.state.settings = stale  # factory-time reference, as in create_app

        unsubscribe = _bind_dynamic_settings(app, service=service)
        new_snapshot = stale.model_copy(update={"jwt_secret": "db-secret"})
        service.replace(new_snapshot)

        assert app.state.settings is new_snapshot
        assert app.state.settings.jwt_secret == "db-secret"

        unsubscribe()
        newer = new_snapshot.model_copy(update={"jwt_secret": "rotated"})
        service.replace(newer)
        assert app.state.settings is new_snapshot  # detached after unsubscribe
