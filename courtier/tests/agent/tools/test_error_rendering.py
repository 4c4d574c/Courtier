"""Unified error-text rendering.

All host-core error strings that reach the model are rendered from
``errors.*`` templates (courtier/prompts/defaults/{locale}/errors.yaml) via
``courtier.prompts.errors.render_error``.  These tests pin the contract:
locale coverage, registry/loop wiring, and the never-empty guarantee.
"""

from __future__ import annotations


import pytest

from courtier.agent.core.loop_phases import execute_tools_phase
from courtier.agent.core.model import ToolCall
from courtier.agent.core.state import AgentState
from courtier.agent.artifacts.models import RuntimePolicy
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry
from courtier.prompts.engine import PromptEngine
from courtier.prompts.errors import render_error


def _zh_engine() -> PromptEngine:
    return PromptEngine.from_domain_directories([], locale="zh-CN")


def _en_engine() -> PromptEngine:
    return PromptEngine.from_domain_directories([], locale="en-US")


class _TestTool:
    name = "policied"
    description = "tool with a call budget"
    parameters: dict = {"type": "object", "properties": {}}
    runtime_policy = RuntimePolicy(max_calls=1, max_consecutive=None)

    async def execute(self, **kwargs):
        return ToolResult(success=True, data="ok")


@pytest.mark.parametrize(
    ("key", "params", "expected"),
    [
        ("errors.tool_missing_param", {"tool_name": "t", "param_name": "key"}, "缺少必填参数 key"),
        ("errors.tool_not_found", {"tool_name": "t", "available_tools": "a"}, "未注册"),
        ("errors.tool_exception", {"tool_name": "t", "error": "boom"}, "执行异常"),
        ("errors.result_unknown_error", {"actor_name": "t"}, "未返回具体错误信息"),
        (
            "errors.tool_max_calls_exceeded",
            {"tool_name": "t", "count": 3, "limit": 2},
            "超过上限",
        ),
        ("errors.memory_unavailable", {}, "记忆功能不可用"),
        ("errors.subagent_timeout", {"timeout_seconds": 5}, "子代理执行超时"),
        ("errors.plugin_call_timeout", {"plugin_name": "p", "timeout": 1}, "调用超时"),
    ],
)
def test_zh_templates_render(key, params, expected):
    assert expected in render_error(key, engine=_zh_engine(), **params)


@pytest.mark.parametrize(
    ("key", "params", "expected"),
    [
        (
            "errors.tool_missing_param",
            {"tool_name": "t", "param_name": "key"},
            "missing required parameter 'key'",
        ),
        ("errors.tool_not_found", {"tool_name": "t", "available_tools": "a"}, "not registered"),
        ("errors.memory_unavailable", {}, "Memory is unavailable"),
        ("errors.subagent_timeout", {"timeout_seconds": 5}, "timed out"),
    ],
)
def test_en_templates_render(key, params, expected):
    assert expected in render_error(key, engine=_en_engine(), **params)


def test_render_error_returns_key_when_missing():
    assert render_error("errors.no_such_key", engine=_zh_engine()) == "errors.no_such_key"


def test_render_error_survives_engine_failure():
    class _Broken:
        def render(self, *_a, **_k):
            raise RuntimeError("engine down")

    assert (
        render_error("errors.memory_unavailable", engine=_Broken()) == "errors.memory_unavailable"
    )


def test_default_engine_follows_courtier_locale(monkeypatch):
    import courtier.prompts.errors as mod

    monkeypatch.setenv("COURTIER_LOCALE", "zh-CN")
    monkeypatch.setattr(mod, "_engine", None)
    try:
        assert "缺少必填参数" in render_error(
            "errors.tool_missing_param", tool_name="t", param_name="key"
        )
    finally:
        monkeypatch.setattr(mod, "_engine", None)


@pytest.mark.asyncio
async def test_registry_unknown_tool_uses_template():
    registry = ToolRegistry()
    result = await registry.execute("missing_tool")

    assert not result.success
    assert "未注册" in (result.error or "")
    assert "missing_tool" in (result.error or "")
    assert result.metadata.get("error_code") == "tool_not_found"


@pytest.mark.asyncio
async def test_registry_runtime_policy_uses_template():
    registry = ToolRegistry()
    registry.register(_TestTool())

    first = await registry.execute("policied")
    assert first.success

    second = await registry.execute("policied")
    assert not second.success
    assert "超过上限" in (second.error or "")
    assert "policied" in (second.error or "")
    assert second.metadata.get("blocked_reason") == "max_calls_exceeded"


@pytest.mark.asyncio
async def test_parse_error_uses_template():
    state = AgentState.initial(task="t").model_copy(
        update={
            "tool_calls": (
                ToolCall(id="c1", name="echo", arguments={"_parse_error": True, "raw": "{oops"}),
            )
        }
    )
    results, _records = await execute_tools_phase(
        state=state,
        tool_registry=ToolRegistry(),
        context_manager=None,
        artifact_store=None,
        on_tool_result=None,
    )

    assert len(results) == 1
    assert not results[0].success
    assert "参数解析失败" in (results[0].error or "")
    assert "{oops" in (results[0].error or "")
    assert results[0].metadata.get("error_code") == "tool_arg_parse"
