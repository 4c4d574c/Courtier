"""Domain-contributed guardrails: declaration, loading, registration."""

from __future__ import annotations

import pytest

from courtier.agent.core.guardrails import GuardrailSystem
from courtier.agent.core.guardrails.domain_guards import (
    GuardLoadError,
    load_domain_guard,
    register_domain_guards,
)
from courtier.domain.loader import DomainConfig


class TestLoadDomainGuard:
    def test_valid_class_path_loads(self):
        guard = load_domain_guard("courtier.agent.testing.DummyDomainGuard")
        assert guard.name == "dummy_domain_guard"
        assert guard.layer == "tool_call"

    def test_bad_path_shape_rejected(self):
        with pytest.raises(GuardLoadError, match="module"):
            load_domain_guard("NoDot")

    def test_unknown_module_rejected(self):
        with pytest.raises(GuardLoadError, match="imported"):
            load_domain_guard("no.such.module.Guard")

    def test_non_guard_class_rejected(self):
        with pytest.raises(GuardLoadError, match="attribute"):
            load_domain_guard("courtier.agent.testing.MockModelClient")

    def test_illegal_layer_rejected(self):
        with pytest.raises(GuardLoadError, match="illegal layer"):
            load_domain_guard("courtier.agent.testing.BadLayerDomainGuard")


class TestDomainGuardRegistration:
    def test_register_reports_names_and_populates_system(self):
        system = GuardrailSystem()
        registered = register_domain_guards(
            system,
            ["courtier.agent.testing.DummyDomainGuard"],
            owner="domain:dummy",
        )
        assert registered == ["dummy_domain_guard"]
        assert [g.name for g in system.guardrails] == ["dummy_domain_guard"]

    def test_failing_declaration_skipped_not_fatal(self):
        system = GuardrailSystem()
        registered = register_domain_guards(
            system,
            ["no.such.module.Guard", "courtier.agent.testing.DummyDomainGuard"],
            owner="domain:dummy",
        )
        assert registered == ["dummy_domain_guard"]


def test_domain_config_accepts_guards():
    config = DomainConfig(name="d", guards=["courtier.agent.testing.DummyDomainGuard"])
    assert config.guards == ["courtier.agent.testing.DummyDomainGuard"]
    assert DomainConfig(name="d").guards == []


class TestActivatorSeam:
    def _activator_with_agent(self):
        from courtier.agent.runtime.activation import DomainActivator
        from types import SimpleNamespace

        activator = object.__new__(DomainActivator)
        agent = SimpleNamespace(guardrail_system=None)
        activator._agent = agent
        pkg = SimpleNamespace(
            config=DomainConfig(name="dummy", guards=["courtier.agent.testing.DummyDomainGuard"])
        )
        return activator, agent, pkg

    def test_seam_registers_into_session_system(self):
        activator, agent, pkg = self._activator_with_agent()
        registered = activator._register_domain_guards("dummy", pkg)
        assert registered == ["dummy_domain_guard"]
        # A missing session system was created and attached.
        assert agent.guardrail_system is not None
        assert [g.name for g in agent.guardrail_system.guardrails] == ["dummy_domain_guard"]

    def test_seam_noop_without_declarations(self):
        activator, agent, pkg = self._activator_with_agent()
        pkg.config = DomainConfig(name="dummy")
        assert activator._register_domain_guards("dummy", pkg) == []
        assert agent.guardrail_system is None


class TestValidateDomainGuards:
    def test_invalid_guard_declaration_reported(self, tmp_path):
        from courtier.domain.loader import DomainLoader

        domain = tmp_path / "d"
        (domain / "config" / "prompts" / "zh-CN").mkdir(parents=True)
        (domain / "config" / "prompts" / "zh-CN" / "x.yaml").write_text("a: b")
        (domain / "skills").mkdir()
        (domain / "skills" / "x.md").write_text("s")
        (domain / "config" / "domain.yaml").write_text(
            "name: d\nguards: ['no.such.module.Guard']\n"
        )
        issues = DomainLoader.validate_domain(domain, plugins_root=tmp_path / "plugins")
        assert any("Guard declaration" in i for i in issues)

    def test_valid_guard_declaration_no_issue(self, tmp_path):
        from courtier.domain.loader import DomainLoader

        domain = tmp_path / "d"
        (domain / "config" / "prompts" / "zh-CN").mkdir(parents=True)
        (domain / "config" / "prompts" / "zh-CN" / "x.yaml").write_text("a: b")
        (domain / "skills").mkdir()
        (domain / "skills" / "x.md").write_text("s")
        (domain / "config" / "domain.yaml").write_text(
            "name: d\nguards: ['courtier.agent.testing.DummyDomainGuard']\n"
        )
        issues = DomainLoader.validate_domain(domain, plugins_root=tmp_path / "plugins")
        assert not any("Guard declaration" in i for i in issues)
