"""ToolRegistry.clone() semantics — per-session isolation with shared instances."""

from __future__ import annotations

from types import SimpleNamespace

from courtier.agent.tools.registry import ToolRegistry


def _fake_tool(name: str):
    return SimpleNamespace(
        name=name,
        description="",
        display_name=None,
        skill="",
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        skip_persist=True,
        output_content_type=None,
        input_contract=None,
        output_contract=None,
        runtime_policy=None,
    )


class TestToolRegistryClone:
    def test_clone_shares_tool_instances_by_reference(self):
        base = ToolRegistry()
        tool = _fake_tool("search_documents")
        base.register(tool)

        clone = base.clone()
        assert clone.get("search_documents") is tool
        assert clone.get("search_documents") is base.get("search_documents")

    def test_clone_registration_does_not_leak_into_base(self):
        base = ToolRegistry()
        base.register(_fake_tool("search_documents"))

        clone = base.clone()
        clone.register(_fake_tool("extra_tool"), force=True)
        clone.unregister("search_documents")

        assert "extra_tool" not in {t.name for t in base.list_tools()}
        assert "search_documents" in {t.name for t in base.list_tools()}
        assert {t.name for t in clone.list_tools()} == {"extra_tool"}

    def test_clone_run_state_counters_are_independent(self):
        base = ToolRegistry()
        base.register(_fake_tool("search_documents"))
        clone = base.clone()

        # The base tracks call counts; a clone resetting its run state must
        # not clear the base's counters.
        base._tool_call_counts["search_documents"] = 3
        clone.reset_run_state()
        assert base._tool_call_counts["search_documents"] == 3
        assert clone._tool_call_counts == {}

    def test_clone_of_clone_keeps_same_instances(self):
        base = ToolRegistry()
        tool = _fake_tool("search_documents")
        base.register(tool)

        clone2 = base.clone().clone()
        assert clone2.get("search_documents") is tool
