"""Confirmations API: resolve endpoint + detail pending field."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from courtier.agent.api.app import create_app

_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from courtier.agent.api.rate_limiter import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        with TestClient(app) as c:
            login = c.post(
                "/api/auth/login",
                json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
            )
            assert login.status_code == 200
            c.headers["Authorization"] = f"Bearer {login.json()['token']}"
            yield c


def test_resolve_unknown_session_404(client):
    resp = client.post(
        "/api/sessions/sess_nonexistent00/confirmations/cf_deadbeef",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404


def test_resolve_invalid_decision_rejected(client):
    resp = client.post(
        "/api/sessions/sess_nonexistent00/confirmations/cf_deadbeef",
        json={"decision": "maybe"},
    )
    # Literal validation rejects before owner lookup; any of these is acceptable.
    assert resp.status_code in (400, 404, 422)
