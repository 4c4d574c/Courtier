"""Tests for sub-agent artifact visibility wiring in AgentRuntime."""

from unittest.mock import patch

import pytest

from courtier.agent.agents.base import AgentResult
from courtier.agent.artifacts.scoped_store import ScopedArtifactView
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.runtime import AgentRuntime, AgentRuntimeBudget
from courtier.agent.runtime.handle import AgentHandle
from courtier.agent.skills import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


def _make_runtime(tmp_path, skill_names=("worker",)):
    """Create an AgentRuntime whose agents are registered from skill files."""
    for name in skill_names:
        (tmp_path / f"{name}.md").write_text(
            f"---\nname: {name}\nversion: '1.0'\ntools: [echo]\n---\nYou are {name}.\n",
            encoding="utf-8",
        )
    skill_registry = SkillRegistry(tmp_path)
    skill_registry.scan()
    tool_registry = ToolRegistry()
    tool_registry.register(EchoTool())
    return AgentRuntime(
        tool_registry=tool_registry,
        model=MockModelClient(tool_calls=[]),
        skill_registry=skill_registry,
    )


def _handle(handle_id=None, ref_ids=None) -> AgentHandle:
    return AgentHandle.create(
        handle_id=handle_id,
        agent_name="worker",
        agent_type="skill",
        task="t",
        budget=AgentRuntimeBudget(),
        ref_ids=ref_ids,
    )


# -- _build_scoped_store --------------------------------------------------------


def test_build_scoped_store_wraps_root_store(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))

    view = runtime._build_scoped_store(store, _handle(handle_id="h-abc"))

    assert isinstance(view, ScopedArtifactView)
    assert not isinstance(view, ArtifactStore)
    assert view.scope == "h-abc"
    assert view.allowed == frozenset()


def test_build_scoped_store_none_passthrough(tmp_path):
    runtime = _make_runtime(tmp_path)
    assert runtime._build_scoped_store(None, _handle()) is None


def test_build_scoped_store_nested_inherits_parent_allowed(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))

    root_view = runtime._build_scoped_store(store, _handle(handle_id="h-orch"))
    child_view = runtime._build_scoped_store(root_view, _handle(handle_id="h-child"))
    grandchild_view = runtime._build_scoped_store(child_view, _handle(handle_id="h-grand"))

    assert child_view.scope == "h-child"
    assert child_view.allowed == frozenset({"h-orch"})
    assert grandchild_view.allowed == frozenset({"h-orch", "h-child"})


def test_build_scoped_store_ref_ids_join_allowed(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))

    view = runtime._build_scoped_store(
        store, _handle(handle_id="h-1", ref_ids=["h-sibling", "$ref:x:1"])
    )

    assert "h-sibling" in view.allowed
    assert "$ref:x:1" in view.allowed


# -- delegate wiring --------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_passes_scoped_view_to_agent_run(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))
    captured: dict = {}

    async def fake_run(self, **kwargs):
        captured.update(kwargs)
        return AgentResult(status="completed", content="x" * 60)

    handle = runtime.spawn(name="worker", task="audit it")
    with patch("courtier.agent.agents.base.Agent.run", fake_run):
        result = await runtime.delegate(handle, artifact_store=store)

    assert result.success
    view = captured["artifact_store"]
    assert isinstance(view, ScopedArtifactView)
    assert view.scope == handle.handle_id
    # artifact_context was removed from the run contract.
    assert "artifact_context" not in captured
    # The initial state already carries the task as its last user message;
    # Agent.run relies on this to avoid appending the task a second time.
    state = captured["state"]
    assert state.messages[-1].role == "user"
    assert state.messages[-1].content == "audit it"


def test_build_agent_wires_scoped_result_store(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))
    view = runtime._build_scoped_store(store, _handle(handle_id="h-1"))

    agent = runtime._build_agent(runtime._configs["worker"], result_store=view)

    # The sub-agent's private registry must see the scoped view, never the
    # shared root store.
    assert agent.tool_registry._result_store is view


# -- End-to-end sibling isolation ----------------------------------------------------


@pytest.mark.asyncio
async def test_sibling_subagents_do_not_see_each_others_artifacts(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))
    seen: dict = {}

    # The orchestrator's own parse result lives in the root store (public).
    store.register_cached_ref(
        ref_id="$ref:parse_layout:1",
        artifact_type="core.plain_text",
        created_by="parse_layout",
        data={"text": "document"},
    )

    async def fake_run(self, **kwargs):
        view = kwargs["artifact_store"]
        if kwargs["task"] == "older":
            view.register_cached_ref(
                ref_id="$ref:format_audit:1",
                artifact_type="core.plain_text",
                created_by="format_audit",
                data={"issues": []},
            )
        else:
            seen["ids"] = [a.artifact_id for a in view.list_all()]
            seen["get"] = view.get("$ref:format_audit:1")
            seen["read"] = await view.read("$ref:format_audit:1")
        return AgentResult(status="completed", content="x" * 60)

    older = runtime.spawn(name="worker", task="older")
    younger = runtime.spawn(name="worker", task="younger")
    with patch("courtier.agent.agents.base.Agent.run", fake_run):
        await runtime.delegate(older, artifact_store=store)
        await runtime.delegate(younger, artifact_store=store)

    # The younger sibling sees the public orchestrator artifact but not the
    # older sibling's output — on every read path.
    assert "$ref:parse_layout:1" in seen["ids"]
    assert "$ref:format_audit:1" not in seen["ids"]
    assert seen["get"] is None
    assert seen["read"] == {
        "error": "result not found: $ref:format_audit:1",
        "data": None,
    }

    # The root store holds both, and the sibling artifact is stamped with the
    # creator's handle_id.
    artifact = store.get("$ref:format_audit:1")
    assert artifact is not None
    assert artifact.metadata.subject == older.handle_id
    assert store.get("$ref:parse_layout:1").metadata.subject == "unknown"


@pytest.mark.asyncio
async def test_ref_ids_explicitly_admit_sibling_artifact(tmp_path):
    runtime = _make_runtime(tmp_path)
    store = ArtifactStore(cache_dir=str(tmp_path / "cache"))
    seen: dict = {}

    async def fake_run(self, **kwargs):
        view = kwargs["artifact_store"]
        if kwargs["task"] == "older":
            view.register_cached_ref(
                ref_id="$ref:format_audit:1",
                artifact_type="core.plain_text",
                created_by="format_audit",
                data={"issues": []},
            )
        else:
            seen["ids"] = [a.artifact_id for a in view.list_all()]
        return AgentResult(status="completed", content="x" * 60)

    older = runtime.spawn(name="worker", task="older")
    # The orchestrator explicitly hands the older sibling's scope to the
    # younger one via ref_ids.
    younger = runtime.spawn(name="worker", task="younger", ref_ids=[older.handle_id])
    with patch("courtier.agent.agents.base.Agent.run", fake_run):
        await runtime.delegate(older, artifact_store=store)
        await runtime.delegate(younger, artifact_store=store)

    assert "$ref:format_audit:1" in seen["ids"]
