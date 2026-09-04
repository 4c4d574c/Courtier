"""Refresh-token lifecycle: rotation, one-time use, reuse detection, logout.

Covers the security-critical path that had zero route-level coverage:
login (DB mode) issues the httpOnly refresh cookie; /api/auth/refresh
rotates it; replaying an old token must revoke the whole family.
"""

import tempfile

import pytest
from fastapi.testclient import TestClient

import courtier.agent.api.db as db_module
from courtier.agent.api.app import create_app
from courtier.agent.api.middleware.auth import hash_password
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables.user import UserRole, UserStatus, UserTable

_PASSWORD = "right-password-1"


async def _mk_user(db: AsyncDatabase) -> None:
    async with db.session() as session:
        session.add(
            UserTable(
                username="refreshuser",
                password_hash=hash_password(_PASSWORD),
                role=UserRole.auditor,
                status=UserStatus.active,
            )
        )


@pytest.fixture
async def env(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite+aiosqlite:///{d}/refresh.db"
        db = AsyncDatabase(url)
        await db.create_all()
        await _mk_user(db)
        monkeypatch.setattr(db_module, "_db", db)
        app = create_app(sessions_dir=d, start_plugins=False)
        # Copy, never mutate: app.state.settings is the process-wide
        # Settings singleton (see test_auth_lockout.py).
        app.state.settings = app.state.settings.model_copy(update={"mysql_url": url})
        with TestClient(app) as client:
            yield client, db
        await db.drop_all(testing=True)
        await db.engine.dispose()


def _login(client: TestClient):
    return client.post(
        "/api/auth/login", json={"username": "refreshuser", "password": _PASSWORD}
    )


def _refresh(client: TestClient):
    return client.post("/api/auth/refresh")


@pytest.mark.asyncio
async def test_login_issues_refresh_cookie_and_refresh_rotates(env):
    client, _db = env
    resp = _login(client)
    assert resp.status_code == 200
    old_refresh = client.cookies.get("refresh_token")
    assert old_refresh
    assert client.cookies.get("access_token")

    refreshed = _refresh(client)
    assert refreshed.status_code == 200
    assert refreshed.json()["user"]["username"] == "refreshuser"
    new_refresh = client.cookies.get("refresh_token")
    assert new_refresh and new_refresh != old_refresh


@pytest.mark.asyncio
async def test_refresh_token_is_one_time_use(env):
    client, _db = env
    _login(client)
    old_refresh = client.cookies.get("refresh_token")
    assert _refresh(client).status_code == 200

    # Replay the old token: rejected …
    client.cookies.set("refresh_token", old_refresh)
    assert _refresh(client).status_code == 401


@pytest.mark.asyncio
async def test_reuse_revokes_the_whole_family(env):
    client, db = env
    _login(client)
    old_refresh = client.cookies.get("refresh_token")
    assert _refresh(client).status_code == 200
    new_refresh = client.cookies.get("refresh_token")

    # Reuse of the old token trips reuse detection: the family (including
    # the token the attacker does not hold — the current one) is revoked.
    client.cookies.set("refresh_token", old_refresh)
    assert _refresh(client).status_code == 401

    client.cookies.set("refresh_token", new_refresh)
    assert _refresh(client).status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(env):
    client, _db = env
    _login(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.cookies.get("refresh_token") is None

    # Even with the raw value replayed, the server-side row is revoked.
    assert _refresh(client).status_code == 401


@pytest.mark.asyncio
async def test_refresh_without_cookie_is_401(env):
    client, _db = env
    client.cookies.clear()
    assert _refresh(client).status_code == 401
