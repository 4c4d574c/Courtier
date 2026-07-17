"""Tests for ListArtifactsTool."""

import pytest

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.tools.builtin.list_artifacts import ListArtifactsTool

from .conftest import _make_simple as _make_artifact
from .conftest import _noop_progress


class TestListArtifactsTool:
    @pytest.mark.asyncio
    async def test_empty_store(self):
        store = ArtifactStore()
        result = await ListArtifactsTool().execute(on_progress=_noop_progress, artifact_store=store)
        assert result.success
        assert result.data["artifacts"] == []
        assert result.data["count"] == 0

    @pytest.mark.asyncio
    async def test_all_candidates(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        store.put(_make_artifact("a2", "docaudit.paragraph_list"))
        result = await ListArtifactsTool().execute(on_progress=_noop_progress, artifact_store=store)
        assert result.success
        assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_filter_by_type(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        store.put(_make_artifact("a2", "docaudit.paragraph_list"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress, artifact_store=store, artifact_type="core.plain_text"
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_type"] == "core.plain_text"

    @pytest.mark.asyncio
    async def test_filter_by_role(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", role="document"))
        store.put(_make_artifact("a2", role="reference"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress, artifact_store=store, role="reference"
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["role"] == "reference"

    @pytest.mark.asyncio
    async def test_filter_by_subject(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", subject="current"))
        store.put(_make_artifact("a2", subject="search_results"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress, artifact_store=store, subject="search_results"
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["subject"] == "search_results"

    @pytest.mark.asyncio
    async def test_excludes_debug_only(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        debug = Artifact(
            artifact_id="a2",
            artifact_type="core.debug_view",
            data={"text": "debug"},
            metadata=ArtifactMetadata(
                created_by="debug_tool",
                semantic_role="debug",
                subject="debug",
                debug_only=True,
                projection_allowed=False,
            ),
        )
        store.put(debug)
        result = await ListArtifactsTool().execute(on_progress=_noop_progress, artifact_store=store)
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_type"] == "core.plain_text"

    def test_runtime_policy_max_consecutive_is_five(self):
        assert ListArtifactsTool.runtime_policy.max_consecutive == 5

    @pytest.mark.asyncio
    async def test_store_none(self):
        result = await ListArtifactsTool().execute(on_progress=_noop_progress, artifact_store=None)
        assert not result.success
        assert "不可用" in result.error

    @pytest.mark.asyncio
    async def test_combined_filters(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", "core.plain_text", "document", "current"))
        store.put(_make_artifact("a2", "core.plain_text", "reference", "search_results"))
        store.put(_make_artifact("a3", "core.text_collection", "document", "current"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            artifact_type="core.plain_text",
            role="document",
            subject="current",
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_id"] == "a1"
