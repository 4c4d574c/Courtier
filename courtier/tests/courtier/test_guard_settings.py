"""Settings surface of declarative guards: guardrail_guards validation
(structural + class-level + builtin authority) and tools_disabled."""

from __future__ import annotations

import pytest

from courtier.config import Settings

FACTORY_GUARD = "courtier.agent.testing.FactoryBuiltGuard"
DUMMY_GUARD = "courtier.agent.testing.DummyDomainGuard"


def _declaration(name: str, class_path: str = FACTORY_GUARD, **overrides):
    item = {"name": name, "class_path": class_path, "scope": "session", "enabled": True}
    item.update(overrides)
    return item


class TestGuardrailGuardsValidator:
    def test_default_is_the_five_builtins(self):
        """Baseline present even with no DB row (env-only mode)."""
        entries = Settings(_env_file=None).guardrail_guards
        assert [e.name for e in entries] == [
            "tool_disabled",
            "path_policy",
            "confirmation",
            "explore_loop",
            "business_artifact",
        ]
        assert all(e.builtin for e in entries)

    def test_valid_declaration_constructs(self):
        settings = Settings(_env_file=None, guardrail_guards=[_declaration("my_guard")])
        assert len(settings.guardrail_guards) == 1
        entry = settings.guardrail_guards[0]
        assert entry.name == "my_guard"
        assert entry.class_path == FACTORY_GUARD
        assert entry.scope == "session"
        assert entry.enabled is True
        assert entry.builtin is False

    def test_json_string_accepted(self):
        settings = Settings(
            _env_file=None,
            guardrail_guards=f'[{{"name": "my_guard", "class_path": "{FACTORY_GUARD}"}}]',
        )
        assert settings.guardrail_guards[0].name == "my_guard"

    def test_bad_class_path_rejected(self):
        with pytest.raises(ValueError, match="imported"):
            Settings(
                _env_file=None,
                guardrail_guards=[_declaration("my_guard", class_path="no.such.module.Guard")],
            )

    def test_tool_call_run_scope_rejected(self):
        with pytest.raises(ValueError, match="session-scoped"):
            Settings(
                _env_file=None,
                guardrail_guards=[_declaration("dummy", class_path=DUMMY_GUARD, scope="run")],
            )

    def test_duplicate_names_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            Settings(
                _env_file=None,
                guardrail_guards=[_declaration("same"), _declaration("same")],
            )

    def test_non_object_item_rejected(self):
        with pytest.raises(ValueError, match="object"):
            Settings(_env_file=None, guardrail_guards=["courtier.agent.testing.DummyDomainGuard"])

    def test_builtin_identity_is_server_authoritative(self):
        """A client-submitted builtin entry keeps only its enabled flag; the
        identity fields are forced back to the seed definition."""
        settings = Settings(
            _env_file=None,
            guardrail_guards=[
                _declaration(
                    "path_policy",
                    class_path=FACTORY_GUARD,  # tampered identity
                    scope="run",  # tampered scope
                )
            ],
        )
        entry = settings.guardrail_guards[0]
        assert entry.class_path.endswith("PathPolicyGuard")
        assert entry.scope == "session"
        assert entry.builtin is True

    def test_seed_override_runs_before_deep_check(self):
        """Tampered fields are replaced by seed values before validation, so
        a builtin entry can never fail via client-supplied identity."""
        settings = Settings(
            _env_file=None,
            guardrail_guards=[
                # run-scope seed entry submitted with a tool_call class and
                # session scope — must come out as the seed definition.
                _declaration("explore_loop", class_path=DUMMY_GUARD, scope="session"),
            ],
        )
        entry = settings.guardrail_guards[0]
        assert entry.class_path.endswith("ExploreLoopGuard")
        assert entry.scope == "run"

    def test_non_builtin_entry_cannot_claim_builtin(self):
        settings = Settings(
            _env_file=None,
            guardrail_guards=[_declaration("my_guard", builtin=True)],
        )
        assert settings.guardrail_guards[0].builtin is False


class TestToolsDisabledValidator:
    def test_default_is_empty(self):
        assert Settings(_env_file=None).tools_disabled == []

    def test_list_normalized_and_deduped(self):
        settings = Settings(_env_file=None, tools_disabled=["deploy", " deploy ", "deploy"])
        assert settings.tools_disabled == ["deploy"]

    def test_json_string_accepted(self):
        settings = Settings(_env_file=None, tools_disabled='["deploy"]')
        assert settings.tools_disabled == ["deploy"]

    def test_non_string_entry_rejected(self):
        with pytest.raises(ValueError, match="non-empty strings"):
            Settings(_env_file=None, tools_disabled=[42])

    def test_unknown_tool_names_allowed(self):
        """Blacklist semantics: names that match no tool are harmless."""
        settings = Settings(_env_file=None, tools_disabled=["not_a_tool_yet"])
        assert settings.tools_disabled == ["not_a_tool_yet"]
