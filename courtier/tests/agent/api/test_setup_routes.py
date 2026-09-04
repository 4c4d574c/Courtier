"""First-run setup wizard routes: status, one-shot admin creation, and the
production network/key gate."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import courtier.agent.api.db as db_module
from courtier.agent.api.routes.setup import router as setup_router
from courtier.db.db_manager import AsyncDatabase


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


def _app(db, *, deployment="development", monkeypatch):
    app = FastAPI()
    app.include_router(setup_router)
    app.state.settings_store = object()  # DB mode
    app.state.settings = SimpleNamespace(deployment_env=deployment)
    # The setup routes and admin_exists() resolve the engine through
    # db_module.get_db(); point the module singleton at the sqlite db.
    monkeypatch.setattr(db_module, "_db", db)
    return app


class TestSetupStatus:
    def test_db_mode_without_admin_requires_setup(self, db, monkeypatch):
        with TestClient(_app(db, monkeypatch=monkeypatch)) as client:
            resp = client.get("/api/setup/status")
        assert resp.status_code == 200
        assert resp.json() == {"setup_required": True}

    def test_env_only_mode_never_requires_setup(self, monkeypatch):
        app = FastAPI()
        app.include_router(setup_router)
        app.state.settings_store = None
        app.state.settings = SimpleNamespace(deployment_env="development")
        with TestClient(app) as client:
            resp = client.get("/api/setup/status")
        assert resp.json() == {"setup_required": False}


class TestCreateAdmin:
    def test_creates_first_admin_once(self, db, monkeypatch):
        app = _app(db, monkeypatch=monkeypatch)
        with TestClient(app) as client:
            first = client.post(
                "/api/setup/admin",
                json={"username": "admin", "password": "super-secret-1"},
            )
            assert first.status_code == 200
            assert first.json()["ok"] is True
            # The gate flag flipped: the production API surface opens.
            assert app.state._has_admin is True

            second = client.post(
                "/api/setup/admin",
                json={"username": "other", "password": "super-secret-2"},
            )
            assert second.status_code == 409

    def test_status_false_after_creation(self, db, monkeypatch):
        app = _app(db, monkeypatch=monkeypatch)
        with TestClient(app) as client:
            client.post(
                "/api/setup/admin", json={"username": "admin", "password": "super-secret-1"}
            )
            assert client.get("/api/setup/status").json() == {"setup_required": False}

    def test_invalid_username_422(self, db, monkeypatch):
        with TestClient(_app(db, monkeypatch=monkeypatch)) as client:
            resp = client.post(
                "/api/setup/admin", json={"username": "bad name!", "password": "super-secret-1"}
            )
        assert resp.status_code == 422

    def test_env_only_409(self, monkeypatch):
        app = FastAPI()
        app.include_router(setup_router)
        app.state.settings_store = None
        app.state.settings = SimpleNamespace(deployment_env="development")
        with TestClient(app) as client:
            resp = client.post(
                "/api/setup/admin", json={"username": "admin", "password": "super-secret-1"}
            )
        assert resp.status_code == 409

    def test_production_gate_requires_key_from_public_source(
        self, db, monkeypatch
    ):
        # TestClient requests come from the non-IP "testclient" host →
        # not a private network → the key is required in production.
        app = _app(db, deployment="production", monkeypatch=monkeypatch)
        with TestClient(app) as client:
            blocked = client.post(
                "/api/setup/admin", json={"username": "admin", "password": "super-secret-1"}
            )
            assert blocked.status_code == 403

            monkeypatch.setenv("COURTIER_SETUP_KEY", "one-time-key")
            allowed = client.post(
                "/api/setup/admin",
                json={
                    "username": "admin",
                    "password": "super-secret-1",
                    "setup_key": "one-time-key",
                },
            )
            assert allowed.status_code == 200

    def test_development_has_no_key_gate(self, db, monkeypatch):
        monkeypatch.delenv("COURTIER_SETUP_KEY", raising=False)
        with TestClient(_app(db, deployment="development", monkeypatch=monkeypatch)) as client:
            resp = client.post(
                "/api/setup/admin", json={"username": "admin", "password": "super-secret-1"}
            )
        assert resp.status_code == 200

    async def test_created_admin_can_authenticate(self, db, monkeypatch):
        """The wizard user lands in the users table with a real bcrypt hash."""
        from sqlalchemy import select

        from courtier.db.tables.user import UserRole, UserTable

        with TestClient(_app(db, monkeypatch=monkeypatch)) as client:
            client.post(
                "/api/setup/admin", json={"username": "wizard", "password": "super-secret-1"}
            )
        async with db.session() as session:
            user = (
                await session.execute(select(UserTable).where(UserTable.username == "wizard"))
            ).scalar_one()
        assert user.role == UserRole.admin
        assert user.password_hash != "super-secret-1"
        assert len(user.password_hash) >= 20

    def test_production_configured_key_required_even_from_private_source(
        self, db, monkeypatch
    ):
        """A configured COURTIER_SETUP_KEY is authoritative: behind a reverse
        proxy every client appears as a private-network source, so the source
        check must never bypass the key."""
        from courtier.agent.api.routes import setup as setup_routes

        app = _app(db, deployment="production", monkeypatch=monkeypatch)
        monkeypatch.setattr(setup_routes, "_from_private_network", lambda request: True)
        monkeypatch.setenv("COURTIER_SETUP_KEY", "one-time-key")
        with TestClient(app) as client:
            no_key = client.post(
                "/api/setup/admin",
                json={"username": "admin", "password": "super-secret-1"},
            )
            assert no_key.status_code == 403

            wrong_key = client.post(
                "/api/setup/admin",
                json={
                    "username": "admin",
                    "password": "super-secret-1",
                    "setup_key": "wrong",
                },
            )
            assert wrong_key.status_code == 403

            good = client.post(
                "/api/setup/admin",
                json={
                    "username": "admin",
                    "password": "super-secret-1",
                    "setup_key": "one-time-key",
                },
            )
            assert good.status_code == 200
