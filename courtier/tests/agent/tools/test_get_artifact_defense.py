"""Defensive validation tests for GetArtifactTool (unified id parameter)."""

import pytest

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.tools.builtin.get_artifact import GetArtifactTool

from .conftest import _make as _make_artifact
from .conftest import _noop_progress


@pytest.fixture(autouse=True)
def _reset_shared_ref_counters():
    from courtier.agent.core import cache_store as _cs

    _cs._SHARED_REF_COUNTERS.clear()
    yield
    _cs._SHARED_REF_COUNTERS.clear()


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
    async def test_non_ref_without_artifact_type_returns_data(self):
        """Non-$ref id without artifact_type returns stored data directly.

        The artifact id already identifies a typed artifact; without a target
        type there is nothing to convert, so the raw stored data is returned.
        """
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
        )
        assert result.success
        assert result.data == {"text": "hello", "language": "zh", "source_scope": "full_document"}

    @pytest.mark.asyncio
    async def test_artifact_id_alias_accepted(self):
        """list_artifacts outputs an ``artifact_id`` field; passing it under
        that name (instead of ``id``) is tolerated as an alias."""
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            artifact_id="a1",
        )
        assert result.success
        assert result.data["text"] == "hello"

    @pytest.mark.asyncio
    async def test_unknown_non_ref_id_without_artifact_type(self):
        """Unknown non-$ref id returns a clear error with id-filling guidance."""
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=ArtifactStore(),
            id="does_not_exist",
        )
        assert not result.success
        assert "未找到 id=does_not_exist" in result.error
        assert "artifact_id" in result.error

    @pytest.mark.asyncio
    async def test_ref_not_found_includes_visibility_hint(self, tmp_path):
        """Not-found $ref errors hint at scope visibility and list_artifacts."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_document:9",
        )
        assert not result.success
        assert "可见范围" in result.error
        assert "list_artifacts" in result.error

    def test_id_is_required_in_schema(self):
        """The parameters schema marks id as required so models don't omit it."""
        assert GetArtifactTool.parameters.get("required") == ["id"]
