"""Tests for ToolRegistry."""

from unittest.mock import MagicMock

import pytest

from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry


class TestToolRegistry:
    def test_register_and_get(self, registry_with_fake):
        tool = registry_with_fake.get("fake")
        assert tool.name == "fake"

    def test_register_duplicate_raises(self, registry_with_fake, fake_tool):
        with pytest.raises(ValueError, match="Duplicate"):
            registry_with_fake.register(fake_tool)

    def test_get_missing_raises(self, registry_with_fake):
        with pytest.raises(KeyError, match="Tool not found"):
            registry_with_fake.get("nonexistent")

    def test_list_tools(self, registry_with_fake):
        tools = registry_with_fake.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "fake"

    def test_get_schemas(self, registry_with_fake):
        schemas = registry_with_fake.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "fake"

    def test_list_tools_empty(self, empty_registry):
        assert empty_registry.list_tools() == []

    async def test_get_schemas_empty(self, empty_registry):
        assert empty_registry.get_schemas() == []

    @pytest.mark.asyncio
    async def test_execute(self, registry_with_fake):
        result = await registry_with_fake.execute("fake", value="hello")
        assert result.success
        assert result.raw_data == "hello"

    @pytest.mark.asyncio
    async def test_execute_default_value(self, registry_with_fake):
        result = await registry_with_fake.execute("fake")
        assert result.success
        assert result.raw_data == "default"

    @pytest.mark.asyncio
    async def test_execute_missing_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError, match="Tool not found"):
            await reg.execute("nonexistent")


class TestExecuteWithRefResolution:
    @pytest.mark.asyncio
    async def test_execute_resolves_refs(self, registry_with_fake):
        """When context_manager is provided, resolve_refs is called on kwargs."""
        mock_cm = MagicMock()
        mock_cm.resolve_refs.return_value = {"value": "resolved_data"}

        result = await registry_with_fake.execute(
            "fake", context_manager=mock_cm, value="$ref:some_tool:1"
        )

        mock_cm.resolve_refs.assert_called_once_with({"value": "$ref:some_tool:1"})
        assert result.success
        assert result.raw_data == "resolved_data"

    @pytest.mark.asyncio
    async def test_execute_without_context_manager(self, registry_with_fake):
        """When context_manager is None, kwargs pass through unchanged."""
        result = await registry_with_fake.execute("fake", value="hello")
        assert result.success
        assert result.raw_data == "hello"


@pytest.mark.asyncio
class TestToolRegistryWithCacheStore:
    @pytest.mark.asyncio
    async def test_execute_resolves_ref_before_execution(self, tmp_path):
        """When cache_store provided, $ref strings in kwargs are resolved."""
        from courtier.agent.core.cache_store import CacheStore

        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 5000}
        await store.persist(data, "parse_document")

        registry = ToolRegistry()

        class _FakeTool:
            name: str = "fake"
            description: str = "A fake tool for testing"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data=kwargs.get("value", "default"))

        registry.register(_FakeTool())

        # Pass $ref as value
        result = await registry.execute(
            "fake", artifact_store=store,
            value="$ref:parse_document:1",
        )
        assert result.success
        # The registry preserves the original resolved data in raw_data (not
        # the cache marker), so the parent agent can consume content directly.
        assert result.raw_data == data

    @pytest.mark.asyncio
    async def test_execute_without_cache_store_uses_context_manager(self):
        """When no cache_store, falls back to context_manager.resolve_refs."""
        mock_cm = MagicMock()
        mock_cm.resolve_refs.return_value = {"value": "resolved"}

        registry = ToolRegistry()

        class _FakeTool:
            name: str = "fake"
            description: str = "A fake tool for testing"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data=kwargs.get("value", "default"))

        registry.register(_FakeTool())

        result = await registry.execute(
            "fake", context_manager=mock_cm,
            value="$ref:some_tool:1",
        )
        mock_cm.resolve_refs.assert_called_once_with({"value": "$ref:some_tool:1"})
        assert result.success
        assert result.raw_data == "resolved"

    @pytest.mark.asyncio
    async def test_execute_resolves_with_type_adaptive(self, tmp_path):
        """$ref resolution uses param schemas for type adaptation."""
        from courtier.agent.core.cache_store import CacheStore

        store = CacheStore(cache_dir=str(tmp_path))
        text_data = "plain text content " + "x" * 5000
        await store.persist(text_data, "run_shell")

        # Create a tool that takes a string param named "content"
        class StringParamTool:
            name = "string_tool"
            description = "test"
            parameters = {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                },
                "required": ["content"],
            }
            async def execute(self, content="", context_manager=None, **kwargs):
                return ToolResult(success=True, data={"length": len(content)})

        registry = ToolRegistry()
        registry.register(StringParamTool())

        result = await registry.execute(
            "string_tool", artifact_store=store,
            content="$ref:run_shell:1",
        )
        assert result.success
        assert result.raw_data == {"length": len(text_data)}

    @pytest.mark.asyncio
    async def test_skip_ref_resolution_passes_ref_id_through(self, tmp_path):
        """Tools with skip_ref_resolution=True keep ref_id as raw $ref string."""
        from courtier.agent.core.cache_store import CacheStore

        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 5000}
        await store.persist(data, "parse_document")

        class ReadCacheLikeTool:
            name = "read_cache"
            description = "test"
            skip_ref_resolution = True
            parameters = {
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string"},
                },
                "required": ["ref_id"],
            }

            async def execute(self, ref_id="", context_manager=None, **kwargs):
                return ToolResult(
                    success=True,
                    data={"received_ref_id": ref_id, "is_string": isinstance(ref_id, str)},
                )

        registry = ToolRegistry()
        registry.register(ReadCacheLikeTool())

        result = await registry.execute(
            "read_cache", artifact_store=store,
            ref_id="$ref:parse_document:1",
        )
        assert result.success
        # ref_id should remain as the raw $ref string, NOT resolved to full JSON
        assert result.raw_data["received_ref_id"] == "$ref:parse_document:1"
        assert result.raw_data["is_string"] is True

    @pytest.mark.asyncio
    async def test_skip_ref_resolution_false_still_resolves(self, tmp_path):
        """Tools without skip_ref_resolution (default) still resolve $ref strings."""
        from courtier.agent.core.cache_store import CacheStore

        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 5000}
        await store.persist(data, "parse_document")

        class NormalTool:
            name = "normal_tool"
            description = "test"
            parameters = {
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string"},
                },
                "required": ["ref_id"],
            }

            async def execute(self, ref_id="", context_manager=None, **kwargs):
                return ToolResult(
                    success=True,
                    data={"received_ref_id": ref_id},
                )

        registry = ToolRegistry()
        registry.register(NormalTool())

        result = await registry.execute(
            "normal_tool", artifact_store=store,
            ref_id="$ref:parse_document:1",
        )
        assert result.success
        # Without skip_ref_resolution, the resolved $ref produces large data,
        # so the result is persisted. The marker data is a __persisted_output__ dict.
        assert isinstance(result.raw_data, dict)
        if result.raw_data.get("__persisted_output__"):
            # Data was persisted — test passes (resolution worked)
            pass
        else:
            # Small data case — check that ref_id was resolved to JSON, not kept as $ref
            received = result.raw_data.get("received_ref_id", "")
            assert received != "$ref:parse_document:1"
            assert "text" in received

    @pytest.mark.asyncio
    async def test_skip_ref_resolution_also_skips_context_manager_fallback(self):
        """When skip_ref_resolution=True, context_manager.resolve_refs is also skipped."""
        mock_cm = MagicMock()
        mock_cm.resolve_refs.return_value = {"ref_id": "resolved_data"}

        class ReadCacheLikeTool:
            name = "read_cache"
            description = "test"
            skip_ref_resolution = True
            parameters = {
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string"},
                },
                "required": ["ref_id"],
            }

            async def execute(self, ref_id="", context_manager=None, **kwargs):
                return ToolResult(
                    success=True,
                    data={"received_ref_id": ref_id},
                )

        registry = ToolRegistry()
        registry.register(ReadCacheLikeTool())

        result = await registry.execute(
            "read_cache", context_manager=mock_cm,
            ref_id="$ref:parse_document:1",
        )
        # context_manager.resolve_refs should NOT be called
        mock_cm.resolve_refs.assert_not_called()
        assert result.success
        assert result.raw_data["received_ref_id"] == "$ref:parse_document:1"


@pytest.mark.asyncio
class TestAutoRegisterArtifacts:
    """Tools without output_artifact_type should still have outputs auto-registered."""

    @pytest.mark.asyncio
    async def test_tool_without_output_artifact_type_gets_auto_registered(self, tmp_path):
        """Tool without output_artifact_type → auto-registered as core.cached_output."""
        from courtier.agent.artifacts.store import ArtifactStore

        store = ArtifactStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()

        class PlainTool:
            name: str = "plain_tool"
            description: str = "A tool without output_artifact_type"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data={"result": "done"})

        registry.register(PlainTool())

        result = await registry.execute(
            "plain_tool", artifact_store=store,
        )
        assert result.success

        # Verify artifact was auto-registered
        artifacts = list(store.list_all())
        assert len(artifacts) >= 1
        # The artifact type should be derived — core.cached_output for unknown tools
        artifact_types = {a.artifact_type for a in artifacts}
        assert "core.cached_output" in artifact_types

    @pytest.mark.asyncio
    async def test_parse_document_gets_correct_artifact_type(self, tmp_path):
        """parse_document → auto-registered as docaudit.parsed_document."""
        from courtier.agent.artifacts.store import ArtifactStore

        store = ArtifactStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()

        class ParseDocumentTool:
            name: str = "parse_document"
            description: str = "Parse a document"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data={"doc_id": "abc", "content": "test"})

        registry.register(ParseDocumentTool())

        result = await registry.execute(
            "parse_document", artifact_store=store,
        )
        assert result.success

        artifacts = list(store.list_all())
        artifact_types = {a.artifact_type for a in artifacts}
        assert "docaudit.parsed_document" in artifact_types

    @pytest.mark.asyncio
    async def test_skip_artifact_registration_flag(self, tmp_path):
        """Tools with skip_artifact_registration=True should not be auto-registered."""
        from courtier.agent.artifacts.store import ArtifactStore

        store = ArtifactStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()

        class SkippedTool:
            name: str = "skipped_tool"
            description: str = "A tool that opts out of artifact registration"
            skip_artifact_registration: bool = True
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data={"result": "done"})

        registry.register(SkippedTool())

        result = await registry.execute(
            "skipped_tool", artifact_store=store,
        )
        assert result.success

        # No artifact should be registered
        artifacts = list(store.list_all())
        assert len(artifacts) == 0


@pytest.mark.asyncio
class TestLargeResultPersistedOnce:
    """A large tool result must be persisted exactly once.

    The registry persists big payloads and records ``persisted_ref_id`` in the
    result metadata; the summarizer reuses that ref instead of writing the same
    content to disk a second time under a new ref_id.
    """

    @pytest.mark.asyncio
    async def test_large_result_single_persist_shared_ref(self, tmp_path):
        from courtier.agent.artifacts.store import ArtifactStore
        from courtier.agent.runtime.summarizer import ResultSummarizer

        store = ArtifactStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.configure_result_handling(
            summarizer=ResultSummarizer(artifact_store=store)
        )

        # > large_output_threshold (3000) so the registry persists, and
        # > summary_inline_max_chars (6000) so the summarizer would persist too.
        big_text = "x" * 7000

        class BigTool:
            name: str = "big_tool"
            description: str = "Returns a large payload"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data=big_text)

        registry.register(BigTool())

        result = await registry.execute("big_tool", artifact_store=store)

        assert result.success
        assert result.result_id == "$ref:big_tool:1"
        # Exactly one ref/file holds the payload — no second persist.
        assert list(store.ref_map) == ["$ref:big_tool:1"]
        assert store.load("$ref:big_tool:1") == big_text
        assert result.metadata["persisted_ref_id"] == result.result_id
        assert result.metadata["stored"]["result_id"] == result.result_id

    @pytest.mark.asyncio
    async def test_summarizer_persists_when_no_registry_ref(self, tmp_path):
        """Without a registry-level persist (no persist store), the summarizer
        keeps its existing behavior and persists large results itself."""
        from courtier.agent.artifacts.store import ArtifactStore
        from courtier.agent.runtime.summarizer import ResultSummarizer

        store = ArtifactStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.configure_result_handling(
            summarizer=ResultSummarizer(artifact_store=store)
        )

        big_text = "y" * 7000

        class BigTool:
            name: str = "big_tool"
            description: str = "Returns a large payload"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data=big_text)

        registry.register(BigTool())

        # No artifact_store/cache_store → registry does not persist; the
        # summarizer must persist on its own.
        result = await registry.execute("big_tool")

        assert result.success
        assert result.result_id == "$ref:big_tool:1"
        assert list(store.ref_map) == ["$ref:big_tool:1"]
        assert "persisted_ref_id" not in result.metadata


class _VersionedTool:
    name = "versioned_tool"
    description = "A versioned tool"
    version = "2.0.0"
    api_version = "2.0"
    deprecated = False
    replaced_by = None
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={"version": self.version})


class _DeprecatedTool:
    name = "versioned_tool"
    description = "Old version"
    version = "1.0.0"
    api_version = "1.0"
    deprecated = True
    replaced_by = "versioned_tool"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={"version": self.version})


class TestToolRegistryVersioning:
    def test_register_multiple_versions(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())
        reg.register(_VersionedTool())

        assert reg.list_available_versions("versioned_tool") == ["1.0.0", "2.0.0"]
        assert reg.get("versioned_tool") is reg.get("versioned_tool", version="2.0.0")

    def test_get_specific_version(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())
        reg.register(_VersionedTool())

        old = reg.get("versioned_tool", version="1.0.0")
        new = reg.get("versioned_tool", version="2.0.0")
        assert old.version == "1.0.0"
        assert new.version == "2.0.0"

    def test_latest_prefers_non_deprecated(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())
        reg.register(_VersionedTool())

        latest = reg.get("versioned_tool")
        assert latest.version == "2.0.0"

    def test_unregister_specific_version(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())
        reg.register(_VersionedTool())

        reg.unregister("versioned_tool", version="1.0.0")
        assert reg.list_available_versions("versioned_tool") == ["2.0.0"]
        with pytest.raises(KeyError):
            reg.get("versioned_tool", version="1.0.0")

    def test_unversioned_duplicate_still_raises(self):
        class _UnversionedTool:
            name = "unversioned"
            description = "test"
            parameters: dict = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, data="ok")

        reg = ToolRegistry()
        reg.register(_UnversionedTool())
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(_UnversionedTool())

    def test_get_schemas_deprecation_annotation(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())

        schemas = {s["function"]["name"]: s["function"]["description"] for s in reg.get_schemas()}
        assert "DEPRECATED" in schemas["versioned_tool"]
        assert "use versioned_tool" in schemas["versioned_tool"]

    def test_get_schemas_can_exclude_deprecated(self):
        reg = ToolRegistry()
        reg.register(_DeprecatedTool())

        schemas = reg.get_schemas(include_deprecated=False)
        assert len(schemas) == 0

    def test_get_tool_info(self):
        reg = ToolRegistry()
        reg.register(_VersionedTool())
        info = reg.get_tool_info("versioned_tool")
        assert info is not None
        assert info.version == "2.0.0"
        assert info.api_version == "2.0"
