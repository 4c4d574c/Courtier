"""Route-level tests: /sessions/{id}/events attach endpoint + reconnect semantics."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from courtier.agent.api.app import create_app
from courtier.agent.api.rate_limiter import limiter

# conftest.py ensures ADMIN_PASSWORD is set (and MYSQL_URL empty → no-DB
# fallback admin login) for every test in this repo.
_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def app_client(tmp_path):
    app = create_app(sessions_dir=str(tmp_path), start_plugins=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
            )
            assert resp.status_code == 200, f"Login failed: {resp.text}"
            client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
            yield app, client


async def _fake_build_agent(*args, **kwargs):
    from courtier.agent.agents.base import Agent
    from courtier.agent.testing import MockModelClient

    model = MockModelClient(tool_calls=[])
    return Agent(name="TestChat", role="Test role", tools=[], model=model), None, "test-model"


def _sse_payloads(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


class TestAttachEndpoint:
    @pytest.mark.asyncio
    async def test_attach_404_without_run(self, app_client):
        _app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        assert (await client.get("/api/sessions")).json()  # session exists
        # The completed run is within its grace period (attach succeeds),
        # so verify 404 with a session id that has no run at all.
        resp = await client.get("/api/sessions/sess_ffffffffff/events")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_attach_replays_full_transcript_with_terminal(self, app_client):
        app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        # Run completed; still within grace — attach replays from seq 0.
        resp = await client.get(f"/api/sessions/{sid}/events", params={"since": 0})
        assert resp.status_code == 200
        payloads = _sse_payloads(resp.text)
        types = [p["type"] for p in payloads]
        assert types[0] == "session"
        assert types[-1] == "complete"

    @pytest.mark.asyncio
    async def test_attach_since_watermark_replays_tail_only(self, app_client):
        app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        detail = (await client.get(f"/api/sessions/{sid}")).json()
        watermark = detail["eventSeq"]
        assert watermark > 0

        # Attach from 3 events back: exactly the last 3 events replay.
        resp = await client.get(f"/api/sessions/{sid}/events", params={"since": watermark - 3})
        assert resp.status_code == 200
        payloads = _sse_payloads(resp.text)
        assert len(payloads) == 3
        assert payloads[-1]["type"] == "complete"

    @pytest.mark.asyncio
    async def test_attach_last_event_id_header_overrides_since(self, app_client):
        app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        detail = (await client.get(f"/api/sessions/{sid}")).json()
        watermark = detail["eventSeq"]

        resp = await client.get(
            f"/api/sessions/{sid}/events",
            params={"since": 0},
            headers={"Last-Event-ID": str(watermark)},
        )
        assert resp.status_code == 200
        assert _sse_payloads(resp.text) == []  # nothing after the watermark

    @pytest.mark.asyncio
    async def test_attach_requires_ownership(self, app_client):
        _app, client = app_client
        resp = await client.get("/api/sessions/sess_aaaaaaaaaaaa/events")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_snapshot_includes_event_seq(self, app_client):
        app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["eventSeq"] >= 1


class TestReconnectSemantics:
    @pytest.mark.asyncio
    async def test_same_task_reconnect_attaches_without_new_turn(self, app_client):
        """EventSource 断线重连(带相同 task 重发)不注册重复轮次。"""
        app, client = app_client

        async def _slow_build(*args, **kwargs):
            from courtier.agent.agents.base import Agent

            class _SlowModel:
                model_name = "slow"
                temperature = 0.0

                async def generate(self, messages, tools=None, **kw):
                    await asyncio.sleep(0.5)
                    from courtier.agent.core.model import ModelResponse

                    return ModelResponse(content="Done.", tool_calls=[])

                async def generate_stream_full(self, messages, tools=None, **kw):
                    resp = await self.generate(messages, tools, **kw)
                    on_content_token = kw.get("on_content_token")
                    if on_content_token:
                        await on_content_token(resp.content)
                    return resp

                async def close(self):
                    return

            return (
                Agent(name="SlowChat", role="slow", tools=[], model=_SlowModel()),
                None,
                "slow-model",
            )

        with patch("courtier.agent.api.routes.sessions.build_agent", new=_slow_build):
            # First connection: starts the run.
            stream = asyncio.create_task(client.get("/api/sessions", params={"task": "hello"}))
            await asyncio.sleep(0.1)  # run is now executing
            sid = None
            while sid is None:
                sessions = (await client.get("/api/sessions")).json()
                if sessions:
                    sid = sessions[0]["id"]
                else:
                    await asyncio.sleep(0.05)

            # Reconnect with the same task while still running → attach.
            resp = await client.get("/api/sessions", params={"task": "hello", "sessionId": sid})
            assert resp.status_code == 200
            await stream
            types = [p["type"] for p in _sse_payloads(resp.text)]
            assert types  # re-attached stream carried events
            assert types[-1] == "complete"

        detail = (await client.get(f"/api/sessions/{sid}")).json()
        # No duplicate turn was registered by the reconnect.
        assert len(detail["turns"]) == 1

    @pytest.mark.asyncio
    async def test_different_task_while_running_409(self, app_client):
        app, client = app_client

        async def _slow_build(*args, **kwargs):
            from courtier.agent.agents.base import Agent

            class _SlowModel:
                model_name = "slow"
                temperature = 0.0

                async def generate(self, messages, tools=None, **kw):
                    await asyncio.sleep(0.5)
                    from courtier.agent.core.model import ModelResponse

                    return ModelResponse(content="Done.", tool_calls=[])

                async def generate_stream_full(self, messages, tools=None, **kw):
                    resp = await self.generate(messages, tools, **kw)
                    on_content_token = kw.get("on_content_token")
                    if on_content_token:
                        await on_content_token(resp.content)
                    return resp

                async def close(self):
                    return

            return (
                Agent(name="SlowChat", role="slow", tools=[], model=_SlowModel()),
                None,
                "slow-model",
            )

        with patch("courtier.agent.api.routes.sessions.build_agent", new=_slow_build):
            stream = asyncio.create_task(client.get("/api/sessions", params={"task": "hello"}))
            await asyncio.sleep(0.1)
            sid = None
            while sid is None:
                sessions = (await client.get("/api/sessions")).json()
                if sessions:
                    sid = sessions[0]["id"]
                else:
                    await asyncio.sleep(0.05)
            resp = await client.get("/api/sessions", params={"task": "different", "sessionId": sid})
            assert resp.status_code == 409
            await stream


class TestLiveStatusOverlay:
    @pytest.mark.asyncio
    async def test_list_overlays_live_run_status(self, app_client):
        app, client = app_client
        with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
            resp = await client.get("/api/sessions", params={"task": "hello"})
        assert resp.status_code == 200
        sid = (await client.get("/api/sessions")).json()[0]["id"]

        # Store says completed; pretend the run manager still tracks a live run.
        from courtier.agent.api.services.run_event_log import RunEventLog
        from courtier.agent.api.services.run_manager import AgentRun
        from courtier.agent.core.event_bus import EventBus

        run = AgentRun(sid, "admin", RunEventLog(), EventBus())
        app.state.run_manager._runs[sid] = run
        statuses = {s["id"]: s["status"] for s in (await client.get("/api/sessions")).json()}
        assert statuses[sid] == "running"
        run.status = "completed"
        run.finished_at = 0  # long past → expired on next access
        statuses = {s["id"]: s["status"] for s in (await client.get("/api/sessions")).json()}
        assert statuses[sid] == "completed"
