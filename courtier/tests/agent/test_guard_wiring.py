"""Declarative guard wiring: the settings declaration list assembles the
session GuardrailSystem. Equivalence contract — with the seeded defaults the
registered set, order, and constructor wiring match the historical
hardcoded assembly in build_agent."""

from __future__ import annotations

from courtier.agent.core.guardrails import GuardrailSystem
from courtier.agent.core.guardrails.registry import (
    GuardLoadError,
    GuardSessionContext,
    wire_guard_declarations,
)
from courtier.config import Settings


def _session_ctx(tmp_path, settings=None, **kwargs):
    return GuardSessionContext(session_workspace=tmp_path / "sess", settings=settings, **kwargs)


class TestWireDefaultDeclarations:
    def test_default_settings_register_baseline_in_order(self, tmp_path):
        system = GuardrailSystem()
        run_descriptors = wire_guard_declarations(
            system, Settings(_env_file=None).guardrail_guards, _session_ctx(tmp_path)
        )
        assert [g.name for g in system.guardrails] == [
            "tool_disabled",
            "path_policy",
            "tool_confirmation",
        ]
        assert [d.name for d in run_descriptors] == ["explore_loop", "business_artifact"]

    def test_path_policy_wired_to_session_workspace(self, tmp_path):
        system = GuardrailSystem()
        wire_guard_declarations(
            system, Settings(_env_file=None).guardrail_guards, _session_ctx(tmp_path)
        )
        path_guard = system.guardrails[1]
        assert path_guard.name == "path_policy"
        assert path_guard._roots == [(tmp_path / "sess").resolve()]

    def test_confirmation_gets_live_approved_set(self, tmp_path):
        approved: set[str] = set()
        system = GuardrailSystem()
        wire_guard_declarations(
            system,
            Settings(_env_file=None).guardrail_guards,
            _session_ctx(tmp_path, approved_tools=approved),
        )
        assert system.guardrails[2].approved_tools is approved


class TestWireDeclaredEntries:
    def test_disabled_builtin_not_registered(self, tmp_path):
        settings = Settings(_env_file=None)
        settings.guardrail_guards[0].enabled = False  # tool_disabled off
        system = GuardrailSystem()
        wire_guard_declarations(system, settings.guardrail_guards, _session_ctx(tmp_path))
        assert [g.name for g in system.guardrails] == ["path_policy", "tool_confirmation"]

    def test_custom_session_entry_appends_in_order(self, tmp_path):
        settings = Settings(
            _env_file=None,
            guardrail_guards=[
                {
                    "name": "my_guard",
                    "class_path": "courtier.agent.testing.FactoryBuiltGuard",
                    "scope": "session",
                }
            ],
        )
        settings.guardrail_guards = [
            *settings.guardrail_guards,
            *Settings(_env_file=None).guardrail_guards,
        ]
        system = GuardrailSystem()
        wire_guard_declarations(system, settings.guardrail_guards, _session_ctx(tmp_path))
        # List order: the custom entry first, then the baseline.
        assert [g.name for g in system.guardrails] == [
            "factory_built_guard",
            "tool_disabled",
            "path_policy",
            "tool_confirmation",
        ]

    def test_broken_declaration_skipped_others_registered(self, tmp_path):
        settings = Settings(_env_file=None)
        system = GuardrailSystem()
        declarations = [
            *settings.guardrail_guards,
            type(settings.guardrail_guards[0])(
                name="broken", class_path="no.such.module.Guard", scope="session"
            ),
        ]
        run_descriptors = wire_guard_declarations(system, declarations, _session_ctx(tmp_path))
        assert [g.name for g in system.guardrails] == [
            "tool_disabled",
            "path_policy",
            "tool_confirmation",
        ]
        assert [d.name for d in run_descriptors] == ["explore_loop", "business_artifact"]


def test_guard_load_error_still_exported():
    from courtier.agent.core.guardrails.registry import GuardLoadError as err

    assert err is GuardLoadError
