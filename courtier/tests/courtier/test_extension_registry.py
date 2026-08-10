"""ExtensionRegistry system_prompt collection — the source of the
tool-usage-guidance injection into agent system prompts."""

from __future__ import annotations

from courtier.plugin import PluginSystem
from courtier.plugin.registry import ExtensionRegistry


def test_system_prompts_collected_on_register():
    registry = ExtensionRegistry()
    registry.on_register("parse", client=object(), capabilities=[], system_prompt="用法说明")
    assert registry.get_system_prompts() == {"parse": "用法说明"}


def test_system_prompts_removed_on_unregister():
    registry = ExtensionRegistry()
    registry.on_register("parse", client=object(), capabilities=[], system_prompt="用法说明")
    registry.on_unregister("parse")
    assert registry.get_system_prompts() == {}


def test_empty_system_prompt_not_stored():
    registry = ExtensionRegistry()
    registry.on_register("parse", client=object(), capabilities=[])
    assert registry.get_system_prompts() == {}


def test_plugin_system_delegates_to_extension_registry(tmp_path):
    system = PluginSystem(plugins_dir=str(tmp_path))
    assert system.get_system_prompts() == {}
    system._registry.on_register(
        "parse", client=object(), capabilities=[], system_prompt="用法说明"
    )
    assert system.get_system_prompts() == {"parse": "用法说明"}
