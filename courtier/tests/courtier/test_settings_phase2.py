"""Phase 2: connection probing, pre-save validation, JWT rotation,
and the production setup gate."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import courtier.agent.api.routes.admin_settings as admin_settings
from courtier.agent.api.routes.admin_users import require_admin
from courtier.agent.api.setup_gate import install_setup_gate
from courtier.config import ConfigService, Settings
from courtier.db.db_manager import AsyncDatabase
from courtier.settings_store import FernetCodec, SettingsStore, probe_connections


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'phase2.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


def _app_with_store(db, monkeypatch, codec=None):
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.state.settings_store = SettingsStore(db, codec)
    app.dependency_overrides[require_admin] = lambda: {"sub": "tester", "role": "admin"}
    local_service = ConfigService()
    local_service.get()
    monkeypatch.setattr(admin_settings, "get_config_service", lambda: local_service)
    monkeypatch.setattr(admin_settings, "get_settings", lambda: local_service.get())
    return app, local_service


class TestProbeConnections:
    async def test_es_probe_healthy_and_failing(self, monkeypatch):
        import elasticsearch as es_mod

        class _Healthy:
            def ping(self):
                return True

            def close(self):
                pass

        class _Sick:
            def ping(self):
                return False

            def close(self):
                pass

        settings = Settings(_env_file=None, es_hosts="http://es:9200")
        monkeypatch.setattr(es_mod, "Elasticsearch", lambda *a, **k: _Healthy())
        assert await probe_connections(settings, {"es"}) == {}
        monkeypatch.setattr(es_mod, "Elasticsearch", lambda *a, **k: _Sick())
        errors = await probe_connections(settings, {"es"})
        assert "ping" in errors["es"]

    async def test_minio_probe_failure(self, monkeypatch):
        import minio as minio_mod

        def boom(*a, **k):
            raise RuntimeError("minio down")

        monkeypatch.setattr(minio_mod, "Minio", boom)
        settings = Settings(_env_file=None, minio_endpoint="minio:9000")
        errors = await probe_connections(settings, {"minio"})
        assert "minio down" in errors["minio"]

    async def test_plugin_dial(self):
        # endpoints unset → nothing to dial, no error
        settings = Settings(_env_file=None)
        assert await probe_connections(settings, {"plugins"}) == {}

    async def test_bad_endpoints_format_reported(self):
        settings = Settings(_env_file=None, courtier_plugin_endpoints="garbage")
        errors = await probe_connections(settings, {"plugins"})
        assert "格式错误" in errors["plugins"]


class TestPreSaveProbe:
    async def test_failing_probe_blocks_save(self, db, monkeypatch):
        app, _ = _app_with_store(db, monkeypatch, codec=FernetCodec("ab" * 32))

        async def failing_probe(settings, targets):
            return {"es": "ES ping 失败"}

        monkeypatch.setattr(admin_settings, "probe_connections", failing_probe)
        with TestClient(app) as client:
            resp = client.put(
                "/api/admin/settings/retrieval", json={"es_hosts": "http://bad:9200"}
            )
        assert resp.status_code == 422
        assert "未保存" in resp.json()["detail"]["message"]
        # Nothing was persisted.
        store = SettingsStore(db)
        overrides, _ = await store.load_overrides()
        assert overrides == {}

    async def test_successful_save_invalidates_clients(
        self, db, monkeypatch
    ):
        app, _ = _app_with_store(db, monkeypatch, codec=FernetCodec("ab" * 32))

        calls = []
        async def ok_probe(_settings, _targets):
            return {}

        monkeypatch.setattr(admin_settings, "probe_connections", ok_probe)
        import courtier.es.client as es_client_mod
        import courtier.storage.client as storage_mod

        monkeypatch.setattr(
            es_client_mod, "invalidate_es_client", lambda: calls.append("es")
        )
        monkeypatch.setattr(
            storage_mod, "invalidate_minio_client", lambda: calls.append("minio")
        )

        with TestClient(app) as client:
            resp = client.put(
                "/api/admin/settings/retrieval", json={"es_hosts": "http://es:9200"}
            )
        assert resp.status_code == 200
        assert resp.json()["revalidated"] == ["es"]
        assert calls == ["es"]  # only the touched target invalidates

    async def test_plugin_change_reloads_connections(self, db, monkeypatch):
        app, _ = _app_with_store(db, monkeypatch, codec=FernetCodec("ab" * 32))
        async def ok_probe(_settings, _targets):
            return {}

        monkeypatch.setattr(admin_settings, "probe_connections", ok_probe)

        reloaded = []

        class _FakeManager:
            def _resolve_connection_config(self):
                reloaded.append("resolve")

            def get_processes(self):
                return {}

            async def restart_plugin(self, name):
                reloaded.append(f"restart:{name}")

        app.state.plugin_system = SimpleNamespace(_manager=_FakeManager())
        with TestClient(app) as client:
            resp = client.put(
                "/api/admin/settings/plugins",
                json={"courtier_plugin_token": "new-shared-token"},
            )
        assert resp.status_code == 200
        assert reloaded == ["resolve"]


class TestConnectionEndpoints:
    def test_es_minio_plugins_endpoints(self, db, monkeypatch):
        app, _ = _app_with_store(db, monkeypatch)

        async def fake_probe(settings, targets):
            assert targets == {"es"} or "es" in targets
            return {"es": "boom"} if "es" in targets else {}

        monkeypatch.setattr(admin_settings, "probe_connections", fake_probe)
        with TestClient(app) as client:
            es = client.post("/api/admin/settings/test/es", json={})
            assert es.status_code == 200
            assert es.json() == {"ok": False, "errors": {"es": "boom"}}

    def test_unknown_target_404(self, db, monkeypatch):
        app, _ = _app_with_store(db, monkeypatch)
        with TestClient(app) as client:
            resp = client.post("/api/admin/settings/test/redis", json={})
        assert resp.status_code == 404


class TestJwtRotate:
    def test_rotate_success(self, db, monkeypatch):
        app, service = _app_with_store(db, monkeypatch, codec=FernetCodec("ab" * 32))
        old_secret = service.get().jwt_secret or ""
        with TestClient(app) as client:
            resp = client.post("/api/admin/settings/jwt/rotate")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert service.get().jwt_secret
        assert service.get().jwt_secret != old_secret or old_secret == ""

    def test_rotate_without_key_422(self, db, monkeypatch):
        app, _ = _app_with_store(db, monkeypatch, codec=None)
        with TestClient(app) as client:
            resp = client.post("/api/admin/settings/jwt/rotate")
        assert resp.status_code == 422

    def test_rotate_env_only_503(self, monkeypatch):
        app = FastAPI()
        app.include_router(admin_settings.router)
        app.state.settings_store = None
        app.dependency_overrides[require_admin] = lambda: {"sub": "t", "role": "admin"}
        with TestClient(app) as client:
            resp = client.post("/api/admin/settings/jwt/rotate")
        assert resp.status_code == 503


def _gate_app(deployment: str, has_admin, mysql_url="mysql://x"):
    from types import SimpleNamespace

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/auth/login")
    async def login():
        return {"ok": True}

    @app.get("/api/setup/status")
    async def setup_status():
        return {"ok": True}

    @app.get("/admin")
    async def static_page():
        return {"page": True}

    settings = SimpleNamespace(deployment_env=deployment, mysql_url=mysql_url)
    install_setup_gate(app, settings)
    if has_admin is not None:
        app.state._has_admin = has_admin
    return app


class TestSetupGate:
    def test_production_blocks_api_until_admin_exists(self):
        with TestClient(_gate_app("production", has_admin=False)) as client:
            assert client.get("/api/auth/login").status_code == 403
            assert client.get("/api/auth/login").json()["detail"] == "setup_required"

    def test_gate_allows_setup_health_and_static(self):
        with TestClient(_gate_app("production", has_admin=False)) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/api/setup/status").status_code == 200
            assert client.get("/admin").status_code == 200

    def test_gate_opens_with_admin(self):
        with TestClient(_gate_app("production", has_admin=True)) as client:
            assert client.get("/api/auth/login").status_code == 200

    def test_development_never_gated(self):
        with TestClient(_gate_app("development", has_admin=False)) as client:
            assert client.get("/api/auth/login").status_code == 200

    def test_env_only_not_gated(self):
        with TestClient(_gate_app("production", has_admin=None, mysql_url="")) as client:
            assert client.get("/api/auth/login").status_code == 200
