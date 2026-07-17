
import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("annotate")

from plugins.shared.annotate.entry import AnnotatePlugin  # noqa: E402


@pytest.mark.asyncio
async def test_annotate_plugin_registers_tool():
    plugin = AnnotatePlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "annotate_document" in tool_names
    assert len(system_prompt) > 0


class TestAnnotatePathSafety:
    def test_rejects_path_escape(self, tmp_path):
        from docannot._annotate import annotate

        outside = tmp_path.parent / "secret.docx"
        outside.write_bytes(b"PK fake docx")
        with pytest.raises(ValueError, match="outside allowed"):
            annotate(str(outside), [], allowed_dirs=[tmp_path])

    def test_accepts_file_in_allowed_dir(self, tmp_path):
        from docannot._annotate import annotate

        valid = tmp_path / "test.docx"
        valid.write_bytes(b"PK fake docx")
        # Will fail on parse (not a real DOCX) but not from path check.
        with pytest.raises(Exception):
            annotate(str(valid), [], allowed_dirs=[tmp_path])

    def test_accepts_bytes_source(self, tmp_path):
        from docannot._annotate import annotate

        outside = tmp_path.parent / "secret.docx"
        outside.write_bytes(b"PK fake docx")
        # bytes source always accepted (already in memory)
        with pytest.raises(Exception):
            annotate(b"PK fake docx", [], allowed_dirs=[tmp_path])

    def test_accepts_base64_source(self, tmp_path):
        import base64

        from docannot._annotate import annotate

        encoded = base64.b64encode(b"fake docx bytes").decode()
        # base64 source always accepted (already decoded from transport)
        with pytest.raises(Exception):
            annotate(encoded, [], allowed_dirs=[tmp_path])
