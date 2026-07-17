
import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("template")

from plugins.shared.template.entry import TemplatePlugin  # noqa: E402


@pytest.mark.asyncio
async def test_template_plugin_registers():
    plugin = TemplatePlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "load_template" in tool_names
