"""PATCH /api/profile password change — wrong/missing current password,
successful rotation, and re-login with the new password."""

import tempfile

import pytest
from fastapi.testclient import TestClient

import courtier.agent.api.db as db_module
from courtier.agent.api.app import create_app
from courtier.agent.api.middleware.auth import hash_password
from courtier.agent.api.rate_limiter import limiter
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables.user import UserRole, UserStatus, UserTable

_PASSWORD = "old-password-1"
_NEW_PASSWORD = "new-password-9"


async def _mk_user(db: AsyncDatabase) -> None:
    async with db.session() as session:
        session.add(
            UserTable(
                username="profileuser",
                password_hash=hash_password(_PASSWORD),
                email="old@example.com",
                role=UserRole.auditor,
                status=UserStatus.active,
            )
        )


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def env(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite+aiosqlite:///{d}/profile.db"
        db = AsyncDatabase(url)
        await db.create_all()
        await _mk_user(db)
        monkeypatch.setattr(db_module, "_db", db)
        app = create_app(sessions_dir=d, start_plugins=False)
        app.state.settings = app.state.settings.model_copy(update={"mysql_url": url})
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "profileuser", "password": _PASSWORD},
            )
            assert login.status_code == 200
            client.headers["Authorization"] = f"Bearer {login.json()['token']}"
            yield client
        await db.drop_all(testing=True)
        await db.engine.dispose()


def _patch(client: TestClient, **payload):
    return client.patch("/api/profile", json=payload)


def test_wrong_current_password_400(env):
    resp = _patch(
        env, current_password="wrong-password", new_password=_NEW_PASSWORD
    )
    assert resp.status_code == 400
    assert "当前密码错误" in resp.json()["detail"]


def test_missing_current_password_400(env):
    resp = _patch(env, new_password=_NEW_PASSWORD)
    assert resp.status_code == 400
    assert "当前密码" in resp.json()["detail"]


def test_no_fields_400(env):
    assert _patch(env).status_code == 400


def test_successful_password_change_and_relogin(env):
    resp = _patch(env, current_password=_PASSWORD, new_password=_NEW_PASSWORD)
    assert resp.status_code == 200

    # The old password must stop working; the new one must work.
    client = env
    client.headers.pop("Authorization")
    old_login = client.post(
        "/api/auth/login", json={"username": "profileuser", "password": _PASSWORD}
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/auth/login", json={"username": "profileuser", "password": _NEW_PASSWORD}
    )
    assert new_login.status_code == 200
