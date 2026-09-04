"""Regression: a locked account must answer 403, not 500.

The ``locked_until`` column is a naive ``DateTime`` and drivers return
naive UTC; comparing it directly against the aware clock used to raise
``TypeError`` on every login attempt of a locked user.
"""

import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import courtier.agent.api.db as db_module
from courtier.agent.api.app import create_app
from courtier.agent.api.middleware.auth import hash_password
from courtier.agent.api.rate_limiter import limiter
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables.user import UserRole, UserStatus, UserTable

_PASSWORD = "right-password-1"


async def _mk_user(db: AsyncDatabase, *, locked_until: datetime | None) -> None:
    async with db.session() as session:
        session.add(
            UserTable(
                username="lockeduser",
                password_hash=hash_password(_PASSWORD),
                role=UserRole.auditor,
                status=UserStatus.active,
                locked_until=locked_until,
            )
        )


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def db_client(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDatabase(f"sqlite+aiosqlite:///{d}/auth.db")
        await db.create_all()
        monkeypatch.setattr(db_module, "_db", db)
        app = create_app(sessions_dir=d, start_plugins=False)
        # Copy, never mutate: app.state.settings is the process-wide Settings
        # singleton and other test files would inherit the sqlite URL.
        app.state.settings = app.state.settings.model_copy(
            update={"mysql_url": f"sqlite+aiosqlite:///{d}/auth.db"}
        )
        with TestClient(app) as client:
            yield client, db
        await db.drop_all(testing=True)
        await db.engine.dispose()


def _login(client: TestClient):
    return client.post(
        "/api/auth/login", json={"username": "lockeduser", "password": _PASSWORD}
    )


@pytest.mark.asyncio
async def test_locked_account_gets_403_not_500(db_client):
    client, db = db_client
    naive_future = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(
        tzinfo=None
    )
    await _mk_user(db, locked_until=naive_future)

    resp = _login(client)
    assert resp.status_code == 403
    assert "锁定" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_expired_naive_lockout_lets_login_proceed(db_client):
    client, db = db_client
    naive_past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
    await _mk_user(db, locked_until=naive_past)

    resp = _login(client)
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "lockeduser"
