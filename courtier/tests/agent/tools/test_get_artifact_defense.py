"""Defensive validation tests for GetArtifactTool (unified id parameter)."""

import pytest

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.tools.builtin.get_artifact import GetArtifactTool

from .conftest import _make as _make_artifact
from .conftest import _noop_progress


class TestGetArtifactDefense:
    @pytest.mark.asyncio
    async def test_ref_without_artifact_type_returns_raw(self, tmp_path):
        """$ref id without artifact_type returns raw data — the most common path."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"text": "some content"}
        await store.persist(data, "parse_document", force=True)

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_document:1",
        )
        # Should succeed with raw data, no projection error.
        assert result.success
        assert result.data == data

    @pytest.mark.asyncio
    async def test_ref_with_artifact_type_runs_projection(self, tmp_path):
        """$ref id with artifact_type runs type projection."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"text": "hello world", "language": "en", "source_scope": "full_document"}
        await store.persist(data, "parse_document", force=True)

        store.register_cached_ref(
            ref_id="$ref:parse_document:1",
            artifact_type="core.plain_text",
            created_by="parse_document",
            data=data,
            role="primary_document",
            subject="current_upload",
            persist_to_disk=False,
        )

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_document:1",
            artifact_type="core.plain_text",
        )
        assert result.success
        assert result.data["value"] == "hello world"

    @pytest.mark.asyncio
    async def test_non_ref_without_artifact_type_is_rejected(self):
        """Non-$ref id without artifact_type should be rejected with clear error."""
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
        )
        assert not result.success
        assert "artifact_type" in result.error
