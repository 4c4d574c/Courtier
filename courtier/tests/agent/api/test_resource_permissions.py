"""Unit tests for personal/public library permission rules."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.dialects import mysql

from courtier.agent.api.services import resource_service
from courtier.agent.api.services.resource_service import (
    _visibility_filter,
    delete_resource,
    ingest_resource,
)


def _sql(clause) -> str:
    return str(clause.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


class TestVisibilityFilter:
    def test_user_all_scope_sees_public_and_own(self):
        sql = _sql(_visibility_filter(owner_id=7, is_admin=False, scope="all"))
        assert "visibility = 'public'" in sql
        assert "visibility = 'personal' AND resources.owner_id = 7" in sql
        assert " OR " in sql

    def test_user_public_scope_sees_only_public(self):
        sql = _sql(_visibility_filter(owner_id=7, is_admin=False, scope="public"))
        assert sql == "resources.visibility = 'public'"

    def test_user_personal_scope_sees_only_own(self):
        sql = _sql(_visibility_filter(owner_id=7, is_admin=False, scope="personal"))
        assert "visibility = 'personal'" in sql
        assert "owner_id = 7" in sql
        assert "public" not in sql

    def test_anonymous_personal_scope_matches_nothing(self):
        sql = _sql(_visibility_filter(owner_id=None, is_admin=False, scope="personal"))
        assert "owner_id = -1" in sql

    def test_admin_all_scope_has_no_filter(self):
        assert _visibility_filter(owner_id=1, is_admin=True, scope="all") is None

    def test_admin_scoped_tabs_still_apply(self):
        sql = _sql(_visibility_filter(owner_id=1, is_admin=True, scope="public"))
        assert sql == "resources.visibility = 'public'"


# -- ingest_resource visibility rules ------------------------------------------


def _fake_settings(**kw):
    base = {
        "es_hosts": "http://localhost:9200",
        "es_index_chunks": "test_chunks",
        "minio_endpoint": "",  # skip MinIO in unit tests
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_db():
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=lambda: _noop())

    async def _noop():
        return None

    return SimpleNamespace(session=session)


def _patch_pipeline(monkeypatch, captured):
    async def fake_create(session, obj_in):
        captured["create"] = obj_in
        return SimpleNamespace(id=42, title=obj_in.title)

    monkeypatch.setattr(resource_service.resource_repo, "create", fake_create)
    monkeypatch.setattr(
        resource_service, "bulk_index_chunks", lambda actions: captured.setdefault("es", actions)
    )


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_visibility(monkeypatch):
    file = UploadFile(filename="a.txt", file=io.BytesIO(b"hello"))
    with pytest.raises(HTTPException) as exc:
        await ingest_resource(
            file,
            title="",
            author="",
            source="",
            tags="",
            publish_date=None,
            owner_name="u",
            owner_id=1,
            is_admin=False,
            visibility="secret",
            settings=_fake_settings(),
            db=_fake_db(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_ingest_forces_personal_for_non_admin(monkeypatch):
    captured = {}
    _patch_pipeline(monkeypatch, captured)
    file = UploadFile(filename="a.txt", file=io.BytesIO("正文内容".encode()))
    await ingest_resource(
        file,
        title="",
        author="",
        source="",
        tags="",
        publish_date=None,
        owner_name="u",
        owner_id=7,
        is_admin=False,
        visibility="public",
        settings=_fake_settings(),
        db=_fake_db(),
    )
    assert captured["create"].visibility == "personal"
    assert captured["create"].owner_id == 7
    es_body = captured["es"][1]
    assert es_body["visibility"] == "personal"
    assert es_body["owner_id"] == 7


@pytest.mark.asyncio
async def test_ingest_admin_defaults_public(monkeypatch):
    captured = {}
    _patch_pipeline(monkeypatch, captured)
    file = UploadFile(filename="a.txt", file=io.BytesIO("正文内容".encode()))
    await ingest_resource(
        file,
        title="",
        author="",
        source="",
        tags="",
        publish_date=None,
        owner_name="admin",
        owner_id=1,
        is_admin=True,
        visibility="public",
        settings=_fake_settings(),
        db=_fake_db(),
    )
    assert captured["create"].visibility == "public"
    assert captured["es"][1]["visibility"] == "public"


@pytest.mark.asyncio
async def test_ingest_admin_can_choose_personal(monkeypatch):
    captured = {}
    _patch_pipeline(monkeypatch, captured)
    file = UploadFile(filename="a.txt", file=io.BytesIO("正文内容".encode()))
    await ingest_resource(
        file,
        title="",
        author="",
        source="",
        tags="",
        publish_date=None,
        owner_name="admin",
        owner_id=1,
        is_admin=True,
        visibility="personal",
        settings=_fake_settings(),
        db=_fake_db(),
    )
    assert captured["create"].visibility == "personal"


# -- delete_resource permission rules ------------------------------------------


def _db_returning(resource):
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=lambda: _noop())

    async def _noop():
        return None

    return SimpleNamespace(session=session), resource


def _patch_get(monkeypatch, resource):
    async def fake_get(session, rid):
        return resource

    async def fake_delete(session, rid):
        return resource

    monkeypatch.setattr(resource_service.resource_repo, "get", fake_get)
    monkeypatch.setattr(resource_service.resource_repo, "delete", fake_delete)
    monkeypatch.setattr(resource_service, "delete_by_resource_id", lambda rid: None)


def _resource(visibility="personal", owner_id=7):
    return SimpleNamespace(id=42, visibility=visibility, owner_id=owner_id, minio_path="")


@pytest.mark.asyncio
async def test_delete_owner_personal_ok(monkeypatch):
    _patch_get(monkeypatch, _resource())
    db, _ = _db_returning(None)
    result = await delete_resource(db, _fake_settings(), 42, owner_id=7, is_admin=False)
    assert result is not None


@pytest.mark.asyncio
async def test_delete_other_personal_forbidden(monkeypatch):
    _patch_get(monkeypatch, _resource())
    db, _ = _db_returning(None)
    with pytest.raises(HTTPException) as exc:
        await delete_resource(db, _fake_settings(), 42, owner_id=8, is_admin=False)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_public_forbidden_for_non_admin(monkeypatch):
    _patch_get(monkeypatch, _resource(visibility="public", owner_id=None))
    db, _ = _db_returning(None)
    with pytest.raises(HTTPException) as exc:
        await delete_resource(db, _fake_settings(), 42, owner_id=7, is_admin=False)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_admin_any_ok(monkeypatch):
    _patch_get(monkeypatch, _resource(visibility="public", owner_id=None))
    db, _ = _db_returning(None)
    result = await delete_resource(db, _fake_settings(), 42, owner_id=1, is_admin=True)
    assert result is not None


@pytest.mark.asyncio
async def test_delete_missing_returns_none(monkeypatch):
    _patch_get(monkeypatch, None)
    db, _ = _db_returning(None)
    result = await delete_resource(db, _fake_settings(), 42, owner_id=1, is_admin=True)
    assert result is None
