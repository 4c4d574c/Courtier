"""Test parse plugin entry point handler logic."""

import asyncio
import json

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("parse")

# Import at module level so the pluginʼs own ``tools`` module is cached
# in sys.modules before other plugin tests can shadow it.
from plugins.docaudit.parse.entry import ParsePlugin  # noqa: E402


@pytest.mark.asyncio
async def test_parse_plugin_registers_with_system_prompt():
    plugin = ParsePlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "parse_document" in tool_names
    assert len(system_prompt) > 0


@pytest.mark.asyncio
async def test_parse_plugin_unknown_tool():
    plugin = ParsePlugin()

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

    responses = [json.loads(line) for line in output_lines if '"id"' in line]
    resp = next(r for r in responses if r.get("id") == 1)
    assert resp["result"]["success"] is False
    assert "Unknown tool" in resp["result"]["error"]


class TestParseDocumentSandbox:
    """Verify parse_document rejects paths outside the upload directory."""

    @pytest.mark.asyncio
    async def test_rejects_path_escape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        # Force a fresh import so the tool picks up the env var
        import importlib

        import plugins.docaudit.parse.tools as tools_mod

        importlib.reload(tools_mod)

        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        tool = tools_mod.ParseTool()
        result = await tool.execute(file_path=str(outside))
        assert result.success is False
        assert "Access denied" in result.error

    @pytest.mark.asyncio
    async def test_allows_file_in_upload_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))

        import importlib

        import plugins.docaudit.parse.tools as tools_mod

        importlib.reload(tools_mod)

        valid = tmp_path / "doc.pdf"
        valid.write_bytes(b"%PDF-1.4 fake pdf content")
        tool = tools_mod.ParseTool()
        # This will fail on parse (not a real PDF) but should NOT be an
        # "Access denied" error — it should pass the sandbox check.
        result = await tool.execute(file_path=str(valid))
        # We expect a docparse parse error, not an access-denied error.
        assert "Access denied" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_missing_upload_dir(self, monkeypatch):
        monkeypatch.delenv("COURTIER_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("DOCAUDIT_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("UPLOAD_DIR", raising=False)

        import importlib

        import plugins.docaudit.parse.tools as tools_mod

        importlib.reload(tools_mod)

        tool = tools_mod.ParseTool()
        result = await tool.execute(file_path="/tmp/test.pdf")
        assert result.success is False
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_prefers_courtier_upload_dir_over_legacy(self, tmp_path, monkeypatch):
        """COURTIER_UPLOAD_DIR takes precedence over DOCAUDIT_UPLOAD_DIR."""
        courtier_root = tmp_path / "courtier_uploads"
        legacy_root = tmp_path / "legacy_uploads"
        courtier_root.mkdir()
        legacy_root.mkdir()

        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(courtier_root))
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(legacy_root))
        monkeypatch.delenv("UPLOAD_DIR", raising=False)

        import importlib

        import plugins.docaudit.parse.tools as tools_mod

        importlib.reload(tools_mod)

        tool = tools_mod.ParseTool()

        # File inside the COURTIER upload dir should pass sandbox (parse will fail).
        valid = courtier_root / "doc.pdf"
        valid.write_bytes(b"%PDF-1.4 fake pdf content")
        result = await tool.execute(file_path=str(valid))
        assert "Access denied" not in (result.error or "")

        # File inside the legacy dir only should be denied.
        outside = legacy_root / "doc.pdf"
        outside.write_bytes(b"%PDF-1.4 fake pdf content")
        result = await tool.execute(file_path=str(outside))
        assert result.success is False
        assert "Access denied" in result.error


# ---------------------------------------------------------------------------
# sanitize / cache / params / schema / non-blocking behaviour
# ---------------------------------------------------------------------------


def _make_fake_doc(warnings: list[str] | None = None):
    """Minimal real Document so model_dump() behaves like production."""
    from docmodels import Document

    return Document(doc_id="d", total_page_num=1, warnings=warnings or [])


def _patch_parse(monkeypatch, calls: list[str] | None = None, parse_fn=None):
    """Patch docparse.parse; tools.py binds it lazily inside execute()."""
    import docparse

    def fake_parse(path: str):
        if calls is not None:
            calls.append(path)
        if parse_fn is not None:
            return parse_fn(path)
        return _make_fake_doc()

    monkeypatch.setattr(docparse, "parse", fake_parse)


class TestSanitize:
    """_sanitize must keep fields the format validator reads from the output."""

    def test_preserves_validator_fields(self):
        from plugins.docaudit.parse.tools import _sanitize

        paragraph = {
            "space_before": 1.5,
            "space_after": 2.0,
            "line_spacing": 1.25,
            "first_indent": 2.0,
            "left_indent": None,
            "right_indent": None,
            "outline_level": "body_text",
            "alignment": "justify",
            "elements": [
                {
                    "position": {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0},
                    "font": {"text": "正文", "font_size": 16.0},
                }
            ],
        }
        doc = {
            "schema_version": "1.0",
            "doc_id": "d",
            "save_path": "/host/secret/path",
            "pages": [
                {
                    "save_path": "/host/secret/page",
                    "page_no": 1,
                    "page_content": {"body": {"main_text": [paragraph]}},
                }
            ],
        }
        out = _sanitize(doc)

        para_out = out["pages"][0]["page_content"]["body"]["main_text"][0]
        for key in (
            "space_before",
            "space_after",
            "line_spacing",
            "first_indent",
            "left_indent",
            "right_indent",
        ):
            assert key in para_out, f"validator field {key} was stripped"
        element = para_out["elements"][0]
        assert element["position"]["x0"] == 1.0
        # New-model contract fields survive sanitizing.
        assert out["schema_version"] == "1.0"

    def test_strips_internal_fields(self):
        from plugins.docaudit.parse.tools import _sanitize

        out = _sanitize(
            {
                "save_path": "/host/path",
                "pages": [{"save_path": "/host/page", "page_no": 1}],
            }
        )
        assert "save_path" not in out
        assert "save_path" not in out["pages"][0]
        assert out["pages"][0]["page_no"] == 1

    def test_preserves_warnings(self):
        from plugins.docaudit.parse.tools import _sanitize

        out = _sanitize({"warnings": ["OCR degraded"], "pages": []})
        assert out["warnings"] == ["OCR degraded"]

    def test_binary_becomes_placeholder(self):
        from plugins.docaudit.parse.tools import _sanitize

        out = _sanitize({"blob": b"\x01\x02"})
        assert out["blob"].startswith("<binary:")


class TestParseCache:
    """Cache keyed by resolved path + (mtime_ns, size) fingerprint."""

    @pytest.mark.asyncio
    async def test_normalized_path_spellings_share_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        calls: list[str] = []
        _patch_parse(monkeypatch, calls)

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "sub" / "doc.pdf"
        target.parent.mkdir()
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        tool = tools_mod.ParseTool()
        r1 = await tool.execute(file_path=str(target))
        assert r1.success, r1.error
        # Same file via a non-normalized absolute spelling ...
        r2 = await tool.execute(file_path=str(tmp_path / "sub" / ".." / "sub" / "doc.pdf"))
        assert r2.success, r2.error
        # ... and via a relative spelling both hit the same cache entry.
        r3 = await tool.execute(file_path="sub/doc.pdf")
        assert r3.success, r3.error
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_file_overwrite_invalidates_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        calls: list[str] = []
        _patch_parse(monkeypatch, calls)

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        tool = tools_mod.ParseTool()
        assert (await tool.execute(file_path=str(target))).success
        assert len(calls) == 1

        # Overwriting the file changes the (mtime_ns, size) fingerprint.
        target.write_bytes(b"%PDF-1.4 fake pdf content, now longer")
        assert (await tool.execute(file_path=str(target))).success
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_cache_entries_are_not_shared_objects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        _patch_parse(monkeypatch)

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        tool = tools_mod.ParseTool()
        r1 = await tool.execute(file_path=str(target))
        # Mutating the caller-owned first result must not pollute the cache.
        r1.data["doc_id"] = "MUTATED"

        r2 = await tool.execute(file_path=str(target))
        assert r2 is not r1
        assert r2.data is not r1.data
        assert r2.data["doc_id"] == "d"

        # Mutating a cache-hit result must not pollute later hits either.
        r2.data["pages"].append({"junk": True})
        r3 = await tool.execute(file_path=str(target))
        assert r3.data["pages"] == []

    @pytest.mark.asyncio
    async def test_escape_path_never_touches_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        calls: list[str] = []
        _patch_parse(monkeypatch, calls)

        import plugins.docaudit.parse.tools as tools_mod

        outside = tmp_path.parent / "secret.pdf"
        outside.write_bytes(b"secret")

        tool = tools_mod.ParseTool()
        result = await tool.execute(file_path=str(outside))
        assert result.success is False
        assert "Access denied" in result.error
        assert calls == []
        assert tool._cache == {}


class TestParseWarnings:
    """Non-fatal parse warnings must reach the model without get_artifact."""

    @pytest.mark.asyncio
    async def test_warnings_surface_in_metadata_and_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        parse_warnings = ["第 3 页无文本层", "第 5 页 OCR 识别失败：超时"]
        _patch_parse(
            monkeypatch,
            parse_fn=lambda path: _make_fake_doc(warnings=parse_warnings),
        )

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        result = await tools_mod.ParseTool().execute(file_path=str(target))
        assert result.success, result.error
        # Canonical payload keeps warnings verbatim.
        assert result.data["warnings"] == parse_warnings
        # Visible summary layer: Chinese one-liner at data top level.
        assert result.data["warnings_summary"] == (
            "解析警告 2 条：第 3 页无文本层；第 5 页 OCR 识别失败：超时"
        )
        # Metadata carries the raw list into the observation envelope.
        assert result.metadata["warnings"] == parse_warnings
        # New-model contract field present in output.
        assert result.data["schema_version"] == "1.0"

    @pytest.mark.asyncio
    async def test_warnings_summary_truncates_beyond_five(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        warns = [f"第 {i} 页 OCR 识别失败" for i in range(1, 8)]
        _patch_parse(monkeypatch, parse_fn=lambda path: _make_fake_doc(warnings=warns))

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        result = await tools_mod.ParseTool().execute(file_path=str(target))
        assert result.success, result.error
        assert result.data["warnings_summary"].startswith("解析警告 7 条：")
        assert "等（共 7 条）" in result.data["warnings_summary"]
        # metadata keeps the full, untruncated list.
        assert result.metadata["warnings"] == warns

    @pytest.mark.asyncio
    async def test_no_warnings_means_no_summary_or_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))
        _patch_parse(monkeypatch)

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        result = await tools_mod.ParseTool().execute(file_path=str(target))
        assert result.success, result.error
        assert "warnings_summary" not in result.data
        assert "warnings" not in result.metadata


class TestExcludeNoneDump:
    """model_dump(exclude_none=True)：None 槽位/间距不再出现在输出里，
    但有语义的默认值（0 / False / "" / []）必须保留（不用 exclude_defaults，
    以免丢掉 page_no=0 等）。"""

    @staticmethod
    def _assert_no_none(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                assert value is not None, f"None leaked at key {key!r}"
                TestExcludeNoneDump._assert_no_none(value)
        elif isinstance(obj, list):
            for item in obj:
                TestExcludeNoneDump._assert_no_none(item)

    @pytest.mark.asyncio
    async def test_none_slots_dropped_defaults_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))

        from docmodels import (
            Body,
            Document,
            Font,
            LineElement,
            Margin,
            Page,
            PageContent,
            Paragraph,
            Position,
        )

        def make_doc():
            para = Paragraph(  # 间距/缩进字段全部 None（未提取）
                alignment="left",
                outline_level="body_text",
                elements=[
                    LineElement(
                        position=Position(x0=0.0, y0=0.0, x1=0.0, y1=0.0),
                        font=Font(font_family="仿宋", font_size=16.0, text="正文", line_no=0),
                    )
                ],
            )
            return Document(
                doc_id="d",
                total_page_num=1,
                pages=[
                    Page(
                        page_no=0,
                        page_content=PageContent(
                            body=Body(main_text=[para]),
                            margin=Margin(
                                top_margin=37.0,
                                bottom_margin=35.0,
                                left_margin=28.0,
                                right_margin=26.0,
                            ),
                        ),
                    )
                ],
            )

        _patch_parse(monkeypatch, parse_fn=lambda path: make_doc())

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "doc.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        result = await tools_mod.ParseTool().execute(file_path=str(target))
        assert result.success, result.error
        data = result.data

        # 全树无 None 值
        self._assert_no_none(data)

        # None 槽位/间距键不再出现
        page = data["pages"][0]
        page_content = page["page_content"]
        assert "copy_number" not in page_content["header"]
        assert "title" not in page_content["body"]
        para = page_content["body"]["main_text"][0]
        for key in (
            "space_before",
            "space_after",
            "line_spacing",
            "first_indent",
            "left_indent",
            "right_indent",
        ):
            assert key not in para, f"None spacing field {key} should be dropped"

        # 有语义的默认值保留
        assert page["page_no"] == 0
        assert para["alignment"] == "left"
        font = para["elements"][0]["font"]
        assert font["font_weight"] is False
        assert font["font_style"] is False
        assert data["warnings"] == []
        assert data["total_page_num"] == 1


class TestMissingParam:
    @pytest.mark.asyncio
    async def test_missing_file_path_returns_friendly_error(self):
        import plugins.docaudit.parse.tools as tools_mod

        result = await tools_mod.ParseTool().execute()
        assert result.success is False
        assert "缺少必填参数 file_path" in (result.error or "")

    @pytest.mark.asyncio
    async def test_non_string_file_path_returns_friendly_error(self):
        import plugins.docaudit.parse.tools as tools_mod

        result = await tools_mod.ParseTool().execute(file_path=123)
        assert result.success is False
        assert "缺少必填参数 file_path" in (result.error or "")


class TestOutputSchema:
    def test_type_names_are_valid_json_schema(self):
        import plugins.docaudit.parse.tools as tools_mod

        allowed = {"string", "integer", "number", "boolean", "array", "object", "null"}

        def walk(node: dict):
            if "type" in node:
                assert node["type"] in allowed, f"illegal JSON Schema type: {node['type']}"
            for value in node.values():
                if isinstance(value, dict):
                    walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            walk(item)

        walk(tools_mod.ParseTool.output_schema)

    def test_top_level_fields_match_document_model(self):
        import plugins.docaudit.parse.tools as tools_mod

        props = tools_mod.ParseTool.output_schema["properties"]
        for key in ("schema_version", "doc_id", "total_page_num", "pages", "warnings"):
            assert key in props
        assert "user_id" not in props

    def test_warnings_summary_declared(self):
        """warnings_summary 已注入 data 顶层，schema 必须同步声明（防漂移）。"""
        import plugins.docaudit.parse.tools as tools_mod

        props = tools_mod.ParseTool.output_schema["properties"]
        assert "warnings_summary" in props
        assert props["warnings_summary"]["type"] == "string"


class TestNonBlockingExecute:
    @pytest.mark.asyncio
    async def test_execute_does_not_block_event_loop(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setenv("DOCAUDIT_UPLOAD_DIR", str(tmp_path))

        def slow_parse(path: str):
            time.sleep(0.4)  # synchronous blocking parse
            return _make_fake_doc()

        _patch_parse(monkeypatch, parse_fn=slow_parse)

        import plugins.docaudit.parse.tools as tools_mod

        target = tmp_path / "big.pdf"
        target.write_bytes(b"%PDF-1.4 fake pdf content")

        tool = tools_mod.ParseTool()
        task = asyncio.create_task(tool.execute(file_path=str(target)))
        start = time.monotonic()
        # The event loop must stay responsive while the parse runs in a thread.
        await asyncio.sleep(0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 0.3, f"event loop was blocked for {elapsed:.2f}s during parse"
        assert not task.done()
        result = await task
        assert result.success, result.error
