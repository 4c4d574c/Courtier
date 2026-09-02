"""Tests for GET /api/files/{file_id} — historical file preview/download."""

import tempfile

import pytest
from fastapi.testclient import TestClient

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


def _make_client():
    app = create_app(sessions_dir=tempfile.mkdtemp(), start_plugins=False)
    return TestClient(app)


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"


def _login_as(client: TestClient, username: str, role: str = "user") -> None:
    """Mint a JWT for a non-bootstrap user (register needs a real DB)."""
    from courtier.agent.api.middleware.auth import create_access_token

    token = create_access_token(
        username, 0, role, secret=client.app.state.settings.jwt_secret
    )
    client.headers["Authorization"] = f"Bearer {token}"


def _upload(client: TestClient, name: str = "test.png") -> str:
    resp = client.post(
        "/api/files",
        files={"file": (name, _PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["fileId"]


def test_download_roundtrip_admin_owner():
    with _make_client() as c:
        _login(c, "admin", "test-admin-password-for-pytest")
        file_id = _upload(c)
        resp = c.get(f"/api/files/{file_id}")
        assert resp.status_code == 200
        assert resp.content == _PNG_BYTES
        assert resp.headers["content-type"].startswith("image/png")
        disposition = resp.headers["content-disposition"]
        assert "inline" in disposition
        assert "test.png" in disposition


def test_download_unknown_file_404():
    with _make_client() as c:
        _login(c, "admin", "test-admin-password-for-pytest")
        resp = c.get("/api/files/file_does_not_exist")
        assert resp.status_code == 404


def test_download_other_user_403():
    with _make_client() as c:
        _login(c, "admin", "test-admin-password-for-pytest")
        file_id = _upload(c)
        _login_as(c, "alice")
        resp = c.get(f"/api/files/{file_id}")
        assert resp.status_code == 403


def test_admin_can_download_other_users_file():
    with _make_client() as c:
        _login_as(c, "bob")
        file_id = _upload(c)
        _login(c, "admin", "test-admin-password-for-pytest")
        resp = c.get(f"/api/files/{file_id}")
        assert resp.status_code == 200


def test_legacy_empty_owner_readable_by_any_user():
    with _make_client() as c:
        _login(c, "admin", "test-admin-password-for-pytest")
        file_id = _upload(c)
        # Overwrite the owner to simulate a legacy (pre-attribution) record.
        record = c.app.state.file_store._files[file_id]
        object.__setattr__(record, "owner", "")
        resp = c.get(f"/api/files/{file_id}")
        assert resp.status_code == 200


def test_cookie_only_auth_downloads():
    with _make_client() as c:
        resp = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-admin-password-for-pytest"},
        )
        assert resp.status_code == 200
        # No Authorization header — the login response's httpOnly cookie
        # must authenticate the iframe-style GET.
        file_id = _upload(c)
        c.headers.pop("Authorization", None)
        resp = c.get(f"/api/files/{file_id}")
        assert resp.status_code == 200
        assert resp.content == _PNG_BYTES
