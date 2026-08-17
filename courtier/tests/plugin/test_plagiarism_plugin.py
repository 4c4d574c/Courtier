import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("detect_plagiarism")

from plugins.docaudit.audit.detect_plagiarism.entry import PlagiarismPlugin  # noqa: E402


@pytest.mark.asyncio
async def test_plagiarism_plugin_registers():
    plugin = PlagiarismPlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "compute_similarity" not in tool_names
    assert "compute_dynamic_threshold" not in tool_names
    assert "detect_plagiarism" in tool_names
