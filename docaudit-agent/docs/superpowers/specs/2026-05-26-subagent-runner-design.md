# SubAgent Runner Design

**Date:** 2026-05-26
**Status:** Draft

## Goal

Enable the OrchestratorAgent to delegate tasks to subagents that run independent LLM loops (think→act→observe). Subagent results flow back into the parent agent's message history so subsequent subagents can reference earlier conclusions.

## Decisions

| Decision | Choice |
|----------|--------|
| Scheduling | Hybrid: deterministic steps in code, flexible steps by LLM |
| Context sharing | Subagent results return to parent message history |
| Domain agent execution | All domain agents run through `agent_loop` |
| Model config | Shared `ModelClient` across all subagents |
| Failure handling | Configurable per subagent (strict / tolerant / retry) |
| Progress display | Transparent — subagent events bubble up with `[name]` prefix |
| Execution model | Single-shot delegation; architecture does not block future multi-turn |

## Architecture

```
OrchestratorAgent
  ├─ run()
  │    ├─ [code] parse document (deterministic)
  │    ├─ [LLM] agent_loop → SubAgentTool.execute()
  │    │              └─ SubAgentRunner.dispatch()
  │    │                    └─ agent.run()  ← independent agent_loop
  │    └─ [code] export results (deterministic)
  └─ SubAgentRunner
       ├─ define("format_auditor", Config(agent=..., strategy=STRICT))
       ├─ define("content_auditor", Config(agent=..., strategy=TOLERANT))
       └─ build_tools() → [_SubAgentTool, _SubAgentTool, ...]
```

## New Components

### File: `src/agent/agents/subagent.py`

#### FailureStrategy

```python
class FailureStrategy(str, Enum):
    STRICT = "strict"       # Failure terminates entire task, exception propagates
    TOLERANT = "tolerant"   # Failure continues, error returned as ToolResult
    RETRY = "retry"         # Retry up to max_retries times, then tolerate
```

#### SubAgentConfig

```python
@dataclass(frozen=True)
class SubAgentConfig:
    agent: Agent
    failure_strategy: FailureStrategy
    max_retries: int = 0
    description: str = ""   # Override agent.role as tool description
```

#### SubAgentRunner

```python
class SubAgentRunner:
    """Manages subagent lifecycle: registration, tool generation, dispatch."""

    def define(self, name: str, config: SubAgentConfig) -> None:
        """Register a subagent configuration."""

    def build_tools(self) -> list[ToolProtocol]:
        """Generate _SubAgentTool instances for all registered subagents."""

    async def dispatch(
        self,
        name: str,
        task: str,
        context: dict[str, str] | None = None,
        callbacks: _CallbackHolder | None = None,
    ) -> ToolResult:
        """Execute a subagent with failure strategy handling."""
```

#### _SubAgentTool

Adapter that implements `ToolProtocol`. Wraps a `SubAgentConfig` and delegates `execute()` to `SubAgentRunner.dispatch()`.

- `name`: `run_{name}` (e.g. `run_format_auditor`)
- `parameters`: `{"task": string (required)}` — the task description to delegate
- Callbacks forwarded from parent via `_CallbackHolder`

#### Callback Injection

`SubAgentRunner` holds a mutable `_callback_holder` attribute. The parent agent's `agent_loop` does not directly set this — instead, `_SubAgentTool.execute()` receives callbacks through a side-channel:

- `SubAgentRunner.set_callbacks(on_step, on_token, on_content_token, on_tool_result)` is called by `OrchestratorAgent.run()` before entering `super().run()`, making the callbacks available to all subagent dispatches.
- `_SubAgentTool.execute()` calls `self._runner.dispatch(name, task, context, self._runner._callback_holder)`.

This avoids modifying the `ToolProtocol.execute()` signature while giving subagent tools access to the current parent callbacks.

#### _CallbackHolder

Holds parent-level callback references. Creates prefixed sub-callbacks:

```python
class _CallbackHolder:
    def __init__(self, on_step, on_token, on_content_token, on_tool_result): ...

    def make_on_step(self, name: str) -> Callable:
        """Returns callback that prepends [name] to events."""

    def make_on_token(self, name: str) -> Callable: ...
    def make_on_content_token(self, name: str) -> Callable: ...
    def make_on_tool_result(self, name: str) -> Callable: ...
```

User sees subagent events with prefix:
```
[format_auditor] think: tool_calls: detect_document_type
[format_auditor] act: executing: detect_document_type
[format_auditor] observe: 完成 (2 个字段)
```

## Failure Strategy Logic

```
dispatch(name, task):
    config = registry[name]
    attempts = 1 + config.max_retries

    for attempt in range(attempts):
        try:
            result = await agent.run(task=task, ...)

            if result.status == "completed":
                return ToolResult(success=True, data=extract(result))

            # Non-terminal success (blocked, etc.)
            if config.strategy == STRICT:
                return ToolResult(success=False, error=describe(result))
            if config.strategy == RETRY and attempt < attempts - 1:
                continue
            return ToolResult(success=False, error=describe(result))

        except Exception:
            if config.strategy == STRICT:
                raise   # Propagates up, terminates parent agent_loop
            if config.strategy == RETRY and attempt < attempts - 1:
                continue
            return ToolResult(success=False, error=str(exc))
```

## Result Extraction

```python
def _extract_result_data(result: AgentResult) -> dict:
    if result.tool_results:
        for tr in reversed(result.tool_results):
            if tr.success and tr.data is not None:
                return {"content": result.content, "data": tr.data}
    return {"content": result.content}
```

The returned `ToolResult` enters the parent's message history via the normal `add_observation()` flow. Subsequent subagents can reference earlier results in their context.

## Domain Agent Changes

All domain agents follow the same pattern — `audit()` becomes a thin wrapper around `self.run()`:

**Before:**
```python
async def audit(self, doc, doc_type=None):
    result = await self.tool_registry.execute("detect_document_type", ...)
    doc_type = result.data
    result = await self.tool_registry.execute("audit_format", document=doc, ...)
    return {"doc_type": doc_type, "errors": result.data}
```

**After:**
```python
async def audit(self, doc, doc_type=None):
    doc_json = json.dumps(doc, ensure_ascii=False, default=str)
    task = f"审核以下文档的格式。\n文档内容：{doc_json}"
    if doc_type:
        task += f"\n文档类型：{doc_type}"
    result = await self.run(task=task)
    return _parse_audit_result(result)
```

The LLM now autonomously decides whether to call `detect_document_type` first or go straight to `audit_format`.

## OrchestratorAgent Integration

```python
class OrchestratorAgent(Agent):
    def __init__(self, parser, auditors, exporter, model, ...):
        # Create domain agents
        self._format_auditor = FormatAuditorAgent(model=model)
        self._content_auditor = ContentAuditorAgent(model=model)
        ...

        # Register subagents
        self._subagent_runner = SubAgentRunner()
        self._subagent_runner.define("format_auditor", SubAgentConfig(
            agent=self._format_auditor,
            failure_strategy=FailureStrategy.STRICT,
        ))
        self._subagent_runner.define("content_auditor", SubAgentConfig(
            agent=self._content_auditor,
            failure_strategy=FailureStrategy.TOLERANT,
        ))
        ...

        # Generate tools
        tools = self._subagent_runner.build_tools()
        super().__init__(
            name="OrchestratorAgent",
            role=...,
            tools=tools,
            skills=skills,
            model=model,
        )

    async def run(self, task, context=None, ...):
        # Deterministic: parse
        parsed_doc = await self._parser.parse(file_path)
        self._doc_ref["_doc"] = parsed_doc

        # Flexible: LLM dispatches subagents
        result = await super().run(task=task, context=context, ...)

        # Deterministic: export
        if self._exporter:
            await self._exporter.export(...)
        return result
```

## File Changes

### New Files

| File | Content |
|------|---------|
| `src/agent/agents/subagent.py` | `FailureStrategy`, `SubAgentConfig`, `SubAgentRunner`, `_SubAgentTool`, `_CallbackHolder` |
| `tests/agent/test_subagent.py` | Unit tests for runner, dispatch, strategies, callback forwarding |

### Modified Files

| File | Change |
|------|--------|
| `src/agent/agents/orch.py` | Remove `_AuditorAsTool`, use `SubAgentRunner` |
| `src/agent/agents/format_auditor.py` | `audit()` → `self.run()` |
| `src/agent/agents/content_auditor.py` | Same |
| `src/agent/agents/correction_auditor.py` | Same |
| `src/agent/agents/style_auditor.py` | Same |
| `src/agent/agents/plagiarism_auditor.py` | Same |

## Test Cases

| Test | Scenario |
|------|----------|
| Normal completion | Subagent completes, ToolResult.success=True, data correctly extracted |
| STRICT failure | Subagent raises, exception propagates, parent terminates |
| TOLERANT failure | Subagent raises, error returned as ToolResult, parent continues |
| RETRY success | First attempt fails, second succeeds |
| RETRY exhausted | All attempts fail, treated as TOLERANT |
| Callback forwarding | Subagent on_step/on_token events carry `[name]` prefix |
| Result context flow | Second subagent can reference first subagent's results in parent history |
