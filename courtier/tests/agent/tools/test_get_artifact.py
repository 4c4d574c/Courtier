"""Tests for GetArtifactTool."""

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
    async def test_non_ref_id_without_artifact_type_returns_data(self):
        """Non-$ref id without artifact_type returns the stored data directly."""
        store = ArtifactStore()
        store.put(_make_artifact("a1"))
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="a1",
        )
        assert result.success
        assert result.data == {"text": "hello", "language": "zh", "source_scope": "full_document"}
        assert result.metadata.get("artifact_id") == "a1"


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


class TestRefVersionFallback:
    """Typed-artifact id vs persisted-ref id mismatch resolves via prefix.

    Regression: the persisted ref (``$ref:convert_document:1``, from
    cache_store numbering) is read successfully, but the typed artifact in
    the store lives under ``$ref:convert_document:latest``.  Without the
    prefix fallback get_artifact registered an untyped cached_output and
    projection to core.plain_text failed ("Could not resolve required field
    value").
    """

    @pytest.mark.asyncio
    async def test_persisted_ref_projects_latest_document_markdown(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        # Persisted layer: numbered ref on disk.
        data = {"markdown": "# 标题\n\n正文", "format": "docx"}
        await store.persist(data, "convert_document", force=True)
        # Typed layer: ToolProxy artifacts live under the :latest id.
        store.put(
            _make_artifact(
                "$ref:convert_document:latest",
                atype="core.document_markdown",
                data=data,
            )
        )

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            artifact_type="core.plain_text",
        )
        assert result.success, result.error
        assert result.data["value"] == "# 标题\n\n正文"

    @pytest.mark.asyncio
    async def test_persisted_ref_without_type_returns_raw_data(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"markdown": "正文", "format": "docx"}
        await store.persist(data, "convert_document", force=True)
        store.put(
            _make_artifact(
                "$ref:convert_document:latest",
                atype="core.document_markdown",
                data=data,
            )
        )

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
        )
        assert result.success
        assert result.data["markdown"] == "正文"


class TestRegistryFirstRefRouting:
    """list_artifacts advertises ``$ref:<tool>:latest`` / ``inline:<tool>:<hash>``
    ids for unpersisted outputs — those live only in the typed registry, so
    get_artifact must look there before the persistence read path."""

    @pytest.mark.asyncio
    async def test_latest_alias_resolves_from_registry(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"markdown": "# 标题\n\n正文", "format": "docx"}
        store.put(
            _make_artifact(
                "$ref:convert_document:latest",
                atype="core.document_markdown",
                data=data,
            )
        )
        # No disk/ES record exists for this id — the old code routed
        # straight to artifact_store.read() and reported not found.
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:latest",
        )
        assert result.success, result.error
        assert result.data == data

    @pytest.mark.asyncio
    async def test_latest_alias_with_artifact_type_projects(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        store.put(
            _make_artifact(
                "$ref:convert_document:latest",
                atype="core.document_markdown",
                data={"markdown": "# 标题\n\n正文", "format": "docx"},
            )
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:latest",
            artifact_type="core.plain_text",
        )
        assert result.success, result.error
        assert result.data["value"] == "# 标题\n\n正文"

    @pytest.mark.asyncio
    async def test_inline_content_addressed_id_resolves(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        store.put(
            _make_artifact(
                "inline:convert_document:abc123def456",
                atype="core.document_markdown",
                data={"markdown": "内容", "format": "docx"},
            )
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="inline:convert_document:abc123def456",
        )
        assert result.success, result.error
        assert result.data["markdown"] == "内容"


class TestStructuredRead:
    """outline/section structured reading over long documents."""

    LONG_MD = (
        "# 一、项目背景\n"
        "背景内容第一段。\n背景内容第二段。\n\n"
        "## 建设方案\n"
        "方案正文A。\n\n"
        "## 经费测算\n"
        "经费正文B。\n\n"
        "# 二、实施计划\n"
        "计划正文C。\n"
    )

    @pytest.mark.asyncio
    async def test_outline_returns_sections(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": self.LONG_MD, "format": "docx"}, "convert_document", force=True)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            outline=True,
        )
        assert result.success, result.error
        titles = [s["title"] for s in result.data["outline"]]
        assert titles == ["一、项目背景", "建设方案", "经费测算", "二、实施计划"]
        assert result.data["total_chars"] == len(self.LONG_MD)

    @pytest.mark.asyncio
    async def test_section_by_title_contains_subsections(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": self.LONG_MD, "format": "docx"}, "convert_document", force=True)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            section="一、项目背景",
        )
        assert result.success, result.error
        assert "背景内容第一段" in result.data["content"]
        assert "方案正文A" in result.data["content"]  # subsections included
        assert "实施计划" not in result.data["content"]  # sibling chapter excluded
        assert result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_section_by_index(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": self.LONG_MD, "format": "docx"}, "convert_document", force=True)
        for selector in ("3", "三", "第三节"):
            result = await GetArtifactTool().execute(
                on_progress=_noop_progress,
                artifact_store=store,
                id="$ref:convert_document:1",
                section=selector,
            )
            assert result.success, result.error
            assert result.data["section_title"] == "经费测算"

    @pytest.mark.asyncio
    async def test_section_miss_lists_candidates(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": self.LONG_MD, "format": "docx"}, "convert_document", force=True)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            section="不存在的节",
        )
        assert not result.success
        assert "经费测算" in result.error
        assert "一、项目背景" in result.error

    @pytest.mark.asyncio
    async def test_truncation_flag_on_oversized_section(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist(
            {"markdown": "# 大节\n" + "字" * 20_000, "format": "docx"},
            "convert_document",
            force=True,
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            section="大节",
            max_tokens=500,
        )
        assert result.success, result.error
        assert result.data["truncated"] is True
        assert len(result.data["content"]) <= 500 * 4
        assert result.data["section_chars"] > 19_000

    @pytest.mark.asyncio
    async def test_registry_first_path_supports_outline(self, tmp_path):
        """Inline artifacts (registry-only) also serve outline reads."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        store.put(
            _make_artifact(
                "inline:convert_document:abc123",
                atype="core.document_markdown",
                data={"markdown": self.LONG_MD, "format": "docx"},
            )
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="inline:convert_document:abc123",
            outline=True,
        )
        assert result.success, result.error
        assert len(result.data["outline"]) == 4

    @pytest.mark.asyncio
    async def test_plain_read_unchanged(self, tmp_path):
        """Without outline/section, behavior is byte-identical to before."""
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": self.LONG_MD, "format": "docx"}, "convert_document", force=True)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
        )
        assert result.success, result.error
        assert result.data == {"markdown": self.LONG_MD, "format": "docx"}
