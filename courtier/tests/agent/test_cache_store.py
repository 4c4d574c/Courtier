"""Tests for CacheStore — unified cache storage layer with JSON persist."""

from __future__ import annotations

import json
import os

import pytest

from courtier.agent.core.cache_store import CacheStore, PersistResult


@pytest.fixture(autouse=True)
def _reset_shared_ref_counters():
    """Global ref numbering must not leak across tests in one session."""
    from courtier.agent.core import cache_store as _cs

    _cs._SHARED_REF_COUNTERS.clear()
    yield
    _cs._SHARED_REF_COUNTERS.clear()


@pytest.fixture
def cache_store(tmp_path):
    """Create a CacheStore with a temporary cache directory."""
    return CacheStore(
        cache_dir=str(tmp_path / ".agent_cache"),
        large_output_threshold=3000,
    )


@pytest.mark.asyncio
class TestCacheStorePersistJson:
    """Tests for the JSON persist layer of CacheStore."""

    async def test_small_data_passes_through(self, cache_store):
        """Small data (below threshold) should not be persisted."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool")

        assert isinstance(result, PersistResult)
        assert result.data == data
        assert result.ref_id == ""
        assert result.persisted is False

    async def test_large_data_persisted_with_ref_id(self, cache_store):
        """Data exceeding threshold should be persisted with a ref_id."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")

        assert isinstance(result, PersistResult)
        assert result.persisted is True
        assert result.ref_id == "$ref:search_documents:1"

        marker = result.data
        assert isinstance(marker, dict)
        assert marker["__persisted_output__"] is True
        assert marker["ref_id"] == "$ref:search_documents:1"
        assert "file" in marker
        assert marker["size_chars"] > 3000
        assert "preview" in marker
        assert "data_shape" in marker
        assert marker["data_shape"]["type"] == "object"
        assert "text" in marker["data_shape"]["keys"]

    async def test_marker_data_shape_summarizes_structure(self, cache_store):
        """data_shape in persist marker gives LLM top-level structure hints."""
        data = {
            "metadata": {"author": "张三"},
            "pages": [{"num": 1}, {"num": 2}, {"num": 3}],
            "title": "测试文档",
            "padding": "x" * 3500,  # ensure above persist threshold
        }
        result = await cache_store.persist(data, "parse_document")
        shape = result.data["data_shape"]

        assert shape["type"] == "object"
        assert set(shape["keys"]) == {"metadata", "pages", "title", "padding"}
        assert shape["_pages_len"] == 3

    async def test_marker_data_shape_array(self, cache_store):
        """data_shape for array data shows length and sample item keys."""
        data = [{"name": "a", "val": 1}, {"name": "b", "val": 2}]
        result = await cache_store.persist(data, "some_tool", force=True)
        shape = result.data["data_shape"]

        assert shape["type"] == "array"
        assert shape["len"] == 2
        assert set(shape["item_keys"]) == {"name", "val"}

    async def test_marker_data_shape_string(self, cache_store):
        """data_shape for plain strings shows type and length."""
        result = await cache_store.persist("hello" * 1000, "text_tool")
        shape = result.data["data_shape"]

        assert shape["type"] == "string"
        assert shape["len"] == 5000

    async def test_identical_data_deduped_per_tool(self, cache_store):
        """Identical output from the same tool reuses the cache file (dedup)."""
        data = {"text": "x" * 3500}
        r1 = await cache_store.persist(data, "tool_a")
        r2 = await cache_store.persist(data, "tool_a")
        r3 = await cache_store.persist(data, "tool_b")

        assert r1.ref_id == "$ref:tool_a:1"
        # Dedup hit: same ref, no second file.
        assert r2.ref_id == "$ref:tool_a:1"
        assert r2.data.get("dedup_hit") is True
        assert r3.ref_id == "$ref:tool_b:1"

    async def test_ref_id_increments_for_different_data(self, cache_store):
        """Ref IDs still increment per tool when the content differs."""
        r1 = await cache_store.persist({"text": "a" * 3500}, "tool_a")
        r2 = await cache_store.persist({"text": "b" * 3500}, "tool_a")

        assert r1.ref_id == "$ref:tool_a:1"
        assert r2.ref_id == "$ref:tool_a:2"

    async def test_ref_map_tracks_all(self, cache_store):
        """ref_map should contain entries for all persisted ref_ids."""
        data = {"text": "x" * 3500}
        r1 = await cache_store.persist(data, "search_documents")
        r2 = await cache_store.persist(data, "other_tool")

        assert cache_store.ref_map[r1.ref_id] == r1.data["file"]
        assert cache_store.ref_map[r2.ref_id] == r2.data["file"]

    async def test_none_data_passes_through(self, cache_store):
        """None data should pass through without persisting."""
        result = await cache_store.persist(None, "test_tool")

        assert isinstance(result, PersistResult)
        assert result.data is None
        assert result.ref_id == ""
        assert result.persisted is False

    async def test_file_on_disk_matches_data(self, cache_store):
        """File written to disk should contain the correct serialized data."""
        data = {"items": [1, 2, 3], "name": "test"}
        result = await cache_store.persist(data, "test_tool", force=True)

        filepath = result.data["file"]
        with open(filepath, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        assert loaded == data

    async def test_content_type_json_in_marker(self, cache_store):
        """Marker should include content_type 'application/json' for dict/list data."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)

        marker = result.data
        assert marker["content_type"] == "application/json"

    async def test_schema_file_created(self, cache_store):
        """A .schema.json file should be created alongside the data file."""
        data = {"name": "test", "count": 42, "nested": {"key": "val"}}
        result = await cache_store.persist(data, "test_tool", force=True)

        data_filepath = result.data["file"]
        schema_filepath = data_filepath.replace(".json", ".schema.json")

        assert os.path.exists(schema_filepath)

        with open(schema_filepath, "r", encoding="utf-8") as f:
            schema = json.load(f)

        assert schema.get("name") == "str"
        assert schema.get("count") == "int"
        assert schema.get("nested.key") == "str"

    async def test_force_persists_small_data(self, cache_store):
        """force=True should persist even small data."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)

        assert result.persisted is True
        assert result.ref_id == "$ref:test_tool:1"
        assert result.data["__persisted_output__"] is True
        assert result.data["size_chars"] < 3000

    async def test_label_with_hyphen_sanitized_and_ref_id_matches_pattern(self, cache_store):
        """Label containing hyphens is sanitized and ref_id matches _REF_PATTERN."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="my-label")

        assert result.persisted is True
        # Hyphen replaced by underscore, ref_id must match _REF_PATTERN.
        assert result.ref_id == "$ref:test_tool:1:my_label"
        assert cache_store._REF_PATTERN.match(result.ref_id) is not None

    async def test_label_already_valid_passes_unchanged(self, cache_store):
        """A label that already matches the pattern should not be altered."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="my_label_2")

        assert result.persisted is True
        assert result.ref_id == "$ref:test_tool:1:my_label_2"
        assert cache_store._REF_PATTERN.match(result.ref_id) is not None

    async def test_label_with_special_chars_sanitized(self, cache_store):
        """Label with spaces, dots, etc. is fully sanitized."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="a.b c-d")

        assert result.persisted is True
        assert result.ref_id == "$ref:test_tool:1:a_b_c_d"
        assert cache_store._REF_PATTERN.match(result.ref_id) is not None

    async def test_label_starting_with_digit_prefixed(self, cache_store):
        """Label starting with a digit gets an underscore prefix."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="123label")

        assert result.persisted is True
        assert result.ref_id == "$ref:test_tool:1:_123label"
        assert cache_store._REF_PATTERN.match(result.ref_id) is not None


@pytest.mark.asyncio
class TestCacheStorePersistText:
    """Tests for plain text persist support."""

    async def test_plain_text_stored_as_txt(self, cache_store):
        """Large plain text string should be persisted as .txt file."""
        data = "This is plain text. " * 200  # ~4000 chars
        result = await cache_store.persist(data, "text_tool")

        assert result.persisted is True
        assert result.data["content_type"] == "text/plain"
        assert result.data["file"].endswith(".txt")

        with open(result.data["file"], "r", encoding="utf-8") as f:
            content = f.read()
        assert content == data

    async def test_string_of_json_dict_stored_as_json(self, cache_store):
        """A string that is valid JSON should be detected as application/json."""
        data = json.dumps({"key": "value"})
        result = await cache_store.persist(data, "test_tool", force=True)

        assert result.data["content_type"] == "application/json"
        assert result.data["file"].endswith(".json")

    async def test_non_json_non_dict_serialized_as_text(self, cache_store):
        """Non-JSON string data is persisted with text/plain content type."""
        data = "Hello, World! " * 200  # ~3200 chars
        content_type, serialized = cache_store._detect_content_type(data)
        assert content_type == "text/plain"
        assert serialized == data

        result = await cache_store.persist(data, "test_tool", force=True)
        assert result.data["content_type"] == "text/plain"
        assert result.data["file"].endswith(".txt")


@pytest.mark.asyncio
class TestCacheStoreResolveRefs:
    """Tests for recursive $ref resolution in resolve_refs()."""

    async def test_resolves_ref_string_to_disk_data(self, cache_store):
        """$ref string should be resolved to actual data from disk."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs({"query": ref_id})
        assert resolved["query"] == data

    def test_no_refs_returns_unchanged(self, cache_store):
        """kwargs with no $ref strings should return unchanged."""
        kwargs = {"query": "hello", "limit": 10}
        resolved = cache_store.resolve_refs(kwargs)
        assert resolved == kwargs

    def test_unknown_ref_keeps_original(self, cache_store):
        """Unknown $ref string should be kept as-is."""
        ref_str = "$ref:unknown_tool:99"
        resolved = cache_store.resolve_refs({"query": ref_str})
        assert resolved["query"] == ref_str

    async def test_resolves_nested_in_list(self, cache_store):
        """$ref strings nested inside lists should be resolved."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs({"items": [ref_id, "plain", ref_id]})
        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain"
        assert resolved["items"][2] == data

    async def test_resolves_nested_in_dict(self, cache_store):
        """$ref strings nested inside dicts should be resolved."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs({"outer": {"inner": ref_id}})
        assert resolved["outer"]["inner"] == data

    async def test_resolves_marker_dict(self, cache_store):
        """__persisted_output__ marker dict should be resolved."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")
        marker = result.data

        resolved = cache_store.resolve_refs({"query": marker})
        assert resolved["query"] == data

    async def test_file_missing_falls_back(self, cache_store):
        """$ref to a missing file should keep the original ref string."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "search_documents")
        ref_id = result.ref_id
        filepath = result.data["file"]
        os.remove(filepath)

        resolved = cache_store.resolve_refs({"query": ref_id})
        assert resolved["query"] == ref_id


@pytest.mark.asyncio
class TestCacheStoreTypeAdaptive:
    """Tests for type-adaptive resolution in resolve_refs()."""

    async def test_string_param_returns_text(self, cache_store):
        """When param type is 'string', dict data is serialized to string."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"query": ref_id},
            param_schemas={"query": {"type": "string"}},
        )
        assert isinstance(resolved["query"], str)
        assert json.loads(resolved["query"]) == data

    async def test_object_param_returns_parsed_json(self, cache_store):
        """When param type is 'object', JSON string on disk is parsed."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"query": ref_id},
            param_schemas={"query": {"type": "object"}},
        )
        assert isinstance(resolved["query"], dict)
        assert resolved["query"] == data

    async def test_text_content_object_param_falls_back(self, cache_store):
        """When type is 'object' but content is non-JSON text, falls back."""
        data = "Hello, World! " * 200  # Plain text, not JSON
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"query": ref_id},
            param_schemas={"query": {"type": "object"}},
        )
        # Falls back to raw string since it's not valid JSON
        assert isinstance(resolved["query"], str)
        assert resolved["query"] == data

    async def test_no_schema_returns_raw(self, cache_store):
        """No param_schemas: return raw data as-is from disk."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs({"query": ref_id})
        assert resolved["query"] == data

    async def test_number_param_converts_string_to_float(self, cache_store):
        """When param type is 'number', string data is converted to float."""
        data = "3.14"
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"value": ref_id},
            param_schemas={"value": {"type": "number"}},
        )
        assert isinstance(resolved["value"], float)
        assert resolved["value"] == 3.14

    async def test_integer_param_converts_string_to_int(self, cache_store):
        """When param type is 'integer', string data is converted to int."""
        data = "42"
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"value": ref_id},
            param_schemas={"value": {"type": "integer"}},
        )
        assert isinstance(resolved["value"], int)
        assert resolved["value"] == 42

    async def test_boolean_param_converts_truthy_strings(self, cache_store):
        """When param type is 'boolean', truthy strings convert to bool."""
        data = "true"
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"flag": ref_id},
            param_schemas={"flag": {"type": "boolean"}},
        )
        assert isinstance(resolved["flag"], bool)
        assert resolved["flag"] is True


@pytest.mark.asyncio
class TestCacheStoreLabel:
    """Tests for label support in persist()."""

    async def test_label_in_ref_id(self, cache_store):
        """Label should appear in ref_id."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="my_result")
        assert ":my_result" in result.ref_id

    async def test_label_in_marker(self, cache_store):
        """Label should appear in __persisted_output__ marker."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool", label="my_result")
        assert result.data["label"] == "my_result"


@pytest.mark.asyncio
class TestCacheStoreSourceMetadata:
    """Tests for source metadata in persist()."""

    async def test_source_in_marker(self, cache_store):
        """Source metadata should appear in __persisted_output__ marker."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(
            data,
            "test_tool",
            source_ref_id="$ref:parent:1",
            source_query="some query",
        )
        assert result.data["source"]["ref_id"] == "$ref:parent:1"
        assert result.data["source"]["query"] == "some query"

    async def test_source_partial(self, cache_store):
        """Marker should handle partial source metadata (only ref_id)."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(
            data,
            "test_tool",
            source_ref_id="$ref:parent:1",
        )
        assert "source" in result.data
        assert result.data["source"]["ref_id"] == "$ref:parent:1"
        assert "query" not in result.data["source"]


@pytest.mark.asyncio
class TestCacheStoreLoad:
    """Tests for load(), exists(), and get_info() methods."""

    async def test_load_returns_parsed_data(self, cache_store):
        """load() returns parsed JSON data for .json files."""
        data = {"key": "value"}
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        loaded = cache_store.load(ref_id)
        assert loaded == data

    async def test_load_text_file(self, cache_store):
        """load() returns raw text for .txt files."""
        data = "Hello, World! " * 200
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        loaded = cache_store.load(ref_id)
        assert loaded == data

    def test_load_unknown_returns_none(self, cache_store):
        """load() returns None for unknown refs."""
        loaded = cache_store.load("$ref:unknown:99")
        assert loaded is None

    async def test_exists_and_get_info(self, cache_store):
        """exists() and get_info() work correctly."""
        data = {"text": "x" * 3500}
        result = await cache_store.persist(data, "test_tool")
        ref_id = result.ref_id

        assert cache_store.exists(ref_id) is True
        assert cache_store.exists("$ref:nonexistent:99") is False

        info = cache_store.get_info(ref_id)
        assert info is not None
        assert info["ref_id"] == ref_id
        assert info["exists"] is True
        assert "size_bytes" in info
        assert "file" in info

        # After deleting file, exists should return False
        os.remove(result.data["file"])
        assert cache_store.exists(ref_id) is False
        assert cache_store.get_info(ref_id) is None


@pytest.mark.asyncio
class TestCacheStoreEndToEnd:
    """End-to-end tests for the full CacheStore + ToolRegistry pipeline."""

    async def test_plain_text_end_to_end(self, tmp_path):
        """Shell output -> persist as text -> resolve as string param."""
        store = CacheStore(cache_dir=str(tmp_path))
        shell_output = "file1.py\ndir/file2.py\n" + "x" * 5000
        persist_result = await store.persist(shell_output, "run_shell")
        ref_id = persist_result.ref_id
        assert ref_id.startswith("$ref:run_shell:")

        kwargs = {"content": ref_id}
        resolved = store.resolve_refs(kwargs, param_schemas={"content": {"type": "string"}})
        assert resolved["content"] == shell_output
        assert isinstance(resolved["content"], str)

    async def test_multiple_query_results_unique_ref_ids(self, tmp_path):
        """Multiple debug_tool calls produce unique, traceable ref_ids."""
        store = CacheStore(cache_dir=str(tmp_path))

        doc_data = {"docs": [{"id": 1, "text": "x" * 3000}, {"id": 2, "text": "y" * 3000}]}
        await store.persist(doc_data, "parse_document", force=True)

        # Persist two different query results
        r1 = await store.persist(
            {"id": 1},
            "debug_tool",
            force=True,
            source_ref_id="$ref:parse_document:1",
            source_query=".docs[0].id",
        )
        r2 = await store.persist(
            {"id": 2},
            "debug_tool",
            force=True,
            source_ref_id="$ref:parse_document:1",
            source_query=".docs[1].id",
        )

        assert r1.ref_id != r2.ref_id
        assert r1.data.get("source", {}).get("query") == ".docs[0].id"
        assert r2.data.get("source", {}).get("query") == ".docs[1].id"


@pytest.mark.asyncio
class TestCacheStoreObjectSchemaStrict:
    """_load_and_adapt should be strict about object vs array schema types."""

    async def test_object_param_rejects_list_data(self, cache_store):
        """When param type is 'object', list data should NOT pass through."""
        data = [{"name": "a"}, {"name": "b"}]
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"query": ref_id},
            param_schemas={"query": {"type": "object"}},
        )
        # List data for object schema should fall back to string or be rejected
        assert not isinstance(resolved["query"], list)

    async def test_array_param_accepts_list_data(self, cache_store):
        """When param type is 'array', list data should pass through."""
        data = [{"name": "a"}, {"name": "b"}]
        result = await cache_store.persist(data, "test_tool", force=True)
        ref_id = result.ref_id

        resolved = cache_store.resolve_refs(
            {"query": ref_id},
            param_schemas={"query": {"type": "array"}},
        )
        assert isinstance(resolved["query"], list)
        assert resolved["query"] == data


@pytest.mark.asyncio
class TestCacheStoreHashIndexSalt:
    """Version-salted dedup keys and bounded hash-index growth."""

    async def test_dedup_hit_within_same_salt(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path), cache_salt="v1")
        data = {"text": "x" * 4000}
        r1 = await store.persist(data, "parse_document")
        r2 = await store.persist(data, "parse_document")
        assert r2.data.get("dedup_hit") is True
        assert r2.ref_id == r1.ref_id

    async def test_different_salt_misses_old_entries(self, tmp_path):
        """After an upgrade (new salt) identical content is re-persisted
        instead of reusing the previous generation's cache file."""
        data = {"text": "x" * 4000}
        old_store = CacheStore(cache_dir=str(tmp_path), cache_salt="v1")
        r1 = await old_store.persist(data, "parse_document")

        new_store = CacheStore(cache_dir=str(tmp_path), cache_salt="v2")
        r2 = await new_store.persist(data, "parse_document")

        assert not r2.data.get("dedup_hit")
        # Re-persisted to a NEW cache file rather than reusing the old one.
        assert r2.data["file"] != r1.data["file"]
        assert new_store.load(r2.ref_id) == data

    async def test_default_salt_is_nonempty_and_deterministic(self, tmp_path):
        from courtier.agent.core.cache_store import _default_cache_salt

        salt = _default_cache_salt()
        assert isinstance(salt, str)
        assert salt == _default_cache_salt()

    async def test_save_prunes_stale_index_entries(self, tmp_path):
        """Writing the index drops entries whose cache file was removed, so
        .hash_index.json does not grow unboundedly."""
        store = CacheStore(cache_dir=str(tmp_path), cache_salt="v1")
        r1 = await store.persist({"text": "x" * 4000}, "parse_document")
        index_path = tmp_path / ".hash_index.json"
        index_before = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(index_before) == 1

        # GC removes the cache file; the next write must prune its entry.
        os.remove(store.ref_map[r1.ref_id])
        r2 = await store.persist({"text": "y" * 4000}, "search_documents")
        assert r2.persisted

        index_after = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(index_after) == 1
        assert all(v["ref_id"] == r2.ref_id for v in index_after.values())


@pytest.mark.asyncio
class TestRefStringAdaptation:
    """Persisted dicts adapted to a string parameter yield the document
    text, not the serialized wrapper object."""

    async def test_markdown_envelope_extracts_text(self, cache_store):
        data = {"markdown": "正文内容", "format": "docx"}
        result = await cache_store.persist(data, "convert_document", force=True)
        kwargs = cache_store.resolve_refs(
            {"text": result.ref_id},
            {"text": {"type": "string"}},
        )
        assert kwargs["text"] == "正文内容"

    async def test_chunk_text_field_extracted(self, cache_store):
        data = {"chunk_text": "条款正文", "title": "条例", "resource_id": 9}
        result = await cache_store.persist(data, "search_documents", force=True)
        kwargs = cache_store.resolve_refs(
            {"text": result.ref_id},
            {"text": {"type": "string"}},
        )
        assert kwargs["text"] == "条款正文"

    async def test_no_text_field_falls_back_to_json(self, cache_store):
        data = {"hits": [{"title": "t"}], "total": 1}
        result = await cache_store.persist(data, "search_documents", force=True)
        kwargs = cache_store.resolve_refs(
            {"text": result.ref_id},
            {"text": {"type": "string"}},
        )
        import json as _json

        parsed = _json.loads(kwargs["text"])
        assert parsed["total"] == 1

    async def test_object_param_still_gets_whole_dict(self, cache_store):
        data = {"markdown": "正文内容", "format": "docx"}
        result = await cache_store.persist(data, "convert_document", force=True)
        kwargs = cache_store.resolve_refs(
            {"payload": result.ref_id},
            {"payload": {"type": "object"}},
        )
        assert kwargs["payload"] == data
