"""Admin settings API — schema view, partial updates, audit, LLM test."""

import json
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

        by_name = {f["name"]: f for c in body["categories"] for f in c["fields"]}
        # secrets are masked: set/tail only, never the plaintext contract
        assert by_name["llm_api_key"]["is_secret"] is True
        assert set(by_name["llm_api_key"]["value"]) == {"set", "tail"}
        # metadata surface
        assert by_name["es_hosts"]["effect"] == "rebuild"
        assert by_name["cors_origins"]["effect"] == "restart"
        assert by_name["es_hosts"]["env_name"] == "ES_HOSTS"

    def test_guardrail_layer_modes_enum_surface(self, client):
        """五层模式以 enum 形式暴露；tool_call 只给 block/log 两档。"""
        resp = client.get("/api/admin/settings")
        assert resp.status_code == 200
        by_name = {
            f["name"]: f for c in resp.json()["categories"] for f in c["fields"]
        }
        for name in (
            "guardrail_input_layer",
            "guardrail_output_layer",
            "guardrail_tool_layer",
            "guardrail_tool_call_layer",
            "guardrail_post_tool_layer",
        ):
            assert by_name[name]["type"] == "enum", name
        assert by_name["guardrail_tool_call_layer"]["choices"] == ["block", "log"]
        assert by_name["guardrail_output_layer"]["choices"] == [
            "allow",
            "log",
            "block",
            "off",
        ]

    def test_tool_call_layer_off_rejected_log_accepted(self, client):
        rejected = client.put(
            "/api/admin/settings/guards",
            json={"guardrail_tool_call_layer": "off"},
        )
        assert rejected.status_code == 422
        accepted = client.put(
            "/api/admin/settings/guards",
            json={"guardrail_tool_call_layer": "log"},
        )
        assert accepted.status_code == 200

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
        resp = client.put("/api/admin/settings/web", json={"cors_origins": ["http://x:5173"]})
        body = resp.json()
        assert body["restart_required"] == ["cors_origins"]

    def test_unknown_field_rejected(self, client):
        resp = client.put("/api/admin/settings/model", json={"not_a_field": 1})
        assert resp.status_code == 422

    def test_wrong_category_404(self, client):
        resp = client.put("/api/admin/settings/nope", json={"llm_model": "x"})
        assert resp.status_code == 404

    def test_validation_error_422(self, client):
        resp = client.put("/api/admin/settings/model", json={"llm_extra_body": "not-json{"})
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
        # The scalar→pool migration may interleave its own audit row
        # (actor system-migrate); assert on the admin's row specifically.
        entry = next(c for c in changes if c["key"] == "max_total_runs")
        assert entry["actor"] == "tester"


class TestModelPoolSettings:
    def _pool(self):
        return {
            "endpoints": [
                {
                    "id": "ep_main",
                    "name": "主接入点",
                    "base_url": "https://pool.example.com/v1",
                    "enabled": True,
                    "models": [{"id": "mdl_main", "name": "池模型", "model": "pool-model"}],
                }
            ],
            "default_model_id": "mdl_main",
        }

    def _fields_by_name(self, body):
        return {f["name"]: f for c in body["categories"] for f in c["fields"]}

    def test_pool_roundtrip_and_per_entry_masking(self, client):
        resp = client.put(
            "/api/admin/settings/model",
            json={
                "llm_model_pool": self._pool(),
                "llm_endpoint_keys": {"ep_main": "sk-secret-1"},
            },
        )
        assert resp.status_code == 200
        assert set(resp.json()["applied"]) == {"llm_model_pool", "llm_endpoint_keys"}

        body = client.get("/api/admin/settings").json()
        fields = self._fields_by_name(body)
        assert fields["llm_model_pool"]["value"]["default_model_id"] == "mdl_main"
        assert fields["llm_model_pool"]["value"]["endpoints"][0]["base_url"] == (
            "https://pool.example.com/v1"
        )
        # keys are masked per entry, never plaintext anywhere in the view
        assert fields["llm_endpoint_keys"]["value"] == {"ep_main": {"set": True, "tail": "et-1"}}
        assert "sk-secret-1" not in json.dumps(body)

    def test_endpoint_keys_entry_level_merge(self, client):
        client.put("/api/admin/settings/model", json={"llm_endpoint_keys": {"ep_a": "sk-aaaa"}})
        body = client.get("/api/admin/settings").json()
        assert self._fields_by_name(body)["llm_endpoint_keys"]["value"] == {
            "ep_a": {"set": True, "tail": "aaaa"}
        }

        # absent entries survive, new entries land, null deletes
        client.put(
            "/api/admin/settings/model",
            json={"llm_endpoint_keys": {"ep_b": "sk-bbbb", "ep_a": "sk-a2a2", "ep_zzz": None}},
        )
        body = client.get("/api/admin/settings").json()
        assert self._fields_by_name(body)["llm_endpoint_keys"]["value"] == {
            "ep_a": {"set": True, "tail": "a2a2"},
            "ep_b": {"set": True, "tail": "bbbb"},
        }

        # whole-field null = clear back to default (empty)
        cleared = client.put(
            "/api/admin/settings/model", json={"llm_endpoint_keys": {"ep_a": None, "ep_b": None}}
        )
        assert cleared.status_code == 200
        body = client.get("/api/admin/settings").json()
        assert self._fields_by_name(body)["llm_endpoint_keys"]["value"] == {}

    def test_endpoint_keys_empty_dict_is_noop(self, client):
        client.put("/api/admin/settings/model", json={"llm_endpoint_keys": {"ep_a": "sk-aaaa"}})
        resp = client.put("/api/admin/settings/model", json={"llm_endpoint_keys": {}})
        assert resp.status_code == 200
        assert "llm_endpoint_keys" not in resp.json()["applied"]

    def test_endpoint_keys_non_object_rejected(self, client):
        resp = client.put("/api/admin/settings/model", json={"llm_endpoint_keys": "nope"})
        assert resp.status_code == 422

    def test_pool_validation_error_422(self, client):
        bad = self._pool()
        bad["default_model_id"] = "mdl_ghost"
        resp = client.put("/api/admin/settings/model", json={"llm_model_pool": bad})
        assert resp.status_code == 422
        assert resp.json()["detail"]["errors"]

    def test_llm_test_uses_pool_entries(self, client, monkeypatch):
        captured = {}

        class _FakeBackend:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def chat(self, request):
                return SimpleNamespace(message=SimpleNamespace(content="ok"), latency_ms=1.0)

            async def close(self):
                pass

        import courtier.agent.core.backends.openai_backend as backend_mod

        monkeypatch.setattr(backend_mod, "OpenAIModelBackend", _FakeBackend)

        client.put(
            "/api/admin/settings/model",
            json={
                "llm_model_pool": self._pool(),
                "llm_endpoint_keys": {"ep_main": "sk-pool"},
            },
        )

        resp = client.post(
            "/api/admin/settings/test/llm",
            json={"endpointId": "ep_main", "modelId": "mdl_main"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert captured["base_url"] == "https://pool.example.com/v1"
        assert captured["api_key"] == "sk-pool"
        assert captured["model"] == "pool-model"

    def test_llm_test_unknown_pool_entries(self, client, monkeypatch):
        import courtier.agent.core.backends.openai_backend as backend_mod

        class _Noop:
            def __init__(self, **kwargs):
                pass

            async def chat(self, request):  # pragma: no cover - never reached
                raise AssertionError("backend must not be called")

            async def close(self):
                pass

        monkeypatch.setattr(backend_mod, "OpenAIModelBackend", _Noop)
        client.put("/api/admin/settings/model", json={"llm_model_pool": self._pool()})

        missing_ep = client.post("/api/admin/settings/test/llm", json={"endpointId": "ep_x"})
        assert missing_ep.json()["ok"] is False
        assert "接入点不存在" in missing_ep.json()["error"]

        missing_mdl = client.post(
            "/api/admin/settings/test/llm",
            json={"endpointId": "ep_main", "modelId": "mdl_x"},
        )
        assert missing_mdl.json()["ok"] is False
        assert "模型不存在" in missing_mdl.json()["error"]


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


class TestPoolBakeOnSave:
    """保存模型池时，服务端把留空的高级字段物化为当前全局默认值。"""

    def test_put_bakes_unset_advanced_fields(self, client, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "bake-model")
        pool = {
            "endpoints": [
                {
                    "id": "ep_bake",
                    "name": "bake",
                    "base_url": "https://bake/v1",
                    "enabled": True,
                    "models": [
                        {
                            "id": "mdl_bake",
                            "name": "B",
                            "model": "bake-model",
                            "temperature": 0.9,  # 显式值保留
                            # 其余缺省 → 物化
                        }
                    ],
                }
            ],
            "default_model_id": "mdl_bake",
        }
        resp = client.put("/api/admin/settings/model", json={"llm_model_pool": pool})
        assert resp.status_code == 200

        body = client.get("/api/admin/settings").json()
        fields = {f["name"]: f for c in body["categories"] for f in c["fields"]}
        saved = fields["llm_model_pool"]["value"]
        assert saved["advanced_defaults_materialized"] is True
        model = saved["endpoints"][0]["models"][0]
        assert model["temperature"] == 0.9
        assert model["timeout_seconds"] == 180.0  # 标量全局默认
        assert model["max_tokens"] == 4096
        assert model["frequency_penalty"] == 0.0
