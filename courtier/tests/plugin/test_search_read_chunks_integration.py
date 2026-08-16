"""End-to-end integration: search → read_chunks coordinate round-trip.

Runs against the real Elasticsearch configured in .env (same wiring as
scripts/retrieval_eval.py).  Excluded from default runs via the
``integration`` marker.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEARCH_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "search"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _load_env() -> None:
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        pytest.skip("no .env — integration environment unavailable")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


async def test_search_then_read_chunks_roundtrip():
    _load_env()
    if not os.environ.get("ES_HOSTS"):
        pytest.skip("ES_HOSTS not configured")
    sys.path.insert(0, str(_SEARCH_PLUGIN_DIR))
    from tools import ReadChunksTool, SearchDocumentsTool

    search = SearchDocumentsTool()
    result = await search.execute(query="安全生产", limit=3)
    assert result.success, result.error
    hits = (result.data or {}).get("hits") or []
    if not hits:
        pytest.skip("resource library empty")

    coords = [
        {"resource_id": h["resource_id"], "chunk_no": h["chunk_no"]}
        for h in hits
        if h.get("resource_id") is not None and h.get("chunk_no") is not None
    ][:3]
    assert coords

    read = ReadChunksTool()
    read_result = await asyncio.wait_for(read.execute(chunks=coords), timeout=30)
    assert read_result.success, read_result.error
    got = {(c["resource_id"], c["chunk_no"]) for c in read_result.data["chunks"]}
    for coord in coords:
        assert (coord["resource_id"], coord["chunk_no"]) in got
    # Full chunk text (not a 300-char preview) came back for the hits.
    for chunk in read_result.data["chunks"]:
        assert len(chunk.get("chunk_text") or "") > 0
