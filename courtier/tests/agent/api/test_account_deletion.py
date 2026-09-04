"""Account deletion suite — pipeline cascade, anonymization, receipt.

The pipeline runs against a real sqlite database + real SessionStore /
FileStore on tmp dirs; ES/MinIO-touching steps are injected as fakes.
Route-level guards (admin undeletable, password check) run through a
TestClient with overridden auth dependencies.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import courtier.agent.api.db as db_module
from courtier.agent.api.file_store import FileStore
from courtier.agent.api.middleware.auth import hash_password
from courtier.agent.api.routes.account_deletion import (
    admin_router,
    admin_users_router,
)
from courtier.agent.api.routes.account_deletion import (
    router as deletion_router,
)
from courtier.agent.api.services.account_deletion_service import (
    execute_account_deletion,
)
from courtier.agent.api.session_store import SessionStore
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables import (
    MemoryChangeTable,
    MemoryTable,
    ResourceTable,
    SettingsChangeTable,
    SettingsTable,
    UserTable,
)
from courtier.db.tables.deletion import (
    REQUEST_PENDING,
    DeletionLogTable,
    DeletionRequestTable,
)
from courtier.db.tables.user import UserRole, UserStatus

TARGET = "wanda"
OTHER = "nobody"


async def _seed_user(db, username: str, *, role: UserRole = UserRole.auditor) -> int:
    async with db.session() as session:
        row = UserTable(
            username=username,
            password_hash=hash_password("correct-password"),
            email=f"{username}@test.local",
            role=role,
            status=UserStatus.active,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.fixture
async def env(tmp_path):
    db = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'deletion.db'}")
    await db.create_all()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    settings = SimpleNamespace(
        cache_dir=str(tmp_path / ".cache"),
        upload_dir=str(upload_dir),
    )
    settings.cache_dir and (tmp_path / ".cache").mkdir(exist_ok=True)
    session_store = SessionStore(str(tmp_path / ".cache" / "sessions"))
    file_store = FileStore(str(upload_dir / ".file_registry"))
    yield SimpleNamespace(
        db=db, settings=settings, sessions=session_store, files=file_store, tmp=tmp_path
    )
    await db.drop_all(testing=True)
    await db.engine.dispose()


async def _seed_user_data(ns) -> None:
    """One full data footprint for TARGET: session, upload, resources,
    audit results, memory, settings refs."""
    uid = await _seed_user(ns.db, TARGET)
    other_uid = await _seed_user(ns.db, OTHER)
    await ns.sessions.create("sess_" + "a" * 12, "任务", "", owner=TARGET)
    await ns.sessions.create("sess_" + "b" * 12, "任务", "", owner=OTHER)

    upload = ns.tmp / "uploads" / "2026" / "doc.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("个人上传", encoding="utf-8")
    other_upload = ns.tmp / "uploads" / "2026" / "other.txt"
    other_upload.write_text("他人上传", encoding="utf-8")
    await ns.files.register("doc.txt", "2026/doc.txt", 15, owner=TARGET)
    await ns.files.register("other.txt", "2026/other.txt", 3, owner=OTHER)

    async with ns.db.session() as session:
        session.add_all(
            [
                ResourceTable(
                    title="个人资源", file_type="pdf", file_size=1, minio_path="p1",
                    md5="h1", owner_id=uid, visibility="personal", status="ready",
                ),
                ResourceTable(
                    title="公共资源", file_type="pdf", file_size=1, minio_path="p2",
                    md5="h2", owner_id=uid, visibility="public", status="ready",
                ),
                ResourceTable(
                    title="他人资源", file_type="pdf", file_size=1, minio_path="p3",
                    md5="h3", owner_id=other_uid, visibility="personal", status="ready",
                ),
            ]
        )
        await session.commit()

    async with ns.db.session() as session:
        session.add_all(
            [
                MemoryTable(layer="user", owner_id=uid, domain="common",
                            title="偏好", content="简洁", created_by=TARGET, updated_by=TARGET),
                MemoryChangeTable(entry_id=1, action="create", layer="user", owner_id=uid,
                                  domain="common", title="偏好", old_hash=None,
                                  new_hash="a" * 64, actor=TARGET),
                MemoryChangeTable(entry_id=2, action="create", layer="user", owner_id=uid,
                                  domain="common", title="代理写入", old_hash=None,
                                  new_hash="b" * 64, actor=f"agent:{TARGET}"),
            ]
        )
        await session.commit()

    async with ns.db.session() as session:
        session.add_all(
            [
                SettingsTable(key="llm_model", value="{}", updated_by=TARGET),
                SettingsChangeTable(
                    key_name="llm_model", old_hash=None, new_hash="c" * 64, actor=TARGET
                ),
            ]
        )
        await session.commit()
    return uid


async def _seed_audit_result(ns, user_id: int) -> None:
    """One audit-results row (format table) owned by TARGET's username."""
    from courtier.db.tables import FormatAuditResultTable

    async with ns.db.session() as session:
        session.add(
            FormatAuditResultTable(
                document_id=0, user_id=TARGET, doc_type="通知",
                total_pages=1, error_count=1, errors_json="[]",
            )
        )
        await session.commit()


async def test_pipeline_cascades_and_writes_receipt(env, monkeypatch):
    uid = await _seed_user_data(env)
    await _seed_audit_result(env, uid)
    resource_calls: list[int] = []
    es_calls: list[list[str]] = []

    async def fake_resource_deleter(db, settings, resource_id, *, owner_id=None):
        resource_calls.append(resource_id)
        async with db.session() as session:
            row = await session.get(ResourceTable, resource_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
                return row
        return None

    def fake_es_cleaner(session_ids):
        es_calls.append(list(session_ids))
        return 7

    result = await execute_account_deletion(
        env.db,
        env.settings,
        env.sessions,
        env.files,
        user_id=uid,
        username=TARGET,
        trigger="admin",
        executed_by="admin",
        resource_deleter=fake_resource_deleter,
        session_results_cleaner=fake_es_cleaner,
    )


    # ES 清理只收到本人会话
    assert es_calls and sorted(es_calls[0]) == ["sess_" + "a" * 12]
    assert result.counts["es_results"] == 7
    assert result.counts["sessions"] == 1  # 只有本人会话
    assert result.counts["files"] == 1
    assert result.counts["resources"] == 1
    assert result.counts["audit_results"] == 1
    assert result.counts["memories"] == 1
    assert result.counts["user"] == 1
    assert result.failures == {}

    async with env.db.session() as session:
        assert await session.get(UserTable, uid) is None  # 账号行已删
        assert await env.sessions.get_owned("sess_" + "a" * 12, TARGET) is None  # 本人会话已删
        assert await env.sessions.get_owned("sess_" + "b" * 12, OTHER) is not None  # 他人会话不动

        remaining = (
            await session.execute(
                select(ResourceTable).where(ResourceTable.title == "公共资源")
            )
        ).scalar_one()
        assert remaining.owner_id is None  # 公共资源保留且匿名
        others = (
            await session.execute(
                select(ResourceTable).where(ResourceTable.title == "他人资源")
            )
        ).scalar_one()
        assert others.owner_id is not None  # 他人资源不动

        assert (
            await session.execute(select(func.count()).select_from(MemoryTable))
        ).scalar_one() == 0
        changes = (await session.execute(select(MemoryChangeTable.actor))).scalars().all()
        # 两条历史审计（用户名 / agent:用户名）匿名化 + delete_user_memory 的 system 删除审计
        assert sorted(changes) == sorted([f"deleted-user:{uid}", f"deleted-user:{uid}", "system"])
        settings_actor = (await session.execute(select(SettingsChangeTable.actor))).scalar_one()
        assert settings_actor == f"deleted-user:{uid}"
        settings_row = (await session.execute(select(SettingsTable))).scalar_one()
        assert settings_row.updated_by == f"deleted-user:{uid}"
        receipt = (await session.execute(select(DeletionLogTable))).scalar_one()
        assert receipt.user_id == uid and receipt.trigger == "admin"
        assert receipt.counts["user"] == 1 and receipt.failures == {}

    # 上传文件物理删除，他人文件仍在
    assert not (env.tmp / "uploads" / "2026" / "doc.txt").exists()
    assert (env.tmp / "uploads" / "2026" / "other.txt").exists()


async def test_pipeline_marks_pending_request_executed(env):
    from sqlalchemy import select

    uid = await _seed_user(env.db, TARGET)
    async with env.db.session() as session:
        session.add(DeletionRequestTable(user_id=uid, status=REQUEST_PENDING, requested_by="self"))
        await session.commit()

    await execute_account_deletion(
        env.db, env.settings, env.sessions, env.files,
        user_id=uid, username=TARGET, trigger="self_approved", executed_by="admin",
    )

    async with env.db.session() as session:
        row = (await session.execute(select(DeletionRequestTable))).scalar_one()
        assert row.status == "executed" and row.decided_by == "admin"


async def test_pipeline_external_failure_recorded_not_fatal(env, monkeypatch):
    uid = await _seed_user_data(env)

    async def broken_resource_deleter(db, settings, resource_id, *, owner_id=None):
        raise RuntimeError("minio down")

    def broken_es(session_ids):
        raise RuntimeError("es down")

    result = await execute_account_deletion(
        env.db, env.settings, env.sessions, env.files,
        user_id=uid, username=TARGET, trigger="admin", executed_by="admin",
        resource_deleter=broken_resource_deleter,
        session_results_cleaner=broken_es,
    )

    assert "es_results" in result.failures
    assert any(k.startswith("resource:") for k in result.failures)
    assert result.counts["user"] == 1  # 外部失败不阻断账号清除
    async with env.db.session() as session:
        receipt = (await session.execute(select(DeletionLogTable))).scalar_one()
        assert receipt.failures  # 回执记录了失败


# ----------------------------------------------------------------- route guards


def _app(ns, monkeypatch):

    app = FastAPI()
    app.include_router(deletion_router)
    app.include_router(admin_router)
    app.include_router(admin_users_router)
    app.state.settings = ns.settings
    app.state.session_store = ns.sessions
    app.state.file_store = ns.files
    app.state.run_manager = None
    monkeypatch.setattr(db_module, "_db", ns.db)
    return app


def _override(app, monkeypatch, payload):
    from courtier.agent.api.middleware.auth import get_current_user
    from courtier.agent.api.routes.admin_users import require_admin

    app.dependency_overrides[get_current_user] = lambda: payload
    app.dependency_overrides[require_admin] = lambda: payload


@pytest.mark.asyncio
async def test_submit_request_rejects_admin_and_wrong_password(env, monkeypatch):
    app = _app(env, monkeypatch)
    admin_id = await _seed_user(env.db, "root", role=UserRole.admin)
    user_id = await _seed_user(env.db, TARGET)

    _override(app, monkeypatch, {"uid": admin_id, "sub": "root", "role": "admin"})
    with TestClient(app) as client:
        resp = client.post("/api/profile/deletion-request", json={"password": "x"})
        assert resp.status_code == 403  # 管理员不可注销

    _override(app, monkeypatch, {"uid": user_id, "sub": TARGET, "role": "auditor"})
    with TestClient(app) as client:
        resp = client.post("/api/profile/deletion-request", json={"password": "wrong"})
        assert resp.status_code == 401
        resp = client.post("/api/profile/deletion-request", json={"password": "correct-password"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        resp = client.post("/api/profile/deletion-request", json={"password": "correct-password"})
        assert resp.status_code == 409  # 重复申请
        resp = client.get("/api/profile/deletion-request")
        assert resp.json()["request"]["status"] == "pending"
        resp = client.delete("/api/profile/deletion-request")
        assert resp.status_code == 200  # 撤回


@pytest.mark.asyncio
async def test_admin_direct_delete_refuses_admin_target(env, monkeypatch):
    app = _app(env, monkeypatch)
    admin_id = await _seed_user(env.db, "root", role=UserRole.admin)

    _override(app, monkeypatch, {"uid": admin_id + 100, "sub": "other-admin", "role": "admin"})
    with TestClient(app) as client:
        resp = client.delete(f"/api/admin/users/{admin_id}")
        assert resp.status_code == 400
