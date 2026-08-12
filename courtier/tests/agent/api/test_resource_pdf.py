"""Tests for the resource PDF preview service (`get_resource_pdf`)."""

from __future__ import annotations

import types
from typing import Any

import pytest

from courtier.agent.api.services import resource_service
from courtier.db.tables.resource import ResourceTable


def _resource(**overrides: Any) -> ResourceTable:
    fields = {
        "id": 1,
        "title": "保密法",
        "author": None,
        "source": None,
        "tags": None,
        "publish_date": None,
        "file_type": "pdf",
        "file_size": 1234,
        "minio_path": "abc123/保密法.pdf",
        "md5": "abc123",
        "chunk_count": 3,
        "char_count": 1200,
        "status": "ready",
        "resource_type": "manual",
        "document_id": None,
        "owner_id": 1,
        "visibility": "public",
    }
    fields.update(overrides)
    return ResourceTable(**fields)


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDb:
    def __init__(self) -> None:
        self._session = _FakeSession()

    def session(self):
        return self._session


def _settings(minio_endpoint: str = "http://minio:9000") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        minio_endpoint=minio_endpoint,
        minio_bucket_resources="resources",
    )


async def _run(db: _FakeDb, settings: Any, resource_id: int, *, owner_id=1, is_admin=False):
    return await resource_service.get_resource_pdf(
        db,
        settings,
        resource_id,
        owner_id=owner_id,
        is_admin=is_admin,
    )


def test_text_to_pdf_bytes_produces_valid_pdf():
    import fitz

    pdf = resource_service._text_to_pdf_bytes("第一行\n第二行内容")
    assert pdf.startswith(b"%PDF")
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count >= 1
        text = "".join(page.get_text() for page in doc)
        assert "第一行" in text


def test_docx_fallback_produces_text_pdf(monkeypatch):
    """When LibreOffice is unavailable, DOCX falls back to a text-layout PDF."""
    monkeypatch.setattr(resource_service, "_convert_docx_to_pdf_via_soffice", lambda src, out: None)
    monkeypatch.setattr(resource_service, "get_object", lambda bucket, key: b"docx-bytes")
    monkeypatch.setattr(resource_service, "_extract_text", lambda path: "第一行内容")
    resource = _resource(file_type="docx", minio_path="abc/doc.docx")
    pdf = resource_service._resource_pdf_bytes(resource, "bucket")
    assert pdf.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_missing_resource_raises_404(monkeypatch):
    async def _get(session, rid):
        return None

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    with pytest.raises(Exception) as excinfo:
        await _run(_FakeDb(), _settings(), 99)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_invisible_personal_resource_raises_403(monkeypatch):
    async def _get(session, rid):
        return _resource(visibility="personal", owner_id=7)

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    with pytest.raises(Exception) as excinfo:
        await _run(_FakeDb(), _settings(), 1, owner_id=1, is_admin=False)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_own_personal_resource_allowed(monkeypatch):
    async def _get(session, rid):
        return _resource(visibility="personal", owner_id=1)

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    monkeypatch.setattr(resource_service, "object_exists", lambda bucket, key: True)
    monkeypatch.setattr(
        resource_service,
        "get_presigned_url",
        lambda bucket, key: f"http://minio/{bucket}/{key}",
    )
    url, converted, original = await _run(_FakeDb(), _settings(), 1, owner_id=1)
    assert url.endswith("/abc123/保密法.pdf")
    assert converted is False
    assert original == url


@pytest.mark.asyncio
async def test_pdf_resource_served_directly_without_conversion(monkeypatch):
    async def _get(session, rid):
        return _resource(file_type="pdf")

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    monkeypatch.setattr(resource_service, "object_exists", lambda bucket, key: True)
    monkeypatch.setattr(
        resource_service,
        "get_presigned_url",
        lambda bucket, key: f"http://minio/{bucket}/{key}",
    )
    url, converted, original = await _run(_FakeDb(), _settings(), 1)
    assert converted is False
    assert url == "http://minio/resources/abc123/保密法.pdf"
    assert original == url


@pytest.mark.asyncio
async def test_docx_converted_and_cached(monkeypatch):
    resource = _resource(file_type="docx", minio_path="abc/doc.docx")
    calls: dict[str, Any] = {}

    async def _get(session, rid):
        return resource

    def _pdf_bytes(resource_, bucket):
        return b"%PDF-cache-me"

    def _object_exists(bucket, key):
        calls["exists"] = key
        return False

    def _put(bucket, key, data, ctype):
        calls["put"] = (key, data, ctype)

    def _presign(bucket, key):
        return f"http://minio/{bucket}/{key}"

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    monkeypatch.setattr(resource_service, "_resource_pdf_bytes", _pdf_bytes)
    monkeypatch.setattr(resource_service, "object_exists", _object_exists)
    monkeypatch.setattr(resource_service, "put_object", _put)
    monkeypatch.setattr(resource_service, "get_presigned_url", _presign)

    url, converted, original = await _run(_FakeDb(), _settings(), 1)
    assert converted is True
    assert url == "http://minio/resources/abc123/preview.pdf"
    assert original == "http://minio/resources/abc/doc.docx"
    assert calls["exists"] == "abc123/preview.pdf"
    assert calls["put"][1] == b"%PDF-cache-me"
    assert calls["put"][2] == "application/pdf"


@pytest.mark.asyncio
async def test_minio_missing_raises_503(monkeypatch):
    async def _get(session, rid):
        return _resource()

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    with pytest.raises(Exception) as excinfo:
        await _run(_FakeDb(), _settings(minio_endpoint=""), 1)
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_ready_status_required(monkeypatch):
    async def _get(session, rid):
        return _resource(status="processing")

    monkeypatch.setattr(resource_service.resource_repo, "get", _get)
    monkeypatch.setattr(resource_service, "object_exists", lambda b, k: True)
    with pytest.raises(Exception) as excinfo:
        await _run(_FakeDb(), _settings(), 1)
    assert excinfo.value.status_code == 409
