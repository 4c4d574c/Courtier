"""Tests for Agent base class ref instruction injection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from courtier.agent.agents.base import Agent


@pytest.fixture
def agent_with_cm():
    """An Agent with a mock context_manager."""
    agent = Agent(
        name="TestAgent",
        role="You are a test agent.",
        model=MagicMock(),
    )
    mock_cm = MagicMock()
    mock_cm.get_ref_instructions.return_value = (
        "Use $ref:tool:N as parameter value to reference persisted outputs."
    )
    return agent, mock_cm


def _make_fake_loop(captured: list):
    """Return an async fake agent_loop that records state into *captured*."""

    async def _fake_loop(**kwargs):
        from courtier.agent.core.state import AgentState

        captured.append(kwargs["state"])
        return AgentState(status="completed", messages=())

    return _fake_loop


class TestRefInstructionInjection:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_ref_instructions(self, agent_with_cm):
        agent, mock_cm = agent_with_cm
        captured: list = []

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop(captured),
        ):
            await agent.run("test task", context_manager=mock_cm)

        assert len(captured) == 1
        system_msg = captured[0].messages[0]
        assert "$ref:" in system_msg.content

    @pytest.mark.asyncio
    async def test_no_injection_without_context_manager(self, agent_with_cm):
        agent, _ = agent_with_cm
        captured: list = []

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop(captured),
        ):
            await agent.run("test task")

        assert len(captured) == 1
        system_msg = captured[0].messages[0]
        assert "$ref:" not in system_msg.content


class TestBuiltinArtifactTools:
    @pytest.mark.asyncio
    async def test_registers_artifact_tools_when_context_manager_provided(self, agent_with_cm):
        """Agents receiving a context_manager must expose the artifact
        introspection tools advertised by ContextManager.get_ref_instructions().
        """
        agent, mock_cm = agent_with_cm

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop([]),
        ):
            await agent.run("test task", context_manager=mock_cm)

        tool_names = {t.name for t in agent.tool_registry.list_tools()}
        assert "list_artifacts" in tool_names
        assert "get_artifact" in tool_names

    @pytest.mark.asyncio
    async def test_does_not_register_artifact_tools_without_context_manager(self, agent_with_cm):
        agent, _ = agent_with_cm

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop([]),
        ):
            await agent.run("test task")

        tool_names = {t.name for t in agent.tool_registry.list_tools()}
        assert "list_artifacts" not in tool_names
        assert "get_artifact" not in tool_names


class TestIdentityDisplayName:
    def test_identity_uses_agent_name_when_provided(self):
        """Skill sub-agents pass a display_name so the model sees a
        natural-language identity instead of a callable tool identifier."""
        agent = Agent(
            name="content_audit",
            role="审核文档内容。",
            model=MagicMock(),
            agent_name="内容审核",
        )
        prompt = agent.build_system_prompt()
        assert "你是 内容审核。" in prompt
        assert "你是 content_audit" not in prompt

    def test_identity_falls_back_to_registry_name(self):
        agent = Agent(
            name="content_audit",
            role="审核文档内容。",
            model=MagicMock(),
        )
        assert "你是 content_audit。" in agent.build_system_prompt()


class TestMaybeForcedFirstTool:
    @staticmethod
    def _agent():
        class _ParseStub:
            name = "parse_document"
            description = "stub"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                raise NotImplementedError

        agent = Agent(
            name="A",
            role="r",
            model=MagicMock(),
            first_required_tool="parse_document",
        )
        agent.tool_registry.register(_ParseStub())
        return agent

    def test_forced_when_file_path_and_no_history(self):
        agent = self._agent()
        call = agent._maybe_forced_first_tool({"file_path": "/tmp/a.docx"}, None)
        assert call is not None
        assert call.name == "parse_document"
        assert call.arguments == {"file_path": "/tmp/a.docx"}

    def test_forced_call_ids_are_unique(self):
        """The forced tool_call id must never repeat within one history."""
        agent = self._agent()
        ids = {
            agent._maybe_forced_first_tool({"file_path": "/tmp/a.docx"}, None).id for _ in range(5)
        }
        assert len(ids) == 5
        assert all(id_.startswith("forced-first-parse_document-") for id_ in ids)

    def test_skipped_without_file_path(self):
        agent = self._agent()
        assert agent._maybe_forced_first_tool({}, None) is None
        assert agent._maybe_forced_first_tool(None, None) is None

    def test_skipped_when_tool_not_registered(self):
        agent = Agent(name="A", role="r", model=MagicMock(), first_required_tool="parse_document")
        assert agent._maybe_forced_first_tool({"file_path": "/tmp/a.docx"}, None) is None

    def test_skipped_when_already_parsed_same_path(self):
        import json as _json

        from courtier.agent.core.state import AgentState, Message
        from courtier.agent.core.tool_call import ToolCall

        agent = self._agent()
        call_id = "tc-1"
        messages = (
            Message(role="system", content="sys"),
            Message(role="user", content="审核"),
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(
                        id=call_id,
                        name="parse_document",
                        arguments={"file_path": "/tmp/a.docx"},
                    ),
                ),
            ),
            Message(
                role="tool",
                content=_json.dumps({"success": True, "raw_data": {"pages": []}}),
                tool_call_id=call_id,
                name="parse_document",
            ),
        )
        state = AgentState.initial(task="t", system_prompt="s").model_copy(
            update={"messages": messages}
        )
        assert agent._maybe_forced_first_tool({"file_path": "/tmp/a.docx"}, state) is None

    def test_forced_again_for_new_upload(self):
        import json as _json

        from courtier.agent.core.state import AgentState, Message
        from courtier.agent.core.tool_call import ToolCall

        agent = self._agent()
        call_id = "tc-1"
        messages = (
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(
                        id=call_id,
                        name="parse_document",
                        arguments={"file_path": "/tmp/old.docx"},
                    ),
                ),
            ),
            Message(
                role="tool",
                content=_json.dumps({"success": True, "raw_data": {"pages": []}}),
                tool_call_id=call_id,
                name="parse_document",
            ),
        )
        state = AgentState.initial(task="t", system_prompt="s").model_copy(
            update={"messages": messages}
        )
        # A different file in this turn must parse again.
        call = agent._maybe_forced_first_tool({"file_path": "/tmp/new.docx"}, state)
        assert call is not None
        assert call.arguments == {"file_path": "/tmp/new.docx"}


class TestHasSuccessfulCallFor:
    """Direct unit tests for the O(n) history check, covering the compacted
    shapes (micro-compact placeholder, full-compaction summary)."""

    @staticmethod
    def _messages(tool_payload: dict, *, file_path: str = "/tmp/a.docx"):
        import json as _json

        from courtier.agent.core.state import Message
        from courtier.agent.core.tool_call import ToolCall

        return (
            Message(role="system", content="sys"),
            Message(role="user", content="审核"),
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(
                        id="tc-1",
                        name="parse_document",
                        arguments={"file_path": file_path},
                    ),
                ),
            ),
            Message(
                role="tool",
                content=_json.dumps(tool_payload, ensure_ascii=False),
                tool_call_id="tc-1",
                name="parse_document",
            ),
        )

    def test_live_success(self):
        from courtier.agent.agents.base import _has_successful_call_for

        msgs = self._messages({"success": True, "raw_data": None, "result_id": "$ref:x:1"})
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is True

    def test_live_failure(self):
        from courtier.agent.agents.base import _has_successful_call_for

        msgs = self._messages({"success": False, "error": "boom"})
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is False

    def test_other_file_not_matched(self):
        from courtier.agent.agents.base import _has_successful_call_for

        msgs = self._messages({"success": True})
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/other.docx") is False

    def test_omitted_placeholder_success(self):
        from courtier.agent.agents.base import _has_successful_call_for

        msgs = self._messages(
            {"_omitted": True, "tool": "parse_document", "success": True, "ref_id": "$ref:x:1"}
        )
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is True

    def test_omitted_placeholder_failure(self):
        from courtier.agent.agents.base import _has_successful_call_for

        msgs = self._messages({"_omitted": True, "tool": "parse_document", "success": False})
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is False

    def test_compaction_summary_with_ref_and_path(self):
        import json as _json

        from courtier.agent.agents.base import _has_successful_call_for
        from courtier.agent.core.state import Message

        summary = Message(
            role="system",
            content=(
                "[上下文压缩 #1] 以下为之前对话的摘要，请继续完成任务：\n\n"
                "已解析 /tmp/a.docx，结果见 $ref:parse_document:1。"
            ),
        )
        msgs = (
            summary,
            Message(role="user", content="继续"),
            Message(
                role="tool",
                content=_json.dumps({"success": True}),
                tool_call_id="unrelated",
                name="echo",
            ),
        )
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is True
        # A summary about a different file must not match.
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/b.docx") is False

    def test_non_json_tool_content_skipped(self):
        from courtier.agent.agents.base import _has_successful_call_for
        from courtier.agent.core.state import Message
        from courtier.agent.core.tool_call import ToolCall

        msgs = (
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(
                        id="tc-1",
                        name="parse_document",
                        arguments={"file_path": "/tmp/a.docx"},
                    ),
                ),
            ),
            Message(role="tool", content="not-json", tool_call_id="tc-1", name="parse_document"),
        )
        assert _has_successful_call_for(msgs, "parse_document", "/tmp/a.docx") is False


class TestProxyReplacementOnSync:
    """Plugin restart: the shared registry gets a new proxy (new client) for
    the same tool name; the agent's private copy must be hot-replaced."""

    @staticmethod
    def _proxy(name: str, plugin_name: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}},
            _client=SimpleNamespace(plugin_name=plugin_name),
        )

    @staticmethod
    async def _run_sync(agent):
        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop([]),
        ):
            await agent.run("task")

    @pytest.mark.asyncio
    async def test_same_name_proxy_replaced_after_restart(self):
        from courtier.agent.tools.registry import ToolRegistry

        shared = ToolRegistry()
        old_proxy = self._proxy("parse_document", "parse")
        shared.register(old_proxy)

        agent = Agent(name="A", role="r", model=MagicMock(), tool_registry=shared)
        assert agent.tool_registry.get("parse_document") is old_proxy

        # Plugin restarts: shared registry swaps in a new proxy instance.
        new_proxy = self._proxy("parse_document", "parse")
        shared.unregister("parse_document")
        shared.register(new_proxy)

        await self._run_sync(agent)
        assert agent.tool_registry.get("parse_document") is new_proxy

    @pytest.mark.asyncio
    async def test_scoped_wrapper_preserved_around_new_proxy(self):
        from courtier.agent.tools.registry import ToolRegistry
        from courtier.agent.tools.scoped import ScopedTool

        shared = ToolRegistry()
        old_proxy = self._proxy("search_documents", "search")
        shared.register(old_proxy)

        agent = Agent(name="A", role="r", model=MagicMock(), tool_registry=shared)
        agent.tool_registry.register(
            ScopedTool(agent.tool_registry.get("search_documents"), {"_owner_scope": 7}),
            force=True,
        )

        new_proxy = self._proxy("search_documents", "search")
        shared.unregister("search_documents")
        shared.register(new_proxy)

        await self._run_sync(agent)
        wrapped = agent.tool_registry.get("search_documents")
        assert isinstance(wrapped, ScopedTool)
        assert wrapped._inner is new_proxy
        assert wrapped._injections == {"_owner_scope": 7}

    @pytest.mark.asyncio
    async def test_same_instance_not_replaced(self):
        """No-op sync: identical object identity leaves the registry alone."""
        from courtier.agent.tools.registry import ToolRegistry

        shared = ToolRegistry()
        proxy = self._proxy("parse_document", "parse")
        shared.register(proxy)
        agent = Agent(name="A", role="r", model=MagicMock(), tool_registry=shared)

        await self._run_sync(agent)
        assert agent.tool_registry.get("parse_document") is proxy


class TestPluginGuidesInjection:
    """Plugin system_prompt (tool-usage guidance) injection channel."""

    @staticmethod
    def _agent_with_proxy():
        from types import SimpleNamespace

        proxy = SimpleNamespace(
            name="parse_document",
            description="",
            parameters={"type": "object", "properties": {}},
            _client=SimpleNamespace(plugin_name="parse"),
        )
        agent = Agent(name="A", role="r", model=MagicMock(), tools=[proxy])
        return agent

    def test_guide_injected_for_held_plugin(self):
        agent = self._agent_with_proxy()
        agent.set_plugin_prompts_provider(
            lambda: {"parse": "解析插件用法：先 parse 再 audit", "other": "不应出现"}
        )
        prompt = agent.build_system_prompt()
        assert "工具使用说明" in prompt
        assert "解析插件用法：先 parse 再 audit" in prompt
        assert "不应出现" not in prompt

    def test_guide_cleared_when_provider_returns_empty(self):
        agent = self._agent_with_proxy()
        agent.set_plugin_prompts_provider(lambda: {"parse": "用法"})
        assert "工具使用说明" in agent.build_system_prompt()
        agent.set_plugin_prompts_provider(lambda: {})
        assert "工具使用说明" not in agent.build_system_prompt()

    def test_no_section_without_provider(self):
        agent = self._agent_with_proxy()
        assert "工具使用说明" not in agent.build_system_prompt()
