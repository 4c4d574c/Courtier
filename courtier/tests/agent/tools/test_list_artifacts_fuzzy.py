"""Tests for fuzzy role/subject matching in ListArtifactsTool."""

import pytest

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.tools.builtin.list_artifacts import ListArtifactsTool

from .conftest import _make_simple as _make_artifact
from .conftest import _noop_progress


class TestListArtifactsFuzzy:
    @pytest.mark.asyncio
    async def test_fuzzy_role_match(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", role="primary_document"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress, artifact_store=store, role="document"
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["role"] == "primary_document"

    @pytest.mark.asyncio
    async def test_fuzzy_subject_match(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", subject="current_upload"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress, artifact_store=store, subject="current"
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["subject"] == "current_upload"

    @pytest.mark.asyncio
    async def test_exact_match_still_works(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", role="document", subject="current"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress,
            artifact_store=store, role="document", subject="current",
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["role"] == "document"
        assert result.data["artifacts"][0]["subject"] == "current"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1", role="primary_document", subject="current_upload"))
        result = await ListArtifactsTool().execute(
            on_progress=_noop_progress,
            artifact_store=store, role="nonexistent", subject="nonexistent",
        )
        assert result.success
        assert result.data["count"] == 0
        assert result.data["artifacts"] == []
