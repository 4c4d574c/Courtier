"""Tests for the search-result citation payload (`_build_citations_payload`).

The payload is what the frontend uses to resolve `[[n]]` markers in assistant
conclusions to a source file and the cited passage.
"""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest

from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.core.loop import _build_citations_payload


def _hit(
    *,
    resource_id: int = 1,
    title: str = "保密法",
    chunk_text: str = "第十四条 国家秘密的密级…",
    highlight: list[str] | None = None,
    chunk_no: int = 1,
    paragraph_index: int = 2,
) -> dict:
    return {
        "document_id": 11,
        "resource_id": resource_id,
        "doc_type": "pdf",
        "title": title,
        "chunk_text": chunk_text,
        "paragraph_index": paragraph_index,
        "tags": [],
        "author": "",
        "publish_date": None,
        "source_id": None,
        "chunk_no": chunk_no,
        "highlight": highlight,
    }


def _search_result(n_hits: int = 2) -> dict:
    return {
        "total": n_hits,
        "took_ms": 3,
        "hits": [
            _hit(
                resource_id=i + 1,
                title=f"来源{i + 1}",
                highlight=[f"<em>关键</em>条款{i + 1}"],
                chunk_no=i + 1,
                paragraph_index=i,
            )
            for i in range(n_hits)
        ],
    }


@pytest.mark.asyncio
async def test_inline_result_produces_compact_citations():
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        raw_data=_search_result(),
    )
    citations = await _build_citations_payload("search_documents", result, None)
    assert citations is not None
    assert len(citations) == 2
    first = citations[0]
    assert first["resourceId"] == 1
    assert first["documentId"] == 11
    assert first["title"] == "来源1"
    assert first["docType"] == "pdf"
    assert first["chunkNo"] == 1
    assert first["paragraphIndex"] == 0
    assert first["highlight"] == ["<em>关键</em>条款1"]
    assert "国家秘密" in first["chunkText"]


@pytest.mark.asyncio
async def test_persisted_result_loads_full_payload_from_artifact_store():
    payload = _search_result()

    class _FakeArtifactStore:
        def load(self, ref_id: str):
            return payload

    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        result_id="$ref:search_documents:1",
        raw_data=None,  # dropped by the summarizer when persisted
    )
    citations = await _build_citations_payload("search_documents", result, _FakeArtifactStore())
    assert citations is not None
    assert citations[0]["title"] == "来源1"


@pytest.mark.asyncio
async def test_store_without_load_falls_back_to_read_unwrap():
    """Stores exposing only ``read`` (wrapped ``{data, metadata}`` dicts) work too."""
    payload = _search_result()

    class _ReadOnlyStore:
        async def read(self, ref_id: str, **kwargs):
            return {"data": payload, "metadata": {}}

    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        result_id="$ref:search_documents:1",
        raw_data=None,
    )
    citations = await _build_citations_payload("search_documents", result, _ReadOnlyStore())
    assert citations is not None
    assert citations[0]["title"] == "来源1"


@pytest.mark.asyncio
async def test_artifact_store_failure_degrades_to_none():
    class _BrokenStore:
        def load(self, ref_id: str):
            raise RuntimeError("store gone")

    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        result_id="$ref:search_documents:1",
    )
    assert await _build_citations_payload("search_documents", result, _BrokenStore()) is None


@pytest.mark.asyncio
async def test_non_search_tool_returns_none():
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="convert_document",
        raw_data=_search_result(),
    )
    assert await _build_citations_payload("convert_document", result, None) is None


@pytest.mark.asyncio
async def test_failed_result_returns_none():
    result = ExecutionResult.from_error(
        actor_type="tool", actor_name="search_documents", error="es down"
    )
    assert await _build_citations_payload("search_documents", result, None) is None


@pytest.mark.asyncio
async def test_bounded_hits_chunk_and_highlight():
    n = 60
    payload = {"total": n, "took_ms": 1, "hits": []}
    for i in range(n):
        payload["hits"].append(
            _hit(
                resource_id=i + 1,
                chunk_text="x" * 900,
                highlight=[f"<em>s</em>{j}" for j in range(5)],
            )
        )
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        raw_data=payload,
    )
    citations = await _build_citations_payload("search_documents", result, None)
    assert len(citations) == 50  # capped at _CITATION_MAX_HITS
    assert len(citations[0]["chunkText"]) == 800  # capped at _CITATION_CHUNK_MAX_CHARS
    assert len(citations[0]["highlight"]) == 3  # capped at _CITATION_HIGHLIGHT_MAX


@pytest.mark.asyncio
async def test_empty_hits_returns_none():
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        raw_data={"total": 0, "took_ms": 1, "hits": []},
    )
    assert await _build_citations_payload("search_documents", result, None) is None


@pytest.mark.asyncio
async def test_non_dict_data_returns_none():
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="search_documents",
        raw_data="plain text",
    )
    assert await _build_citations_payload("search_documents", result, None) is None


@pytest.mark.asyncio
async def test_end_to_end_event_bus_flow_with_real_loop():
    """A real agent run with a search_documents tool emits SSE tool_result
    events whose `citations` payload resolves `[[n]]` markers, both inline
    and persisted (artifact store) variants."""
    from courtier.agent.api.session_store import SessionStore
    from courtier.agent.api.sse_adapter import SSEAdapter
    from courtier.agent.core.event_bus import EventBus
    from courtier.agent.core.loop import agent_loop
    from courtier.agent.core.model import ToolCall
    from courtier.agent.core.state import AgentState
    from courtier.agent.testing import MockModelClient
    from courtier.agent.tools.protocol import ToolResult
    from courtier.agent.tools.registry import ToolRegistry

    session_id = "sess_c1ee00000001"
    with tempfile.TemporaryDirectory() as d:
        store = SessionStore(d)
        await store.create(session_id, "task", None)

        bus = EventBus()
        queue: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, session_id)
        adapter.start_listening(bus)

        class _SearchTool:
            name = "search_documents"
            description = "search"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data=_search_result())

        registry = ToolRegistry()
        registry.register(_SearchTool())

        try:
            model = MockModelClient(
                tool_calls=[ToolCall(id="c1", name="search_documents", arguments={})]
            )
            final = await agent_loop(
                state=AgentState.initial(task="trace"),
                model=model,
                tool_registry=registry,
                event_bus=bus,
                session_id=session_id,
            )
            assert final.status == "completed"
            await asyncio.sleep(0.1)

            payloads = []
            while not queue.empty():
                tag, line = queue.get_nowait()
                assert tag == "event"
                payloads.append(json.loads(line.replace("data: ", "").strip()))

            tool_result = [p for p in payloads if p.get("type") == "tool_result"]
            assert len(tool_result) == 1
            citations = tool_result[0].get("citations")
            assert citations is not None, "tool_result SSE event must carry citations"
            assert citations[0]["title"] == "来源1"
            assert citations[0]["resourceId"] == 1

            # Persisted steps keep citations for session restore.
            session = await store.get(session_id)
            assert session is not None
            step_tools = [t for s in session.steps or [] for t in s.tools]
            assert step_tools, "expected stored tool infos"
            assert any(t.name == "search_documents" and t.citations for t in step_tools)
        finally:
            adapter.stop_listening()
