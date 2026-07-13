"""Test parse plugin entry point handler logic."""
import asyncio
import json

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("parse")

# Import at module level so the pluginʼs own ``tools`` module is cached
# in sys.modules before other plugin tests can shadow it.
from plugins.shared.parse.entry import ParsePlugin  # noqa: E402


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

    req = json.dumps({
        "id": 1,
        "method": "tool.execute",
        "params": {"tool": "nonexistent_tool", "args": {}},
    })
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

        import plugins.shared.parse.tools as tools_mod

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

        import plugins.shared.parse.tools as tools_mod

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

        import plugins.shared.parse.tools as tools_mod

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

        import plugins.shared.parse.tools as tools_mod

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
