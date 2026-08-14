"""Test search plugin entry point handler logic."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("search")

# Import at module level so the pluginʼs own ``tools`` module is cached
# in sys.modules before other plugin tests can shadow it.
from plugins.shared.search.entry import SearchPlugin  # noqa: E402

# Load the plugin's tools.py by path for direct _build_es_query assertions
# (same pattern as tests/agent/api/test_search_scope.py).
_TOOLS_PATH = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "search" / "tools.py"
_spec = importlib.util.spec_from_file_location("search_plugin_tools_semantics", _TOOLS_PATH)
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)


@pytest.mark.asyncio
async def test_search_plugin_registers_with_system_prompt():
    plugin = SearchPlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "search_documents" in tool_names
    assert len(system_prompt) > 0


@pytest.mark.asyncio
async def test_search_plugin_unknown_tool():
    plugin = SearchPlugin()

    output_lines: list[str] = []
    input_queue: asyncio.Queue[str] = asyncio.Queue()

    class TestWriter:
        def write(self, data: str | bytes):
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            output_lines.append(data)

        def flush(self):
            pass

    plugin._reader = input_queue
    plugin._writer = TestWriter()

    req = json.dumps(
        {
            "id": 1,
            "method": "tool.execute",
            "params": {"tool": "nonexistent_tool", "args": {}},
        }
    )
    input_queue.put_nowait(req)
    input_queue.put_nowait("")  # EOF

    await plugin.run()

    # Parse responses, find the one with id=1
    responses = [json.loads(line) for line in output_lines if '"id"' in line]
    resp = next(r for r in responses if r.get("id") == 1)
    assert resp["result"]["success"] is False
    assert "Unknown tool" in resp["result"]["error"]


def _multi_match(body: dict) -> dict:
    (clause,) = [c for c in body["query"]["bool"]["must"] if "multi_match" in c]
    return clause["multi_match"]


class TestBuildEsQuerySemantics:
    def test_free_text_requires_and_operator(self):
        body = tools._build_es_query("安全生产主体责任")
        mm = _multi_match(body)
        assert mm["operator"] == "and"
        assert "minimum_should_match" not in mm

    def test_multi_word_query_uses_minimum_should_match(self):
        body = tools._build_es_query("安全生产 主体责任 落实 煤矿")
        mm = _multi_match(body)
        assert mm["minimum_should_match"] == "70%"
        assert "operator" not in mm

    def test_long_unspaced_query_uses_minimum_should_match(self):
        body = tools._build_es_query("各单位应当落实安全生产主体责任并定期组织应急演练")
        mm = _multi_match(body)
        assert mm["minimum_should_match"] == "70%"
        assert "operator" not in mm

    def test_free_text_adds_phrase_booster(self):
        body = tools._build_es_query("安全生产主体责任")
        (booster,) = body["query"]["bool"]["should"]
        assert booster["match_phrase"]["chunk_text"] == {
            "query": "安全生产主体责任",
            "slop": 2,
            "boost": 2.0,
        }

    def test_quoted_phrase_stays_in_must(self):
        body = tools._build_es_query('关于"安全生产"的通知')
        must = body["query"]["bool"]["must"]
        (phrase,) = [c for c in must if "match_phrase" in c]
        assert phrase["match_phrase"]["chunk_text"] == {"query": "安全生产", "slop": 0}

    def test_default_fields_are_weighted(self):
        body = tools._build_es_query("安全生产")
        mm = _multi_match(body)
        assert mm["fields"] == ["chunk_text^1", "title^3"]

    def test_custom_fields_respected_without_weights(self):
        body = tools._build_es_query("安全生产", search_fields=["chunk_text", "tags"])
        mm = _multi_match(body)
        assert mm["fields"] == ["chunk_text", "tags"]

    def test_sort_is_deterministic(self):
        body = tools._build_es_query("安全生产")
        assert body["sort"] == [
            {"_score": {"order": "desc"}},
            {"publish_date": {"order": "desc", "unmapped_type": "date"}},
            {"resource_id": {"order": "asc", "unmapped_type": "long"}},
            {"chunk_no": {"order": "asc", "unmapped_type": "long"}},
        ]

    def test_highlight_uses_plain_field_names(self):
        body = tools._build_es_query("安全生产")
        assert set(body["highlight"]["fields"]) == {"chunk_text", "title"}

    def test_short_query_keeps_fuzziness(self):
        body = tools._build_es_query("通知")
        assert _multi_match(body)["fuzziness"] == "AUTO"

    def test_synonym_expansion_adds_phrase_boosters(self):
        body = tools._build_es_query("安监局的通知")
        should = body["query"]["bool"]["should"]
        expanded = [c["match_phrase"]["chunk_text"]["query"] for c in should]
        assert "安全生产监督管理局" in expanded
        # The original free text keeps its own (stronger) phrase booster.
        assert {"query": "安监局的通知", "slop": 2, "boost": 2.0} in [
            c["match_phrase"]["chunk_text"] for c in should
        ]

    def test_synonym_expansion_does_not_alter_must(self):
        body = tools._build_es_query("安监局的通知")
        mm = _multi_match(body)
        assert mm["query"] == "安监局的通知"
