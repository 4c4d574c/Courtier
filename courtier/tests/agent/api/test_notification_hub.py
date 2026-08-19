"""Tests for NotificationHub + the global events channel (GET /api/events)."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter
from courtier.agent.api.services.notification_hub import NotificationHub

_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


class TestNotificationHub:
    async def test_publish_reaches_owner_connections(self):
        hub = NotificationHub()
        alice = hub.subscribe("alice")
        bob = hub.subscribe("bob")
        hub.publish("alice", session_id="sess_aaaaaaaaaaaa", status="completed", conclusion="结论")
        line = await asyncio.wait_for(alice.queue.get(), timeout=1)
        payload = json.loads(line.split("data: ", 1)[1])
        assert payload["type"] == "run_status"
        assert payload["sessionId"] == "sess_aaaaaaaaaaaa"
        assert payload["status"] == "completed"
        assert payload["conclusion"] == "结论"
        assert bob.queue.empty()

    async def test_unsubscribe_stops_delivery(self):
        hub = NotificationHub()
        conn = hub.subscribe("alice")
        hub.unsubscribe(conn)
        hub.publish("alice", session_id="sess_aaaaaaaaaaaa", status="error")
        assert conn.queue.empty()
        assert hub.connection_count == 0

    async def test_full_queue_drops_oldest(self):
        hub = NotificationHub()
        conn = hub.subscribe("alice")
        conn.queue = asyncio.Queue(maxsize=2)
        for i in range(5):
            hub.publish("alice", session_id=f"sess_{i:012d}", status="running")
        # Queue holds only the latest events; none lost silently below bound.
        drained = []
        while not conn.queue.empty():
            drained.append(json.loads(conn.queue.get_nowait().split("data: ", 1)[1]))
        assert len(drained) == 2
        assert drained[-1]["sessionId"] == "sess_000000000004"

    async def test_stream_yields_heartbeat_when_idle(self):
        hub = NotificationHub()
        conn = hub.subscribe("alice")
        gen = hub.stream(conn, heartbeat_seconds=0.01)
        line = await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert line == ": hb\n\n"
        await gen.aclose()

    async def test_stream_unsubscribes_on_close(self):
        hub = NotificationHub()
        conn = hub.subscribe("alice")
        gen = hub.stream(conn, heartbeat_seconds=0.01)
        # Drive one iteration so the generator is inside the try-block before
        # closing (finally runs on the next scheduled aclose turn).
        await asyncio.wait_for(gen.__anext__(), timeout=2)
        await gen.aclose()
        await asyncio.sleep(0)
        assert hub.connection_count == 0


class TestGlobalEventsChannel:
    @pytest.mark.asyncio
    async def test_channel_requires_auth(self, tmp_path):
        app = create_app(sessions_dir=str(tmp_path), start_plugins=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/events")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_run_completion_reaches_channel(self, tmp_path):
        """发起任务 → 终态状态经全局通道推给该用户的连接。"""
        app = create_app(sessions_dir=str(tmp_path), start_plugins=False)

        async def _fake_build_agent(*args, **kwargs):
            from courtier.agent.agents.base import Agent
            from courtier.agent.testing import MockModelClient

            model = MockModelClient(tool_calls=[])
            return Agent(name="T", role="r", tools=[], model=model), None, "test-model"

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", timeout=15
            ) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
                )
                token = resp.json().get("token") or resp.json().get("access_token")
                client.headers["Authorization"] = f"Bearer {token}"

                # Open the channel via the hub directly (an HTTP-level stream
                # assertion would require streaming reads; the hub is the
                # contract the route forwards verbatim).
                conn = app.state.notification_hub.subscribe("admin")
                with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
                    resp = await client.get("/api/sessions", params={"task": "hello"})
                assert resp.status_code == 200
                line = await asyncio.wait_for(conn.queue.get(), timeout=5)
                payload = json.loads(line.split("data: ", 1)[1])
                assert payload["type"] == "run_status"
                assert payload["status"] == "completed"
                assert "tokensIn" in payload and "tokensOut" in payload
