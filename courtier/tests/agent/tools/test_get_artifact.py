"""Tests for GetArtifactTool."""

import pytest

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.tools.builtin.get_artifact import GetArtifactTool

from .conftest import _make as _make_artifact
from .conftest import _noop_progress


class TestGetArtifactTool:
    """Tests using non-$ref artifact IDs (typed projection path)."""

    @pytest.mark.asyncio
    async def test_get_by_artifact_id(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
            artifact_type="core.plain_text",
        )
        assert result.success
        assert result.data["value"] == "hello"

    @pytest.mark.asyncio
    async def test_get_specific_artifact_by_id(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        store.put(
            _make_artifact(
                "a2", data={"text": "world", "language": "zh", "source_scope": "full_document"}
            )
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a2",
            artifact_type="core.plain_text",
        )
        assert result.success
        assert result.data["value"] == "world"

    @pytest.mark.asyncio
    async def test_resolution_failure_for_unprojectable_type(self):
        """When the artifact can't be projected to the requested type, resolution fails."""
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
            artifact_type="docaudit.audit_finding_list",
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_id_not_found(self):
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=ArtifactStore(),
            id="x",
            artifact_type="core.plain_text",
        )
        assert not result.success
        assert "list_artifacts" in result.error

    @pytest.mark.asyncio
    async def test_store_none(self):
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=None,
            id="a1",
            artifact_type="core.plain_text",
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_label(self):
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
            artifact_type="core.plain_text",
            label="my_label",
        )
        assert result.success
        assert result.metadata.get("label") == "my_label"

    @pytest.mark.asyncio
    async def test_non_ref_id_without_artifact_type_errors(self):
        """Non-$ref id without artifact_type should return an error."""
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
        )
        assert not result.success
        assert "artifact_type" in result.error


class TestGetArtifactViaRef:
    """Tests using $ref IDs — the primary raw-data path."""

    @pytest.mark.asyncio
    async def test_fetch_raw_data_by_ref(self, tmp_path):
        """$ref without artifact_type returns raw persisted data."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"text": "persisted content", "count": 42}
        await store.persist(data, "test_tool", force=True)

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:test_tool:1",
        )
        assert result.success
        assert result.data == data
        assert result.metadata.get("result_id") == "$ref:test_tool:1"

    @pytest.mark.asyncio
    async def test_fetch_with_ref_and_artifact_type(self, tmp_path):
        """$ref + artifact_type resolves and projects the data."""
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
    async def test_ref_not_found(self, tmp_path):
        """Nonexistent $ref returns an error."""
        store = ArtifactStore(cache_dir=str(tmp_path))

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:nonexistent:99",
        )
        assert not result.success
        assert (
            "result_id" in result.error.lower()
            or "未找到" in result.error
            or "not found" in result.error.lower()
        )

    @pytest.mark.asyncio
    async def test_missing_id_param(self):
        """No id provided → error."""
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=ArtifactStore(),
        )
        assert not result.success
        assert "id" in result.error

    @pytest.mark.asyncio
    async def test_ref_with_query(self, tmp_path):
        """$ref with query returns matching excerpts."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {
            "items": [
                {"name": "apple", "color": "red"},
                {"name": "banana", "color": "yellow"},
                {"name": "cherry", "color": "red"},
            ]
        }
        await store.persist(data, "test_tool", force=True)

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:test_tool:1",
            query="red",
        )
        assert result.success
        assert result.data is not None
