"""approved_tools persistence roundtrip (confirmation chain T3)."""

from __future__ import annotations

import asyncio

from courtier.agent.api.session_store import SessionStore

SESSION_ID = "sess_7e57e57e0001"


def test_approved_tools_roundtrip(tmp_path):
    async def _run():
        store = SessionStore(str(tmp_path))
        await store.create(SESSION_ID, "task", "")
        await store.update(SESSION_ID, approved_tools=["deploy", "purge_cache"])

        fresh = SessionStore(str(tmp_path))
        record = await fresh.get(SESSION_ID)
        return record

    record = asyncio.run(_run())
    assert record is not None
    assert record.approved_tools == ["deploy", "purge_cache"]


def test_detail_dict_exposes_approved_tools():
    from courtier.agent.api.models import SessionRecord

    record = SessionRecord(id="x", task="t", file_id="", approved_tools=["deploy"])
    detail = record.to_detail_dict()
    assert detail["approvedTools"] == ["deploy"]
