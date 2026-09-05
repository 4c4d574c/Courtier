"""PluginSystem.get_system_prompts — live domain-list annex.

Plugins that consume the ``memory_store`` host service must see the loaded
domain packages (write validation rejects unknown domains, so the model
needs the valid names); other plugins stay untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.plugin import PluginSystem
from courtier.plugin.manager import PluginProcess, PluginState
from courtier.plugin.manifest import PluginManifest


def _process(name: str, host_services: list[str]) -> PluginProcess:
    manifest = PluginManifest(
        name=name,
        version="1.0.0",
        api="v1",
        dependencies={"host_services": host_services},
    )
    return PluginProcess(
        name=name,
        manifest=manifest,
        plugin_dir=Path("."),
        state=PluginState.ACTIVE,
    )


def _system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompts: dict[str, str],
    processes: dict[str, PluginProcess],
    domain_names_provider=None,
) -> PluginSystem:
    system = PluginSystem(
        plugins_dir=tmp_path / "plugins",
        domain_names_provider=domain_names_provider,
    )
    monkeypatch.setattr(system._registry, "get_system_prompts", lambda: dict(prompts))
    monkeypatch.setattr(system._manager, "get_processes", lambda: dict(processes))
    return system


class TestMemoryDomainAnnex:
    def test_annex_appended_to_memory_store_plugins_only(self, tmp_path, monkeypatch):
        system = _system(
            tmp_path,
            monkeypatch,
            prompts={"memory": "记忆指引", "search": "搜索指引"},
            processes={
                "memory": _process("memory", ["memory_store"]),
                "search": _process("search", ["storage"]),
            },
            domain_names_provider=lambda: {"docaudit", "legal"},
        )
        prompts = system.get_system_prompts()
        assert prompts["memory"] == "记忆指引\n\n已加载领域包：docaudit、legal"
        assert prompts["search"] == "搜索指引"

    def test_annex_reflects_live_provider_per_call(self, tmp_path, monkeypatch):
        domains = {"docaudit"}
        system = _system(
            tmp_path,
            monkeypatch,
            prompts={"memory": "M"},
            processes={"memory": _process("memory", ["memory_store"])},
            domain_names_provider=lambda: domains,
        )
        assert system.get_system_prompts()["memory"].endswith("已加载领域包：docaudit")
        domains.add("other")
        assert system.get_system_prompts()["memory"].endswith("已加载领域包：docaudit、other")

    def test_no_annex_when_provider_missing(self, tmp_path, monkeypatch):
        system = _system(
            tmp_path,
            monkeypatch,
            prompts={"memory": "M"},
            processes={"memory": _process("memory", ["memory_store"])},
        )
        assert system.get_system_prompts() == {"memory": "M"}

    def test_no_annex_when_no_domains_loaded(self, tmp_path, monkeypatch):
        system = _system(
            tmp_path,
            monkeypatch,
            prompts={"memory": "M"},
            processes={"memory": _process("memory", ["memory_store"])},
            domain_names_provider=set,
        )
        assert system.get_system_prompts() == {"memory": "M"}

    def test_provider_failure_degrades_to_plain_prompts(self, tmp_path, monkeypatch):
        def _boom():
            raise RuntimeError("config not ready")

        system = _system(
            tmp_path,
            monkeypatch,
            prompts={"memory": "M"},
            processes={"memory": _process("memory", ["memory_store"])},
            domain_names_provider=_boom,
        )
        assert system.get_system_prompts() == {"memory": "M"}
