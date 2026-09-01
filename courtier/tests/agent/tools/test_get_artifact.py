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
        await store.persist(data, "parse_layout", force=True)

        store.register_cached_ref(
            ref_id="$ref:parse_layout:1",
            artifact_type="core.plain_text",
            created_by="parse_layout",
            data=data,
            role="primary_document",
            subject="current_upload",
            persist_to_disk=False,
        )

        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
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


def _para(text: str) -> dict:
    """构造与 docparse 输出一致的段落块（elements[].font.text）。"""
    return {"elements": [{"font": {"text": text}}]}


PARSED_DOC_DATA = {
    "schema_version": "1.0",
    "source": "docx",
    "doc_id": "d1",
    "total_page_num": 1,
    "pages": [
        {
            "page_no": 0,
            "page_content": {
                "body": {
                    "title": _para("关于申请百京市智慧城市建设项目资金支持的请示"),
                    "main_text": [
                        _para("现将项目有关情况请示如下。"),
                        _para("一、项目背景与建设必要性"),
                        _para("（一）国家战略导向与政策机遇"),
                        _para("当前，以大数据、人工智能为代表的新一代信息技术加速创新。"),
                    ],
                }
            },
        }
    ],
}


class TestGetArtifactRefAutoProjection:
    """$ref + 投影参数（无 artifact_type）自动推断目标类型。"""

    def _register_parsed_doc(self, store: ArtifactStore, ref_id: str = "$ref:parse_layout:1"):
        store.register_cached_ref(
            ref_id=ref_id,
            artifact_type="docaudit.parsed_layout",
            created_by="parse_layout",
            data=PARSED_DOC_DATA,
        )

    @pytest.mark.asyncio
    async def test_ref_materialize_string_with_body_scope(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register_parsed_doc(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
            materialize_as="string",
            source_scope="body",
        )
        assert result.success, result.error
        value = result.data["value"]
        assert isinstance(value, str)
        # 正文段落按阅读序拼接；标题不在 body 范围
        expected = "\n".join(
            p["elements"][0]["font"]["text"]
            for p in PARSED_DOC_DATA["pages"][0]["page_content"]["body"]["main_text"]
        )
        assert value == expected

    @pytest.mark.asyncio
    async def test_ref_materialize_string_default_scope(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register_parsed_doc(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
            materialize_as="string",
        )
        assert result.success, result.error
        value = result.data["value"]
        assert isinstance(value, str)
        title = PARSED_DOC_DATA["pages"][0]["page_content"]["body"]["title"]["elements"][0]["font"]["text"]
        assert title in value
        assert "现将项目有关情况请示如下。" in value

    @pytest.mark.asyncio
    async def test_ref_source_scope_alone_projects_to_text(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register_parsed_doc(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
            source_scope="body",
        )
        assert result.success, result.error
        value = result.data["value"]
        assert isinstance(value, str)
        assert "关于申请百京市" not in value  # 标题不在 body
        assert "一、项目背景与建设必要性" in value

    @pytest.mark.asyncio
    async def test_ref_raw_read_without_projection_params(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register_parsed_doc(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
        )
        assert result.success, result.error
        assert result.data == PARSED_DOC_DATA  # raw-read 行为不变

    @pytest.mark.asyncio
    async def test_ref_projection_inference_failure_reports_error(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        store.register_cached_ref(
            ref_id="$ref:some_tool:1",
            artifact_type="docaudit.audit_report",
            created_by="some_tool",
            data={"report": "x"},
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:some_tool:1",
            materialize_as="string",
        )
        assert not result.success
        assert "无法投影" in result.error
        assert "projectable_to_types" in result.error

    @pytest.mark.asyncio
    async def test_untyped_persisted_ref_projection_reports_error(self, tmp_path):
        """持久化存在但注册表无类型信息：投影请求必须报错而非静默返回原始数据。"""
        store = ArtifactStore(cache_dir=str(tmp_path))
        await store.persist({"markdown": "text"}, "convert_document", force=True)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:convert_document:1",
            materialize_as="string",
        )
        assert not result.success
        assert "类型化工件" in result.error


class TestGetArtifactOutlineParsedDocument:
    """parse_layout 产物的大纲/按节读取（Phase 4）。"""

    def _register(self, store: ArtifactStore):
        store.register_cached_ref(
            ref_id="$ref:parse_layout:1",
            artifact_type="docaudit.parsed_layout",
            created_by="parse_layout",
            data=PARSED_DOC_DATA,
        )

    @pytest.mark.asyncio
    async def test_outline_for_parsed_layout(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
            outline=True,
        )
        assert result.success, result.error
        titles = [s["title"] for s in result.data["outline"]]
        assert "一、项目背景与建设必要性" in titles
        assert "（一）国家战略导向与政策机遇" in titles

    @pytest.mark.asyncio
    async def test_section_read_for_parsed_layout(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        self._register(store)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:parse_layout:1",
            section="一、项目背景与建设必要性",
        )
        assert result.success, result.error
        content = result.data["content"]
        assert "（一）国家战略导向与政策机遇" in content
        assert "当前，以大数据" in content
        assert "关于申请百京市" not in content  # 标题段不混入该节

    @pytest.mark.asyncio
    async def test_outline_failure_reports_honest_error(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        store.register_cached_ref(
            ref_id="$ref:search_documents:1",
            artifact_type="docaudit.search_results",
            created_by="search_documents",
            data={"hits": [{"chunk_text": "x"}], "total": 1},
        )
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:search_documents:1",
            outline=True,
        )
        assert not result.success
        assert "不支持大纲" in result.error
        assert "不含可解析的文本内容" not in result.error


class TestGetArtifactCapFallback:
    """仅尺寸上限类约束且无法投影时：原始数据满足上限即直接返回。

    对应 2026-08-16 第二轮日志 sess_4ee0d4de0fd1 子代理 turn_003 场景：
    correct_text 结果（core.cached_output，不可投影）+ max_chars=50000。
    """

    def _register_cached(self, store: ArtifactStore, data):
        store.register_cached_ref(
            ref_id="$ref:correct_text:4",
            artifact_type="core.cached_output",
            created_by="correct_text",
            data=data,
            debug_only=True,
        )

    @pytest.mark.asyncio
    async def test_max_chars_within_cap_returns_raw(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"results": [{"source": "x" * 8000, "target": "x" * 8000, "errors": []}]}
        self._register_cached(store, data)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:correct_text:4",
            max_chars=50000,
        )
        assert result.success, result.error
        assert result.data == data

    @pytest.mark.asyncio
    async def test_max_chars_exceeding_cap_reports_error(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"results": [{"source": "x" * 8000, "target": "x" * 8000, "errors": []}]}
        self._register_cached(store, data)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:correct_text:4",
            max_chars=1000,
        )
        assert not result.success
        assert "无法投影" in result.error

    @pytest.mark.asyncio
    async def test_semantic_constraint_still_strict(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        data = {"results": [{"source": "x" * 8000, "target": "x" * 8000, "errors": []}]}
        self._register_cached(store, data)
        result = await GetArtifactTool().execute(
            on_progress=_noop_progress,
            artifact_store=store,
            id="$ref:correct_text:4",
            source_scope="body",
        )
        assert not result.success
        assert "无法投影" in result.error
