"""Declarative guard registry: descriptors, session context, unified loading."""

from __future__ import annotations

import pytest

from courtier.agent.core.guardrails.domain_guards import load_domain_guard
from courtier.agent.core.guardrails.registry import (
    GuardDescriptor,
    GuardLoadError,
    GuardSessionContext,
    check_guard_declaration,
    descriptor_from_raw,
    instantiate_guard,
    load_guard_descriptor,
)


class TestInstantiateGuard:
    def test_noarg_class_loads(self):
        guard = instantiate_guard("courtier.agent.testing.DummyDomainGuard")
        assert guard.name == "dummy_domain_guard"
        assert guard.layer == "tool_call"

    def test_factory_receives_session_context(self, tmp_path):
        ctx = GuardSessionContext(session_workspace=tmp_path)
        guard = instantiate_guard("courtier.agent.testing.FactoryBuiltGuard", ctx)
        assert guard.name == "factory_built_guard"
        assert guard.workspace == str(tmp_path)

    def test_factory_guard_without_context_falls_back_to_noarg(self):
        guard = instantiate_guard("courtier.agent.testing.FactoryBuiltGuard")
        assert guard.workspace is None

    def test_bad_path_shape_rejected(self):
        with pytest.raises(GuardLoadError, match="module"):
            instantiate_guard("NoDot")

    def test_unknown_module_rejected(self):
        with pytest.raises(GuardLoadError, match="imported"):
            instantiate_guard("no.such.module.Guard")

    def test_non_guard_class_rejected(self):
        with pytest.raises(GuardLoadError, match="attribute"):
            instantiate_guard("courtier.agent.testing.MockModelClient")

    def test_illegal_layer_rejected(self):
        with pytest.raises(GuardLoadError, match="illegal layer"):
            instantiate_guard("courtier.agent.testing.BadLayerDomainGuard")

    def test_domain_loader_delegates_to_registry(self):
        guard = load_domain_guard("courtier.agent.testing.DummyDomainGuard")
        assert guard.name == "dummy_domain_guard"


class TestCheckGuardDeclaration:
    def test_returns_class_level_name(self):
        name = check_guard_declaration("courtier.agent.testing.FactoryBuiltGuard")
        assert name == "factory_built_guard"

    def test_required_ctor_args_without_build_rejected(self):
        with pytest.raises(GuardLoadError, match="build"):
            check_guard_declaration("courtier.agent.testing.RequiresArgsGuard")

    def test_factory_class_passes_without_instantiation(self):
        name = check_guard_declaration("courtier.agent.testing.FactoryBuiltGuard", scope="session")
        assert name == "factory_built_guard"

    def test_tool_call_run_scope_rejected_statically(self):
        with pytest.raises(GuardLoadError, match="session-scoped"):
            check_guard_declaration("courtier.agent.testing.DummyDomainGuard", scope="run")

    def test_unknown_class_rejected(self):
        with pytest.raises(GuardLoadError, match="imported"):
            check_guard_declaration("no.such.module.Guard")


class TestLoadGuardDescriptor:
    def test_session_descriptor_loads(self):
        descriptor = GuardDescriptor(
            name="dummy", class_path="courtier.agent.testing.DummyDomainGuard"
        )
        guard = load_guard_descriptor(descriptor)
        assert guard.name == "dummy_domain_guard"

    def test_run_scope_tool_call_rejected_at_load(self):
        descriptor = GuardDescriptor(
            name="dummy",
            class_path="courtier.agent.testing.DummyDomainGuard",
            scope="run",
        )
        with pytest.raises(GuardLoadError, match="session-scoped"):
            load_guard_descriptor(descriptor)


class TestDescriptorFromRaw:
    def test_full_object_parses(self):
        descriptor = descriptor_from_raw(
            {
                "name": "my_guard",
                "class_path": "courtier.agent.testing.FactoryBuiltGuard",
                "scope": "run",
                "enabled": False,
                "builtin": True,
            }
        )
        assert descriptor.name == "my_guard"
        assert descriptor.scope == "run"
        assert descriptor.enabled is False
        assert descriptor.builtin is True

    def test_defaults_apply(self):
        descriptor = descriptor_from_raw({"name": "my_guard", "class_path": "x.y.Z"})
        assert descriptor.scope == "session"
        assert descriptor.enabled is True
        assert descriptor.builtin is False

    def test_non_object_rejected(self):
        with pytest.raises(GuardLoadError, match="object"):
            descriptor_from_raw("courtier.agent.testing.DummyDomainGuard")

    @pytest.mark.parametrize("missing", ["name", "class_path"])
    def test_missing_required_field_rejected(self, missing):
        raw = {"name": "a", "class_path": "b.c"}
        raw.pop(missing)
        with pytest.raises(GuardLoadError, match=missing):
            descriptor_from_raw(raw)

    def test_illegal_scope_rejected(self):
        with pytest.raises(GuardLoadError, match="scope"):
            descriptor_from_raw({"name": "a", "class_path": "b.c", "scope": "turn"})

    def test_non_boolean_enabled_rejected(self):
        with pytest.raises(GuardLoadError, match="enabled"):
            descriptor_from_raw({"name": "a", "class_path": "b.c", "enabled": "yes"})
