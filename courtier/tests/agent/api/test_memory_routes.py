"""Route-level coverage for /api/memory — the permission boundaries the
service-layer tests cannot see: admin-only global writes, per-user
isolation of /mine, and the no-DB 503.
"""

import tempfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import courtier.agent.api.db as db_module
from courtier.agent.api.app import create_app
from courtier.agent.api.middleware.auth import create_access_token, hash_password
from courtier.agent.api.rate_limiter import limiter
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables.user import UserRole, UserStatus, UserTable


async def _mk_users(db: AsyncDatabase) -> tuple[int, int]:
    async with db.session() as session:
        admin = UserTable(
            username="memadmin",
            password_hash=hash_password("password-1"),
            role=UserRole.admin,
            status=UserStatus.active,
        )
        plain = UserTable(
            username="memuser",
            password_hash=hash_password("password-1"),
            role=UserRole.auditor,
            status=UserStatus.active,
        )
        session.add(admin)
        session.add(plain)
    async with db.session() as session:
        from sqlalchemy import select

        a = (
            await session.execute(
                select(UserTable).where(UserTable.username == "memadmin")
            )
        ).scalar_one()
        p = (
            await session.execute(
                select(UserTable).where(UserTable.username == "memuser")
            )
        ).scalar_one()
        return a.id, p.id


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def env(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite+aiosqlite:///{d}/memory.db"
        db = AsyncDatabase(url)
        await db.create_all()
        admin_id, plain_id = await _mk_users(db)
        monkeypatch.setattr(db_module, "_db", db)
        app = create_app(sessions_dir=d, start_plugins=False)
        app.state.settings = app.state.settings.model_copy(update={"mysql_url": url})
        with TestClient(app) as client:
            def login(username: str) -> dict:
                resp = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": "password-1"},
                )
                assert resp.status_code == 200
                return resp.json()

            yield SimpleNamespace(
                client=client,
                db=db,
                admin=login("memadmin"),
                plain=login("memuser"),
                admin_id=admin_id,
                plain_id=plain_id,
            )
        await db.drop_all(testing=True)
        await db.engine.dispose()


def _auth(env, who: str) -> dict:
    login = env.admin if who == "admin" else env.plain
    user_id = env.admin_id if who == "admin" else env.plain_id
    token = create_access_token(
        login["user"]["username"],
        user_id,
        "admin" if who == "admin" else "auditor",
        secret=env.client.app.state.settings.jwt_secret,
    )
    return {"Authorization": f"Bearer {token}"}


def test_memory_requires_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        # mysql_url empty (fallback mode): the whole /api/memory surface is 503.
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": __import__("os").environ.get(
                        "ADMIN_PASSWORD", "test-admin-password-for-pytest"
                    ),
                },
            )
            assert login.status_code == 200
            client.headers["Authorization"] = f"Bearer {login.json()['token']}"
            assert client.get("/api/memory/global").status_code == 503


def test_plain_user_cannot_write_global(env):
    headers = _auth(env, "plain")
    resp = env.client.post(
        "/api/memory/global",
        json={"title": "口径", "content": "全局内容"},
        headers=headers,
    )
    assert resp.status_code == 403


def test_admin_global_crud_and_plain_readonly(env):
    admin = _auth(env, "admin")
    created = env.client.post(
        "/api/memory/global",
        json={"title": "口径", "content": "全局内容"},
        headers=admin,
    )
    assert created.status_code == 200
    entry_id = created.json()["id"]

    # Plain users read global…
    plain = _auth(env, "plain")
    listed = env.client.get("/api/memory/global", headers=plain)
    assert listed.status_code == 200
    assert any(item["id"] == entry_id for item in listed.json())

    # …but cannot update or delete it.
    assert (
        env.client.patch(
            f"/api/memory/global/{entry_id}",
            json={"content": "篡改"},
            headers=plain,
        ).status_code
        == 403
    )
    assert (
        env.client.delete(
            f"/api/memory/global/{entry_id}", headers=plain
        ).status_code
        == 403
    )

    # Admin can delete.
    assert (
        env.client.delete(
            f"/api/memory/global/{entry_id}", headers=admin
        ).status_code
        == 200
    )


def test_mine_entries_are_isolated_between_users(env):
    plain = _auth(env, "plain")
    created = env.client.post(
        "/api/memory/mine",
        json={"title": "私记", "content": "私有内容"},
        headers=plain,
    )
    assert created.status_code == 200
    entry_id = created.json()["id"]

    # Another (admin) user must not see or delete it via /mine.
    admin = _auth(env, "admin")
    assert (
        env.client.get(
            f"/api/memory/mine/{entry_id}", headers=admin
        ).status_code
        == 404
    )
    assert env.client.get("/api/memory/mine", headers=admin).json() == []

    # The owner sees it.
    mine = env.client.get("/api/memory/mine", headers=plain)
    assert [i["id"] for i in mine.json()] == [entry_id]


def test_mine_delete_then_404(env):
    plain = _auth(env, "plain")
    created = env.client.post(
        "/api/memory/mine",
        json={"title": "待删", "content": "内容"},
        headers=plain,
    )
    entry_id = created.json()["id"]
    assert (
        env.client.delete(
            f"/api/memory/mine/{entry_id}", headers=plain
        ).status_code
        == 200
    )
    assert (
        env.client.get(
            f"/api/memory/mine/{entry_id}", headers=plain
        ).status_code
        == 404
    )
