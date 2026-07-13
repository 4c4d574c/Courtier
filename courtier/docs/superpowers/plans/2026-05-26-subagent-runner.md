# SubAgent Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SubAgentRunner infrastructure so domain agents run through independent LLM loops, and the orchestrator dispatches them via configurable failure strategies.

**Architecture:** New `subagent.py` module introduces `SubAgentRunner`, `SubAgentConfig`, `FailureStrategy`, `_SubAgentTool`, and `_CallbackHolder`. Each domain agent's `audit()` method is rewritten to call `self.run()` (the inherited `Agent.run()` which drives `agent_loop`). The `OrchestratorAgent` replaces `_AuditorAsTool` with `SubAgentRunner.define()` + `build_tools()`.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio (auto mode), pydantic, existing agent framework

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/agent/agents/subagent.py` | SubAgentRunner, SubAgentConfig, FailureStrategy, _SubAgentTool, _CallbackHolder, _extract_result_data, _describe_failure |
| Create | `tests/agent/test_subagent.py` | All unit tests for subagent infrastructure |
| Modify | `src/agent/agents/format_auditor.py` | Rewrite audit() to use self.run() |
| Modify | `src/agent/agents/content_auditor.py` | Rewrite audit() to use self.run() |
| Modify | `src/agent/agents/correction_auditor.py` | Rewrite audit() to use self.run() |
| Modify | `src/agent/agents/style_auditor.py` | Rewrite audit() to use self.run() |
| Modify | `src/agent/agents/plagiarism_auditor.py` | Rewrite audit() to use self.run() |
| Modify | `src/agent/agents/orch.py` | Replace _AuditorAsTool with SubAgentRunner |
| Modify | `src/agent/agents/__init__.py` | Export new subagent types |
| Modify | `tests/agent/test_domain_agents.py` | Update tests for new audit() behavior |
| Modify | `tests/agent/test_orchestrator.py` | Update tests for SubAgentRunner integration |
| Modify | `tests/agent/test_regression.py` | Update regression tests for new flow |

---

### Task 1: _CallbackHolder

**Files:**
- Create: `src/agent/agents/subagent.py`
- Test: `tests/agent/test_subagent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_subagent.py
"""Tests for SubAgentRunner infrastructure."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from src.agent.agents.subagent import _CallbackHolder


class TestCallbackHolder:
    def test_make_on_step_prepends_name(self):
        events = []
        holder = _CallbackHolder(
            on_step=AsyncMock(side_effect=lambda e, d: events.append((e, d))),
            on_token=AsyncMock(),
            on_content_token=AsyncMock(),
            on_tool_result=AsyncMock(),
        )
        callback = holder.make_on_step("format_auditor")
        # Callbacks are async
        import asyncio
        asyncio.get_event_loop().run_until_complete(callback("think", "tool_calls: x"))
        assert events == [("[format_auditor] think", "tool_calls: x")]

    def test_make_on_token_prepends_name(self):
        tokens = []
        holder = _CallbackHolder(
            on_step=AsyncMock(),
            on_token=AsyncMock(side_effect=lambda t: tokens.append(t)),
            on_content_token=AsyncMock(),
            on_tool_result=AsyncMock(),
        )
        callback = holder.make_on_token("content_auditor")
        import asyncio
        asyncio.get_event_loop().run_until_complete(callback("hello"))
        assert tokens == ["[content_auditor] hello"]

    def test_make_on_content_token_prepends_name(self):
        tokens = []
        holder = _CallbackHolder(
            on_step=AsyncMock(),
            on_token=AsyncMock(),
            on_content_token=AsyncMock(side_effect=lambda t: tokens.append(t)),
            on_tool_result=AsyncMock(),
        )
        callback = holder.make_on_content_token("style_auditor")
        import asyncio
        asyncio.get_event_loop().run_until_complete(callback("world"))
        assert tokens == ["[style_auditor] world"]

    def test_make_on_tool_result_prepends_name(self):
        results = []
        holder = _CallbackHolder(
            on_step=AsyncMock(),
            on_token=AsyncMock(),
            on_content_token=AsyncMock(),
            on_tool_result=AsyncMock(side_effect=lambda n, s: results.append((n, s))),
        )
        callback = holder.make_on_tool_result("plagiarism_auditor")
        import asyncio
        asyncio.get_event_loop().run_until_complete(callback("detect_plagiarism", "完成"))
        assert results == [("[plagiarism_auditor] detect_plagiarism", "完成")]

    def test_returns_none_when_parent_callback_is_none(self):
        holder = _CallbackHolder(
            on_step=None,
            on_token=None,
            on_content_token=None,
            on_tool_result=None,
        )
        assert holder.make_on_step("x") is None
        assert holder.make_on_token("x") is None
        assert holder.make_on_content_token("x") is None
        assert holder.make_on_tool_result("x") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.agents.subagent'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/subagent.py
"""SubAgentRunner — manages subagent lifecycle, tool generation, and dispatch."""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .base import Agent, AgentResult
from ..tools.protocol import ToolProtocol, ToolResult

logger = logging.getLogger(__name__)


class FailureStrategy(str, Enum):
    STRICT = "strict"
    TOLERANT = "tolerant"
    RETRY = "retry"


@dataclass(frozen=True)
class SubAgentConfig:
    agent: Agent
    failure_strategy: FailureStrategy
    max_retries: int = 0
    description: str = ""


class _CallbackHolder:
    """Holds parent-level callbacks and creates prefixed sub-callbacks."""

    def __init__(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, str], Awaitable[None]] | None,
    ) -> None:
        self._on_step = on_step
        self._on_token = on_token
        self._on_content_token = on_content_token
        self._on_tool_result = on_tool_result

    def make_on_step(self, name: str) -> Callable[[str, str], Awaitable[None]] | None:
        if self._on_step is None:
            return None
        prefix = f"[{name}]"

        async def _forward(event: str, detail: str) -> None:
            await self._on_step(f"{prefix} {event}", detail)

        return _forward

    def make_on_token(self, name: str) -> Callable[[str], Awaitable[None]] | None:
        if self._on_token is None:
            return None
        prefix = f"[{name}]"

        async def _forward(token: str) -> None:
            await self._on_token(f"{prefix}{token}")

        return _forward

    def make_on_content_token(self, name: str) -> Callable[[str], Awaitable[None]] | None:
        if self._on_content_token is None:
            return None
        prefix = f"[{name}]"

        async def _forward(token: str) -> None:
            await self._on_content_token(f"{prefix}{token}")

        return _forward

    def make_on_tool_result(self, name: str) -> Callable[[str, str], Awaitable[None]] | None:
        if self._on_tool_result is None:
            return None
        prefix = f"[{name}]"

        async def _forward(tool_name: str, summary: str) -> None:
            await self._on_tool_result(f"{prefix} {tool_name}", summary)

        return _forward
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py::TestCallbackHolder -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: add _CallbackHolder for subagent callback forwarding"
```

---

### Task 2: _extract_result_data and _describe_failure

**Files:**
- Modify: `src/agent/agents/subagent.py`
- Modify: `tests/agent/test_subagent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_subagent.py`:

```python
from src.agent.agents.subagent import _extract_result_data, _describe_failure
from src.agent.agents.base import AgentResult
from src.agent.tools.protocol import ToolResult
from src.agent.core.state import AgentStatus


class TestExtractResultData:
    @pytest.mark.asyncio
    async def test_extracts_last_successful_tool_data(self):
        result = AgentResult(
            status="completed",
            content="Audit complete",
            tool_results=(
                ToolResult(success=True, data={"doc_type": "通知"}),
                ToolResult(success=True, data={"errors": []}),
            ),
        )
        extracted = _extract_result_data(result)
        assert extracted == {"content": "Audit complete", "data": {"errors": []}}

    @pytest.mark.asyncio
    async def test_falls_back_to_content_when_no_tool_results(self):
        result = AgentResult(
            status="completed",
            content="All checks passed",
            tool_results=(),
        )
        extracted = _extract_result_data(result)
        assert extracted == {"content": "All checks passed"}

    @pytest.mark.asyncio
    async def test_falls_back_to_content_when_all_tool_results_failed(self):
        result = AgentResult(
            status="completed",
            content="Some issues found",
            tool_results=(
                ToolResult(success=False, error="Tool error"),
            ),
        )
        extracted = _extract_result_data(result)
        assert extracted == {"content": "Some issues found"}


class TestDescribeFailure:
    def test_describes_error_status(self):
        result = AgentResult(
            status="error",
            content="Something went wrong",
            termination_reason="model_error",
        )
        desc = _describe_failure(result)
        assert "error" in desc
        assert "model_error" in desc

    def test_describes_blocked_status(self):
        result = AgentResult(
            status="blocked",
            content=None,
            termination_reason="Permission denied: dangerous_tool",
        )
        desc = _describe_failure(result)
        assert "blocked" in desc
        assert "Permission denied" in desc

    def test_describes_with_no_reason(self):
        result = AgentResult(
            status="error",
            content="fail",
            termination_reason=None,
        )
        desc = _describe_failure(result)
        assert "error" in desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py::TestExtractResultData tests/agent/test_subagent.py::TestDescribeFailure -v`
Expected: FAIL — `ImportError: cannot import name '_extract_result_data'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/agent/agents/subagent.py`:

```python
def _extract_result_data(result: AgentResult) -> dict[str, Any]:
    """Extract structured data from an AgentResult."""
    if result.tool_results:
        for tr in reversed(result.tool_results):
            if tr.success and tr.data is not None:
                return {"content": result.content, "data": tr.data}
    return {"content": result.content}


def _describe_failure(result: AgentResult) -> str:
    """Describe why a subagent failed."""
    parts = [f"status={result.status}"]
    if result.termination_reason:
        parts.append(f"reason={result.termination_reason}")
    if result.content:
        parts.append(f"content={result.content[:200]}")
    return ", ".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py::TestExtractResultData tests/agent/test_subagent.py::TestDescribeFailure -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: add _extract_result_data and _describe_failure helpers"
```

---

### Task 3: SubAgentRunner.dispatch with failure strategies

**Files:**
- Modify: `src/agent/agents/subagent.py`
- Modify: `tests/agent/test_subagent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_subagent.py`:

```python
from src.agent.agents.subagent import SubAgentRunner, FailureStrategy, SubAgentConfig
from src.agent.agents.base import Agent
from src.agent.core.model import MockModelClient


def _make_agent(model=None, name="TestAgent") -> Agent:
    return Agent(
        name=name,
        role="Test role",
        model=model or MockModelClient(tool_calls=[]),
    )


class TestSubAgentRunnerDefine:
    def test_define_registers_config(self):
        runner = SubAgentRunner()
        agent = _make_agent(name="FormatAuditor")
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.STRICT,
        )
        runner.define("format_auditor", config)
        assert "format_auditor" in runner._configs
        assert runner._configs["format_auditor"].agent is agent


class TestSubAgentRunnerBuildTools:
    def test_build_tools_returns_subagent_tools(self):
        runner = SubAgentRunner()
        agent = _make_agent(name="FormatAuditor")
        runner.define("format_auditor", SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
        ))
        tools = runner.build_tools()
        assert len(tools) == 1
        assert tools[0].name == "run_format_auditor"

    def test_build_tools_multiple_subagents(self):
        runner = SubAgentRunner()
        runner.define("format_auditor", SubAgentConfig(
            agent=_make_agent(name="FormatAuditor"),
            failure_strategy=FailureStrategy.STRICT,
        ))
        runner.define("content_auditor", SubAgentConfig(
            agent=_make_agent(name="ContentAuditor"),
            failure_strategy=FailureStrategy.TOLERANT,
        ))
        tools = runner.build_tools()
        names = {t.name for t in tools}
        assert names == {"run_format_auditor", "run_content_auditor"}


class TestSubAgentRunnerDispatch:
    @pytest.mark.asyncio
    async def test_normal_completion(self):
        runner = SubAgentRunner()
        agent = _make_agent(model=MockModelClient(tool_calls=[]), name="FormatAuditor")
        runner.define("format_auditor", SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.STRICT,
        ))
        result = await runner.dispatch("format_auditor", task="Check formatting")
        assert result.success is True
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_strict_failure_raises(self):
        runner = SubAgentRunner()

        class FailingAgent(Agent):
            async def run(self, task, **kwargs):
                raise RuntimeError("Agent crashed")

        runner.define("fail_agent", SubAgentConfig(
            agent=FailingAgent(name="FailAgent", role="fail"),
            failure_strategy=FailureStrategy.STRICT,
        ))
        with pytest.raises(RuntimeError, match="Agent crashed"):
            await runner.dispatch("fail_agent", task="do something")

    @pytest.mark.asyncio
    async def test_tolerant_failure_returns_error(self):
        runner = SubAgentRunner()

        class FailingAgent(Agent):
            async def run(self, task, **kwargs):
                raise RuntimeError("Agent crashed")

        runner.define("fail_agent", SubAgentConfig(
            agent=FailingAgent(name="FailAgent", role="fail"),
            failure_strategy=FailureStrategy.TOLERANT,
        ))
        result = await runner.dispatch("fail_agent", task="do something")
        assert result.success is False
        assert "Agent crashed" in result.error

    @pytest.mark.asyncio
    async def test_retry_success_on_second_attempt(self):
        runner = SubAgentRunner()
        call_count = 0

        class RetryAgent(Agent):
            async def run(self, task, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("Transient failure")
                return AgentResult(status="completed", content="OK")

        runner.define("retry_agent", SubAgentConfig(
            agent=RetryAgent(name="RetryAgent", role="retry"),
            failure_strategy=FailureStrategy.RETRY,
            max_retries=2,
        ))
        result = await runner.dispatch("retry_agent", task="try again")
        assert result.success is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_error(self):
        runner = SubAgentRunner()

        class AlwaysFailAgent(Agent):
            async def run(self, task, **kwargs):
                raise RuntimeError("Permanent failure")

        runner.define("fail_agent", SubAgentConfig(
            agent=AlwaysFailAgent(name="FailAgent", role="fail"),
            failure_strategy=FailureStrategy.RETRY,
            max_retries=2,
        ))
        result = await runner.dispatch("fail_agent", task="try anyway")
        assert result.success is False
        assert "Permanent failure" in result.error

    @pytest.mark.asyncio
    async def test_callbacks_forwarded_with_prefix(self):
        runner = SubAgentRunner()
        steps = []

        async def on_step(event, detail):
            steps.append((event, detail))

        agent = _make_agent(model=MockModelClient(tool_calls=[]), name="FormatAuditor")
        runner.define("format_auditor", SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.STRICT,
        ))
        holder = _CallbackHolder(
            on_step=on_step,
            on_token=None,
            on_content_token=None,
            on_tool_result=None,
        )
        result = await runner.dispatch(
            "format_auditor", task="Check formatting", callbacks=holder,
        )
        assert result.success is True
        assert any("[format_auditor]" in e for e, _ in steps)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py::TestSubAgentRunner -v`
Expected: FAIL — `TypeError` or `AttributeError` because `SubAgentRunner` doesn't exist yet

- [ ] **Step 3: Write minimal implementation**

Append to `src/agent/agents/subagent.py`:

```python
class SubAgentRunner:
    """Manages subagent lifecycle: registration, tool generation, dispatch."""

    def __init__(self) -> None:
        self._configs: dict[str, SubAgentConfig] = {}
        self._callback_holder: _CallbackHolder | None = None

    def define(self, name: str, config: SubAgentConfig) -> None:
        """Register a subagent configuration."""
        self._configs[name] = config

    def build_tools(self) -> list[ToolProtocol]:
        """Generate _SubAgentTool instances for all registered subagents."""
        return [
            _SubAgentTool(name=name, config=config, runner=self)
            for name, config in self._configs.items()
        ]

    def set_callbacks(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, str], Awaitable[None]] | None,
    ) -> None:
        """Set parent callbacks for all subsequent dispatches."""
        self._callback_holder = _CallbackHolder(
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
        )

    async def dispatch(
        self,
        name: str,
        task: str,
        context: dict[str, str] | None = None,
        callbacks: _CallbackHolder | None = None,
    ) -> ToolResult:
        """Execute a subagent with failure strategy handling."""
        config = self._configs[name]
        agent = config.agent
        cb = callbacks or self._callback_holder

        attempts = 1 + config.max_retries

        for attempt in range(attempts):
            try:
                result = await agent.run(
                    task=task,
                    context=context,
                    on_step=cb.make_on_step(name) if cb else None,
                    on_token=cb.make_on_token(name) if cb else None,
                    on_content_token=cb.make_on_content_token(name) if cb else None,
                    on_tool_result=cb.make_on_tool_result(name) if cb else None,
                )

                if result.status == "completed":
                    return ToolResult(
                        success=True,
                        data=_extract_result_data(result),
                    )

                if config.failure_strategy == FailureStrategy.STRICT:
                    return ToolResult(
                        success=False, error=_describe_failure(result),
                    )
                if config.failure_strategy == FailureStrategy.RETRY and attempt < attempts - 1:
                    continue
                return ToolResult(
                    success=False, error=_describe_failure(result),
                )

            except Exception as exc:
                if config.failure_strategy == FailureStrategy.STRICT:
                    raise
                if config.failure_strategy == FailureStrategy.RETRY and attempt < attempts - 1:
                    continue
                return ToolResult(success=False, error=str(exc))

        return ToolResult(success=False, error="All retries exhausted")
```

Also add the `_SubAgentTool` class:

```python
class _SubAgentTool:
    """Adapter: makes a SubAgentConfig callable as a ToolProtocol."""

    def __init__(
        self,
        name: str,
        config: SubAgentConfig,
        runner: SubAgentRunner,
    ) -> None:
        self.name = f"run_{name}"
        self.description = config.description or config.agent.role
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description to delegate to the sub-agent.",
                },
            },
            "required": ["task"],
        }
        self._name = name
        self._runner = runner

    async def execute(self, **kwargs: Any) -> ToolResult:
        task = kwargs["task"]
        return await self._runner.dispatch(self._name, task=task)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py::TestSubAgentRunner -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: add SubAgentRunner with dispatch and failure strategies"
```

---

### Task 4: Update __init__.py exports

**Files:**
- Modify: `src/agent/agents/__init__.py`

- [ ] **Step 1: Update exports**

```python
# src/agent/agents/__init__.py
"""Agent implementations."""
from .base import Agent, AgentResult
from .parser import ParserAgent
from .exporter import ExporterAgent
from .format_auditor import FormatAuditorAgent
from .content_auditor import ContentAuditorAgent
from .correction_auditor import CorrectionAuditorAgent
from .plagiarism_auditor import PlagiarismAuditorAgent
from .style_auditor import StyleAuditorAgent
from .orch import OrchestratorAgent
from .assistant import AssistantAgent
from .subagent import SubAgentRunner, SubAgentConfig, FailureStrategy

__all__ = [
    "Agent",
    "AgentResult",
    "ParserAgent",
    "ExporterAgent",
    "FormatAuditorAgent",
    "ContentAuditorAgent",
    "CorrectionAuditorAgent",
    "PlagiarismAuditorAgent",
    "StyleAuditorAgent",
    "OrchestratorAgent",
    "AssistantAgent",
    "SubAgentRunner",
    "SubAgentConfig",
    "FailureStrategy",
]
```

- [ ] **Step 2: Verify import works**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -c "from src.agent.agents import SubAgentRunner, FailureStrategy, SubAgentConfig; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/agent/agents/__init__.py
git commit -m "feat: export SubAgentRunner, SubAgentConfig, FailureStrategy from __init__"
```

---

### Task 5: Rewrite FormatAuditorAgent.audit()

**Files:**
- Modify: `src/agent/agents/format_auditor.py`
- Modify: `tests/agent/test_domain_agents.py`

- [ ] **Step 1: Write the failing tests**

Replace `TestFormatAuditorAgent` in `tests/agent/test_domain_agents.py`:

```python
class TestFormatAuditorAgent:
    def test_format_auditor_extends_agent_base(self):
        from src.agent.agents.base import Agent
        from src.agent.agents.format_auditor import FormatAuditorAgent
        agent = FormatAuditorAgent()
        assert isinstance(agent, Agent)

    def test_has_all_four_format_tools(self):
        from src.agent.agents.format_auditor import FormatAuditorAgent
        agent = FormatAuditorAgent()
        tool_names = [t.name for t in agent.tool_registry.list_tools()]
        assert "detect_document_type" in tool_names
        assert "audit_format" in tool_names
        assert "load_format_spec" in tool_names
        assert "list_format_rule_types" in tool_names

    @pytest.mark.asyncio
    async def test_audit_delegates_to_run(self):
        """audit() should call self.run() with a task string."""
        from src.agent.agents.format_auditor import FormatAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = FormatAuditorAgent()
        doc = {"title": "关于开展安全检查的通知", "pages": [{"text": "内容"}]}

        run_result = AgentResult(
            status="completed",
            content='{"doc_type": "通知", "errors": []}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit(doc)
        agent.run.assert_called_once()
        task_arg = agent.run.call_args[1]["task"]
        assert "审核" in task_arg
        assert "关于开展安全检查的通知" in task_arg

    @pytest.mark.asyncio
    async def test_audit_with_doc_type_hint(self):
        """audit() passes doc_type hint to the task string."""
        from src.agent.agents.format_auditor import FormatAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = FormatAuditorAgent()
        doc = {"title": "测试", "pages": []}
        run_result = AgentResult(
            status="completed",
            content='{"doc_type": "函", "errors": []}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit(doc, doc_type="函")
        task_arg = agent.run.call_args[1]["task"]
        assert "函" in task_arg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestFormatAuditorAgent -v`
Expected: FAIL — old test `test_audit_detects_doc_type_then_audits` expects `tool_registry.execute` to be called directly, but new tests expect `run()` to be called

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/format_auditor.py
"""FormatAuditorAgent — wraps format audit tools for GB/T 9704-2012 compliance."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, AgentResult
from ..skills.loader import SkillLoader


class FormatAuditorAgent(Agent):
    """Audits document formatting against GB/T 9704-2012.

    Delegates to agent_loop via self.run() — LLM decides which tools to call.
    """

    def __init__(self, **kwargs: Any) -> None:
        loader = SkillLoader()
        skill = loader.load("format_audit")
        super().__init__(
            name="FormatAuditorAgent",
            role="Audit document formatting against GB/T 9704-2012 standards.",
            tools=list(skill.tools),
            **kwargs,
        )

    async def audit(
        self, doc: dict[str, Any], doc_type: str | None = None
    ) -> dict[str, Any]:
        """Run format audit via agent_loop and return {doc_type, errors}."""
        doc_json = json.dumps(doc, ensure_ascii=False, default=str)
        task = f"审核以下文档的格式。\n文档内容：{doc_json}"
        if doc_type:
            task += f"\n文档类型：{doc_type}"
        result = await self.run(task=task)
        return _parse_audit_result(result)


def _parse_audit_result(result: AgentResult) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"doc_type": None, "errors": [], "summary": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestFormatAuditorAgent -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/format_auditor.py tests/agent/test_domain_agents.py
git commit -m "refactor: FormatAuditorAgent.audit() delegates to self.run() via agent_loop"
```

---

### Task 6: Rewrite ContentAuditorAgent.audit()

**Files:**
- Modify: `src/agent/agents/content_auditor.py`
- Modify: `tests/agent/test_domain_agents.py`

- [ ] **Step 1: Write the failing tests**

Replace `TestContentAuditorAgent` in `tests/agent/test_domain_agents.py`:

```python
class TestContentAuditorAgent:
    def test_content_auditor_extends_agent_base(self):
        from src.agent.agents.base import Agent
        from src.agent.agents.content_auditor import ContentAuditorAgent
        agent = ContentAuditorAgent()
        assert isinstance(agent, Agent)

    def test_has_required_tools(self):
        from src.agent.agents.content_auditor import ContentAuditorAgent
        agent = ContentAuditorAgent()
        tool_names = [t.name for t in agent.tool_registry.list_tools()]
        assert "audit_content" in tool_names
        assert "classify_domain" in tool_names

    @pytest.mark.asyncio
    async def test_audit_delegates_to_run(self):
        from src.agent.agents.content_auditor import ContentAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = ContentAuditorAgent()
        run_result = AgentResult(
            status="completed",
            content='{"domain": "通知", "violations": []}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit(["段落1"])
        agent.run.assert_called_once()
        task_arg = agent.run.call_args[1]["task"]
        assert "段落1" in task_arg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestContentAuditorAgent -v`
Expected: FAIL — old test expects `tool_registry.execute` call sequence

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/content_auditor.py
"""ContentAuditorAgent — wraps content compliance audit tools."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, AgentResult
from ..skills.loader import SkillLoader


class ContentAuditorAgent(Agent):
    """Audits document content for compliance violations.

    Delegates to agent_loop via self.run() — LLM decides which tools to call.
    """

    def __init__(self, **kwargs: Any) -> None:
        loader = SkillLoader()
        skill = loader.load("content_audit")
        super().__init__(
            name="ContentAuditorAgent",
            role="Audit document content for compliance violations.",
            tools=list(skill.tools),
            **kwargs,
        )

    async def audit(self, paragraphs: list[str]) -> dict[str, Any]:
        """Run content audit via agent_loop and return {domain, violations}."""
        text = "\n".join(paragraphs)
        task = f"审核以下文本的内容合规性。\n文本内容：{text}"
        result = await self.run(task=task)
        return _parse_audit_result(result)


def _parse_audit_result(result: AgentResult) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"domain": "通用", "violations": [], "summary": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestContentAuditorAgent -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/content_auditor.py tests/agent/test_domain_agents.py
git commit -m "refactor: ContentAuditorAgent.audit() delegates to self.run() via agent_loop"
```

---

### Task 7: Rewrite CorrectionAuditorAgent.audit()

**Files:**
- Modify: `src/agent/agents/correction_auditor.py`
- Modify: `tests/agent/test_domain_agents.py`

- [ ] **Step 1: Write the failing tests**

Replace `TestCorrectionAuditorAgent` in `tests/agent/test_domain_agents.py`:

```python
class TestCorrectionAuditorAgent:
    def test_correction_auditor_extends_agent_base(self):
        from src.agent.agents.base import Agent
        from src.agent.agents.correction_auditor import CorrectionAuditorAgent
        agent = CorrectionAuditorAgent()
        assert isinstance(agent, Agent)

    def test_has_all_five_tools(self):
        from src.agent.agents.correction_auditor import CorrectionAuditorAgent
        agent = CorrectionAuditorAgent()
        tool_names = [t.name for t in agent.tool_registry.list_tools()]
        assert len(tool_names) == 5

    @pytest.mark.asyncio
    async def test_audit_delegates_to_run(self):
        from src.agent.agents.correction_auditor import CorrectionAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = CorrectionAuditorAgent()
        run_result = AgentResult(
            status="completed",
            content='{"corrections": []}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit(["测试"])
        agent.run.assert_called_once()
        task_arg = agent.run.call_args[1]["task"]
        assert "测试" in task_arg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestCorrectionAuditorAgent -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/correction_auditor.py
"""CorrectionAuditorAgent — wraps text correction tools."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, AgentResult
from ..skills.loader import SkillLoader


class CorrectionAuditorAgent(Agent):
    """Corrects text errors in documents.

    Delegates to agent_loop via self.run() — LLM decides which tools to call.
    """

    def __init__(self, **kwargs: Any) -> None:
        loader = SkillLoader()
        skill = loader.load("text_correction")
        super().__init__(
            name="CorrectionAuditorAgent",
            role="Correct text errors in Chinese government documents.",
            tools=list(skill.tools),
            **kwargs,
        )

    async def audit(self, paragraphs: list[str]) -> dict[str, Any]:
        """Run correction checks via agent_loop and return combined results."""
        text = "\n".join(paragraphs)
        task = f"检查以下文本中的文字错误并进行纠正。\n文本内容：{text}"
        result = await self.run(task=task)
        return _parse_audit_result(result)


def _parse_audit_result(result: AgentResult) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"corrections": [], "summary": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestCorrectionAuditorAgent -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/correction_auditor.py tests/agent/test_domain_agents.py
git commit -m "refactor: CorrectionAuditorAgent.audit() delegates to self.run() via agent_loop"
```

---

### Task 8: Rewrite StyleAuditorAgent.audit()

**Files:**
- Modify: `src/agent/agents/style_auditor.py`
- Modify: `tests/agent/test_domain_agents.py`

- [ ] **Step 1: Write the failing tests**

Replace `TestStyleAuditorAgent` in `tests/agent/test_domain_agents.py`:

```python
class TestStyleAuditorAgent:
    def test_style_auditor_extends_agent_base(self):
        from src.agent.agents.base import Agent
        from src.agent.agents.style_auditor import StyleAuditorAgent
        agent = StyleAuditorAgent()
        assert isinstance(agent, Agent)

    def test_has_required_tools(self):
        from src.agent.agents.style_auditor import StyleAuditorAgent
        agent = StyleAuditorAgent()
        tool_names = [t.name for t in agent.tool_registry.list_tools()]
        assert "audit_writing_style" in tool_names

    @pytest.mark.asyncio
    async def test_audit_delegates_to_run(self):
        from src.agent.agents.style_auditor import StyleAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = StyleAuditorAgent()
        run_result = AgentResult(
            status="completed",
            content='{"is_valid": true, "violations": []}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit("测试文本", doc_type="通知")
        agent.run.assert_called_once()
        task_arg = agent.run.call_args[1]["task"]
        assert "测试文本" in task_arg
        assert "通知" in task_arg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestStyleAuditorAgent -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/style_auditor.py
"""StyleAuditorAgent — wraps writing style audit tools."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, AgentResult
from ..skills.loader import SkillLoader


class StyleAuditorAgent(Agent):
    """Audits document writing style against compliance rules.

    Delegates to agent_loop via self.run() — LLM decides which tools to call.
    """

    def __init__(self, **kwargs: Any) -> None:
        loader = SkillLoader()
        skill = loader.load("style_audit")
        super().__init__(
            name="StyleAuditorAgent",
            role="Audit document writing style against content compliance rules.",
            tools=list(skill.tools),
            **kwargs,
        )

    async def audit(
        self, text: str, doc_type: str = "通知"
    ) -> dict[str, Any]:
        """Run style audit via agent_loop and return {is_valid, violations}."""
        task = f"审核以下文本的行文风格。\n文档类型：{doc_type}\n文本内容：{text}"
        result = await self.run(task=task)
        return _parse_audit_result(result)


def _parse_audit_result(result: AgentResult) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"is_valid": True, "violations": [], "summary": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestStyleAuditorAgent -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/style_auditor.py tests/agent/test_domain_agents.py
git commit -m "refactor: StyleAuditorAgent.audit() delegates to self.run() via agent_loop"
```

---

### Task 9: Rewrite PlagiarismAuditorAgent.audit()

**Files:**
- Modify: `src/agent/agents/plagiarism_auditor.py`
- Modify: `tests/agent/test_domain_agents.py`

- [ ] **Step 1: Write the failing tests**

Replace `TestPlagiarismAuditorAgent` in `tests/agent/test_domain_agents.py`:

```python
class TestPlagiarismAuditorAgent:
    def test_plagiarism_auditor_extends_agent_base(self):
        from src.agent.agents.base import Agent
        from src.agent.agents.plagiarism_auditor import PlagiarismAuditorAgent
        agent = PlagiarismAuditorAgent()
        assert isinstance(agent, Agent)

    def test_has_detect_plagiarism_tool(self):
        from src.agent.agents.plagiarism_auditor import PlagiarismAuditorAgent
        agent = PlagiarismAuditorAgent()
        tool_names = [t.name for t in agent.tool_registry.list_tools()]
        assert "detect_plagiarism" in tool_names

    @pytest.mark.asyncio
    async def test_audit_delegates_to_run(self):
        from src.agent.agents.plagiarism_auditor import PlagiarismAuditorAgent
        from src.agent.agents.base import AgentResult
        agent = PlagiarismAuditorAgent()
        run_result = AgentResult(
            status="completed",
            content='{"is_plagiarism": false}',
            tool_results=(),
        )
        agent.run = AsyncMock(return_value=run_result)
        result = await agent.audit(["段落1"], ["库文档1"])
        agent.run.assert_called_once()
        task_arg = agent.run.call_args[1]["task"]
        assert "段落1" in task_arg
        assert "库文档1" in task_arg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestPlagiarismAuditorAgent -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/plagiarism_auditor.py
"""PlagiarismAuditorAgent — wraps plagiarism detection tools."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, AgentResult
from ..skills.loader import SkillLoader


class PlagiarismAuditorAgent(Agent):
    """Detects plagiarism by comparing document text against a reference library.

    Delegates to agent_loop via self.run() — LLM decides which tools to call.
    """

    def __init__(self, **kwargs: Any) -> None:
        loader = SkillLoader()
        skill = loader.load("plagiarism")
        super().__init__(
            name="PlagiarismAuditorAgent",
            role="Detect plagiarism by comparing document text against a reference library.",
            tools=list(skill.tools),
            **kwargs,
        )

    async def audit(
        self, new_doc: list[str], library_docs: list[str], k: int = 5
    ) -> dict[str, Any]:
        """Run plagiarism detection via agent_loop and return results."""
        task = (
            f"检测以下文本是否存在抄袭。\n"
            f"待检测文本：{chr(10).join(new_doc)}\n"
            f"参考库文档：{chr(10).join(library_docs)}\n"
            f"Top-K：{k}"
        )
        result = await self.run(task=task)
        return _parse_audit_result(result)


def _parse_audit_result(result: AgentResult) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"is_plagiarism": False, "summary": content}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_domain_agents.py::TestPlagiarismAuditorAgent -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/plagiarism_auditor.py tests/agent/test_domain_agents.py
git commit -m "refactor: PlagiarismAuditorAgent.audit() delegates to self.run() via agent_loop"
```

---

### Task 10: Rewrite OrchestratorAgent with SubAgentRunner

**Files:**
- Modify: `src/agent/agents/orch.py`
- Modify: `tests/agent/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Replace entire `tests/agent/test_orchestrator.py`:

```python
"""Tests for OrchestratorAgent with SubAgentRunner."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agent.core.model import MockModelClient, ToolCall
from src.agent.agents.subagent import FailureStrategy


class TestOrchestratorAgent:
    def _make_orchestrator(self, tool_calls=None):
        """Build OrchestratorAgent with mocked components."""
        from src.agent.agents.orch import OrchestratorAgent

        parser = MagicMock()
        parser.name = "ParserAgent"
        parser.parse = AsyncMock(return_value={
            "title": "测试通知", "doc_type": "通知",
            "pages": [{"text": "段落1"}],
        })

        model = MockModelClient(tool_calls=tool_calls or [])

        return OrchestratorAgent(
            parser=parser,
            model=model,
        )

    def test_orchestrator_extends_agent_base(self):
        from src.agent.agents.base import Agent
        orch = self._make_orchestrator()
        assert isinstance(orch, Agent)
        assert orch.name == "OrchestratorAgent"

    def test_subagent_tools_registered(self):
        orch = self._make_orchestrator()
        tool_names = [t.name for t in orch.tool_registry.list_tools()]
        assert "run_format_auditor" in tool_names
        assert "run_content_auditor" in tool_names
        assert "run_correction_auditor" in tool_names
        assert "run_plagiarism_auditor" in tool_names
        assert "run_style_auditor" in tool_names

    @pytest.mark.asyncio
    async def test_run_parses_then_loops(self):
        orch = self._make_orchestrator(
            tool_calls=[
                ToolCall(id="c1", name="run_format_auditor", arguments={"task": "审核格式"}),
            ]
        )
        result = await orch.run(
            task="Audit /path/to/doc.pdf",
            context={"file_path": "/path/to/doc.pdf"},
        )
        assert result.status == "completed"
        orch._parser.parse.assert_called_once_with("/path/to/doc.pdf")

    @pytest.mark.asyncio
    async def test_run_without_file_path_raises(self):
        orch = self._make_orchestrator()
        with pytest.raises(ValueError, match="file_path"):
            await orch.run(task="Audit something")

    @pytest.mark.asyncio
    async def test_pipeline_includes_export_phase(self):
        from src.agent.agents.orch import OrchestratorAgent

        parser = MagicMock()
        parser.name = "ParserAgent"
        parser.parse = AsyncMock(return_value={
            "title": "测试", "doc_type": "通知",
            "pages": [{"text": "p1"}],
        })

        exporter = MagicMock()
        exporter.name = "ExporterAgent"
        exporter.export = AsyncMock(return_value="/tmp/annotated.docx")

        model = MockModelClient(tool_calls=[
            ToolCall(id="c1", name="run_format_auditor", arguments={"task": "审核格式"}),
        ])

        orch = OrchestratorAgent(
            parser=parser, exporter=exporter, model=model,
        )
        result = await orch.run(
            task="Audit and export",
            context={"file_path": "/path/to/doc.pdf"},
        )

        assert result.status == "completed"
        exporter.export.assert_called_once()
        assert "_export_path" in orch.audit_results
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_orchestrator.py -v`
Expected: FAIL — old OrchestratorAgent constructor signature expects `auditors=` parameter

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent/agents/orch.py
"""OrchestratorAgent — composes domain agents for the full audit pipeline."""
from __future__ import annotations

import copy
import logging
from typing import Any

from .base import Agent, AgentResult
from .subagent import SubAgentRunner, SubAgentConfig, FailureStrategy
from .format_auditor import FormatAuditorAgent
from .content_auditor import ContentAuditorAgent
from .correction_auditor import CorrectionAuditorAgent
from .plagiarism_auditor import PlagiarismAuditorAgent
from .style_auditor import StyleAuditorAgent
from ..core.model import ModelClient, MockModelClient
from ..tools.builtin.read_cached import ReadCachedOutputTool
from ..hooks.chain import HookChain
from ..permissions.gate import PermissionGate

logger = logging.getLogger(__name__)

_SUBAGENTS = [
    ("format_auditor", FormatAuditorAgent, FailureStrategy.STRICT),
    ("content_auditor", ContentAuditorAgent, FailureStrategy.TOLERANT),
    ("correction_auditor", CorrectionAuditorAgent, FailureStrategy.TOLERANT),
    ("plagiarism_auditor", PlagiarismAuditorAgent, FailureStrategy.TOLERANT),
    ("style_auditor", StyleAuditorAgent, FailureStrategy.TOLERANT),
]


class OrchestratorAgent(Agent):
    """Composes domain agents for the full audit pipeline.

    Flow: parse → plan (LLM) → dispatch subagents → export.

    Domain agents run their own agent_loop via SubAgentRunner.
    """

    DEFAULT_SKILLS = [
        "parse",
        "format_audit",
        "content_audit",
        "style_audit",
        "text_correction",
        "plagiarism",
        "search",
        "annotate",
        "template",
    ]

    def __init__(
        self,
        parser: Any = None,
        exporter: Any = None,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        skills: list[str] | None = None,
    ) -> None:
        self._parser = parser
        self._exporter = exporter
        self._audit_results: dict[str, Any] = {}

        # Build subagents with shared model
        self._subagent_runner = SubAgentRunner()
        for name, agent_cls, strategy in _SUBAGENTS:
            agent = agent_cls(model=model or MockModelClient(tool_calls=[]))
            self._subagent_runner.define(name, SubAgentConfig(
                agent=agent,
                failure_strategy=strategy,
            ))

        # Build tools from subagents + read_cached
        tools = self._subagent_runner.build_tools()
        tools.append(ReadCachedOutputTool())

        role = (
            "You are the OrchestratorAgent for DocAudit document audit platform. "
            "Your role is to coordinate audit workflows. "
            "When you receive a document audit task, call the appropriate audit tools "
            "based on the document type and audit requirements. "
            "For government documents (通知/函/请示), always run format audit first "
            "to verify document structure, then run other audits as appropriate. "
            "After all audits complete, summarize the findings concisely."
        )

        super().__init__(
            name="OrchestratorAgent",
            role=role,
            tools=tools,
            skills=skills if skills is not None else self.DEFAULT_SKILLS,
            model=model or MockModelClient(tool_calls=[]),
            hooks=hooks,
            permissions=permissions,
        )
        self._prompt_pipeline.add_section(
            "AuditStrategy",
            "Available audit types correspond to the registered tools. "
            "For government documents, always run format audit first to determine "
            "the document type, then dispatch other audits as needed. "
            "You may run multiple audits in sequence.",
        )

    async def run(
        self,
        task: str,
        context: dict[str, str] | None = None,
        on_step: Any = None,
        on_token: Any = None,
        on_content_token: Any = None,
        on_tool_result: Any = None,
        context_manager: Any = None,
    ) -> AgentResult:
        """Run the full audit pipeline."""
        file_path = (context or {}).get("file_path", "")
        if not file_path:
            raise ValueError("context must include 'file_path'")

        # Phase 1: Parse document (deterministic — no LLM needed)
        if self._parser:
            if on_step:
                await on_step("parse", "parsing_document")
            parsed_doc = await self._parser.parse(file_path)

        # Set callbacks for subagent dispatch
        self._subagent_runner.set_callbacks(
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
        )

        # Phase 2: LLM-driven audit planning and dispatch (agent_loop)
        result = await super().run(
            task=task,
            context=context,
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
            context_manager=context_manager,
        )

        # Collect audit results from subagent tool results
        if result.final_state:
            for msg in result.final_state.messages:
                if msg.role == "tool" and msg.name:
                    name = msg.name
                    if name.startswith("run_"):
                        key = name[4:]  # strip "run_"
                        try:
                            import json
                            tool_data = json.loads(msg.content)
                            if tool_data.get("success") and tool_data.get("data"):
                                self._audit_results[key] = tool_data["data"]
                        except (json.JSONDecodeError, TypeError):
                            pass

        # Phase 3: Export annotated document with audit results
        if self._exporter and self._parser:
            try:
                audit_rules = _build_export_rules(self._audit_results)
                output_path = await self._exporter.export(
                    source=file_path, rules=audit_rules
                )
                self._audit_results["_export_path"] = output_path
                logger.info("Export complete: %s", output_path)
            except Exception as exc:
                logger.warning("Export failed: %s", exc)

        return result

    @property
    def audit_results(self) -> dict[str, Any]:
        """Aggregated results from all auditors, keyed by auditor name."""
        return copy.deepcopy(self._audit_results)


def _build_export_rules(audit_results: dict[str, Any]) -> list[dict[str, Any]]:
    """Build export rules list from audit results."""
    rules: list[dict[str, Any]] = []
    for key, value in audit_results.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            rules.append({"source": key, **value})
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    rules.append({"source": key, **item})
        else:
            rules.append({"source": key, "result": value})
    return rules
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_orchestrator.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/orch.py tests/agent/test_orchestrator.py
git commit -m "refactor: OrchestratorAgent uses SubAgentRunner instead of _AuditorAsTool"
```

---

### Task 11: Update regression tests

**Files:**
- Modify: `tests/agent/test_regression.py`

- [ ] **Step 1: Update regression tests**

The old tests pass `auditors=` to OrchestratorAgent constructor and assert `auditor.audit.assert_called_once()`. The new OrchestratorAgent creates its own domain agents internally, so we need to adapt.

Replace `tests/agent/test_regression.py`:

```python
"""Regression tests — full pipeline on 3 test assets.

Verifies the spec gate: "3 测试文件上全流程（上传→解析→全审→导出）输出一致"
Tests ceshi(1).docx, doc.pdf, 函1.pdf through parse → audit → export.

Uses real file paths and mocked tools — pipeline structure is the regression target.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.core.model import MockModelClient, ToolCall

ASSETS = [
    ("ceshi(1).docx", {"title": "测试通知", "doc_type": "通知", "pages": [{"text": "段落1"}]}),
    ("doc.pdf", {"title": "关于xxx的通知", "doc_type": "通知", "pages": [{"text": "段落内容"}]}),
    ("函1.pdf", {"title": "关于xxx的函", "doc_type": "函", "pages": [{"text": "函件内容"}]}),
]

SUBAGENT_TOOL_NAMES = [
    "run_format_auditor",
    "run_content_auditor",
    "run_correction_auditor",
    "run_plagiarism_auditor",
    "run_style_auditor",
]


def _build_tool_calls() -> list[ToolCall]:
    return [
        ToolCall(
            id=f"c{i+1}",
            name=name,
            arguments={"task": f"Run {name}"},
        )
        for i, name in enumerate(SUBAGENT_TOOL_NAMES)
    ]


class TestFullPipelineRegression:
    """Run the full parse→audit→export pipeline against 3 test assets."""

    @pytest.mark.asyncio
    async def test_pipeline_ceshi_docx(self):
        await self._run_pipeline_for_asset(*ASSETS[0])

    @pytest.mark.asyncio
    async def test_pipeline_doc_pdf(self):
        await self._run_pipeline_for_asset(*ASSETS[1])

    @pytest.mark.asyncio
    async def test_pipeline_han1_pdf(self):
        await self._run_pipeline_for_asset(*ASSETS[2])

    @pytest.mark.asyncio
    async def test_all_three_assets_run_full_pipeline(self):
        """All 3 assets complete the full pipeline with consistent structure."""
        results = {}
        for filename, doc_dict in ASSETS:
            result = await self._run_pipeline_for_asset(filename, doc_dict)
            results[filename] = result

        for filename, (result, audit_results) in results.items():
            assert result.status == "completed", f"{filename}: must complete"
            assert "_export_path" in audit_results, (
                f"{filename}: must have export path"
            )

    async def _run_pipeline_for_asset(
        self, filename: str, doc_dict: dict | None = None
    ):
        """Run full parse→audit→export pipeline for a single asset."""
        from src.agent.agents.orch import OrchestratorAgent

        if doc_dict is None:
            doc_dict = {"title": "测试", "doc_type": "通知", "pages": []}

        parser = MagicMock()
        parser.name = "ParserAgent"
        parser.parse = AsyncMock(return_value=doc_dict)

        exporter = MagicMock()
        exporter.name = "ExporterAgent"
        exporter.export = AsyncMock(return_value=f"/tmp/annotated_{filename}")

        model = MockModelClient(tool_calls=_build_tool_calls())

        orch = OrchestratorAgent(
            parser=parser,
            exporter=exporter,
            model=model,
        )

        result = await orch.run(
            task=f"Audit document: {filename}",
            context={"file_path": f"/fake/path/{filename}"},
        )

        assert result.status == "completed", f"{filename}: pipeline must complete"
        assert result.data.get("steps", 0) > 0, f"{filename}: must have steps"

        exporter.export.assert_called_once()

        audit_results = orch.audit_results
        assert "_export_path" in audit_results

        return result, audit_results

    def test_all_subagent_tools_registered(self):
        """Verify all 5 subagent tools are registered in orchestrator."""
        from src.agent.agents.orch import OrchestratorAgent

        parser = MagicMock()
        parser.name = "ParserAgent"
        parser.parse = AsyncMock(return_value={"title": "t", "pages": []})

        orch = OrchestratorAgent(parser=parser)

        tool_names = [t.name for t in orch.tool_registry.list_tools()]
        for name in SUBAGENT_TOOL_NAMES:
            assert name in tool_names, f"Missing tool {name} in {tool_names}"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_regression.py -v`
Expected: PASS (5 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/agent/test_regression.py
git commit -m "test: update regression tests for SubAgentRunner integration"
```

---

### Task 12: Full test suite verification

**Files:**
- No new changes — verification only

- [ ] **Step 1: Run full agent test suite**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Run only subagent tests for confirmation**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_subagent.py -v`
Expected: All PASS (18 tests: 5 CallbackHolder + 6 ExtractResultData/DescribeFailure + 7 Runner)

- [ ] **Step 3: Final commit if any fixes needed**

If any test fixes were required during verification:

```bash
git add -A
git commit -m "fix: resolve test failures after SubAgentRunner integration"
```
