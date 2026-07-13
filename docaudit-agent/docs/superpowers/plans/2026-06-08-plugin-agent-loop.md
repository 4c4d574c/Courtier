# 插件 Agent Loop 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 PluginRuntime 中内置 `run_agent()` 方法，使插件子进程能运行完整的 LLM agent loop（think → tool_call → observe）。

**Architecture:** `run_agent()` 是 PluginRuntime 的 async generator 方法，内部创建 `AsyncOpenAI` client、构建系统提示词（含自动注入工具列表）、运行 think→act→observe 循环，yield `stream_event()` dict。插件 handler 一行委托 `async for event in self.run_agent(...): yield event`。宿主通过 `agent.run` params 传入 `model_config`（api_key, base_url, model）。

**Tech Stack:** Python 3.12+, openai, asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-06-08-plugin-agent-loop-design.md`

**依赖（已完成）：** `stream_event()` helper、`JSONRPCStreamChunk` 扩展、`SubAgentStreamEvent`、`SSEAdapter.on_subagent_event()`

---

## 文件结构

| 文件 | 角色 | 操作 |
|------|------|------|
| `src/plugin/sdk/runtime.py` | `run_agent()` + 5 个辅助方法 + `_tool_instances` 存储 | 修改 |
| `src/plugin/proxies.py` | ProxyAgent.run() 转发 model_config | 修改 |
| `src/agent/api/services/stream_service.py` | 传入 model_config 到 agent.run | 修改 |
| `plugins/format_audit/entry.py` | agent.run handler 改用 run_agent() | 修改 |
| `plugins/content_audit/entry.py` | agent.run handler 改用 run_agent() | 修改 |
| `plugins/style_audit/entry.py` | agent.run handler 改用 run_agent() | 修改 |
| `plugins/plagiarism/entry.py` | agent.run handler 改用 run_agent() | 修改 |
| `plugins/text_correction/entry.py` | agent.run handler 改用 run_agent() | 修改 |

---

### Task 1: register_tool() 存储工具实例

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestRegisterToolInstance:
    """Test that register_tool() stores instances for _execute_tool()."""

    def test_register_tool_stores_instance(self):
        """register_tool() 应该保存工具实例以便后续 _execute_tool() 调用。"""
        runtime = PluginRuntime()

        class FakeTool:
            name = "fake_tool"
            description = "A fake tool"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return type("Result", (), {"success": True, "data": kwargs})()

        tool = FakeTool()
        runtime.register_tool(tool)

        assert "fake_tool" in runtime._tool_instances
        assert runtime._tool_instances["fake_tool"] is tool
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestRegisterToolInstance -v
```
Expected: FAIL — `AttributeError: 'PluginRuntime' object has no attribute '_tool_instances'`

- [ ] **Step 3: 修改 PluginRuntime**

编辑 `src/plugin/sdk/runtime.py`。

在 `__init__` 方法中添加 `_tool_instances` dict。找到 `self._pending_caps` 初始化行（约第 112 行），在其后添加：

```python
        self._tool_instances: dict[str, Any] = {}
```

在 `register_tool` 方法末尾（`self._pending_caps.append(cap)` 之后），添加实例存储：

```python
        self._tool_instances[tool_instance.name] = tool_instance
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestRegisterToolInstance -v
```
Expected: PASS

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: store tool instances in register_tool() for later execution"
```

---

### Task 2: _build_tool_schemas() — 生成 OpenAI function-calling 格式

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestBuildToolSchemas:
    """Test _build_tool_schemas() generates OpenAI function-calling format."""

    def test_build_tool_schemas_from_registered_tools(self):
        runtime = PluginRuntime()

        class ToolA:
            name = "tool_a"
            description = "Tool A description"
            parameters = {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            }

        class ToolB:
            name = "tool_b"
            description = "Tool B description"
            parameters = {"type": "object", "properties": {}}

        runtime.register_tool(ToolA())
        runtime.register_tool(ToolB())

        schemas = runtime._build_tool_schemas()

        assert len(schemas) == 2
        assert schemas[0] == {
            "type": "function",
            "function": {
                "name": "tool_a",
                "description": "Tool A description",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        }
        assert schemas[1]["function"]["name"] == "tool_b"

    def test_build_tool_schemas_empty_when_no_tools(self):
        runtime = PluginRuntime()
        schemas = runtime._build_tool_schemas()
        assert schemas == []
```

- [ ]**Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestBuildToolSchemas -v
```
Expected: FAIL — `AttributeError: 'PluginRuntime' object has no attribute '_build_tool_schemas'`

- [ ] **Step 3: 实现 _build_tool_schemas()**

在 `PluginRuntime` 类中添加方法（放在 `_collect_capabilities` 方法之后）：

```python
    def _build_tool_schemas(self) -> list[dict[str, Any]]:
        """Build OpenAI function-calling tool schemas from registered tools."""
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tool_instances.items():
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(tool, "description", ""),
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}}),
                },
            })
        return schemas
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestBuildToolSchemas -v
```
Expected: PASS (2 tests)

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add _build_tool_schemas() for OpenAI function-calling format"
```

---

### Task 3: _build_full_system_prompt() — 系统提示词 + 工具列表

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestBuildFullSystemPrompt:
    """Test _build_full_system_prompt() composes system prompt + tool list."""

    def test_build_full_system_prompt_includes_capabilities_and_tools(self):
        class MyPlugin(PluginRuntime):
            def register_capabilities(self):
                return {
                    "capabilities": [
                        {"type": "agent", "name": "test", "role": "tester"},
                    ],
                    "system_prompt": "你是测试专家。",
                }

        plugin = MyPlugin()
        # register a tool
        class FakeTool:
            name = "search"
            description = "搜索文档"
            parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

        plugin.register_tool(FakeTool())
        # Hack: call _collect_capabilities to populate system_prompt
        plugin._collect_capabilities()

        prompt = plugin._build_full_system_prompt()

        assert "你是测试专家" in prompt
        assert "search" in prompt
        assert "搜索文档" in prompt

    def test_build_full_system_prompt_no_tools(self):
        class MyPlugin(PluginRuntime):
            def register_capabilities(self):
                return {"capabilities": [], "system_prompt": "无工具。"}

        plugin = MyPlugin()
        plugin._collect_capabilities()

        prompt = plugin._build_full_system_prompt()
        assert prompt == "无工具。"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestBuildFullSystemPrompt -v
```
Expected: FAIL

- [ ] **Step 3: 实现 _build_full_system_prompt() 和 _system_prompt 存储**

首先，在 `_collect_capabilities` 方法中存储 system_prompt。找到方法末尾 `return caps, system_prompt`，在其前添加：

```python
        self._system_prompt = system_prompt
```

在 `PluginRuntime.__init__` 中初始化（`self._tool_instances` 之后）：

```python
        self._system_prompt: str = ""
```

然后在类中添加方法：

```python
    def _build_full_system_prompt(self) -> str:
        """Build complete system prompt with auto-generated tool list."""
        parts: list[str] = []
        if self._system_prompt:
            parts.append(self._system_prompt)

        schemas = self._build_tool_schemas()
        if schemas:
            parts.append("\n# 可用工具\n")
            for schema in schemas:
                func = schema["function"]
                parts.append(f"## {func['name']}\n{func['description']}")
                params = func.get("parameters", {})
                import json as _json
                parts.append(f"参数: {_json.dumps(params, ensure_ascii=False)}\n")

        return "\n".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestBuildFullSystemPrompt -v
```
Expected: PASS (2 tests)

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add _build_full_system_prompt() with auto-generated tool list"
```

---

### Task 4: _execute_tool() — 按名称执行注册的工具

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestExecuteTool:
    """Test _execute_tool() dispatches to registered tool instances."""

    @pytest.mark.asyncio
    async def test_execute_tool_dispatches_correctly(self):
        runtime = PluginRuntime()

        class FakeTool:
            name = "greet"
            description = "Greets"
            parameters = {}

            async def execute(self, **kwargs):
                from src.agent.tools.protocol import ToolResult
                return ToolResult(success=True, data={"greeting": f"Hello, {kwargs.get('name', 'world')}!"})

        runtime.register_tool(FakeTool())

        result = await runtime._execute_tool("greet", {"name": "Bob"})
        assert result == {"success": True, "data": {"greeting": "Hello, Bob!"}, "error": None}

    @pytest.mark.asyncio
    async def test_execute_tool_unknown_returns_error(self):
        runtime = PluginRuntime()
        result = await runtime._execute_tool("nonexistent", {})
        assert result == {"success": False, "error": "Unknown tool: nonexistent"}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestExecuteTool -v
```
Expected: FAIL — `AttributeError: 'PluginRuntime' object has no attribute '_execute_tool'`

- [ ] **Step 3: 实现 _execute_tool()**

在 `PluginRuntime` 类中添加方法：

```python
    async def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a registered tool by name and return result dict."""
        tool = self._tool_instances.get(tool_name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        try:
            result = await tool.execute(**args)
            return {
                "success": getattr(result, "success", True),
                "data": getattr(result, "data", None),
                "error": getattr(result, "error", None),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestExecuteTool -v
```
Expected: PASS (2 tests)

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add _execute_tool() for dispatching tool calls by name"
```

---

### Task 5: _collect_stream() — 消费 OpenAI 流式响应

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class MockStreamChunk:
    """Mock OpenAI stream chunk for testing _collect_stream."""
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.index = 0

    def __iter__(self):
        return iter([self])


class MockDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockToolCallDelta:
    def __init__(self, index=0, id=None, function_name=None, function_args=None):
        self.index = index
        self.id = id
        self.function = None
        if function_name or function_args:
            self.function = type("F", (), {"name": function_name, "arguments": function_args})()


class MockChoice:
    def __init__(self, delta):
        self.delta = delta


class MockChunk:
    def __init__(self, choice):
        self.choices = [choice]

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestCollectStream:
    """Test _collect_stream() consumes OpenAI streaming response."""

    @pytest.mark.asyncio
    async def test_collect_stream_yields_token_events_and_returns_text(self):
        runtime = PluginRuntime()

        # Build a mock stream
        chunks = [
            MockChunk(MockChoice(MockDelta(content="Hello"))),
            MockChunk(MockChoice(MockDelta(content=" world"))),
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        events: list[dict] = []
        # Call _collect_stream and collect yielded events
        gen = runtime._collect_stream(mock_stream(), "test_agent")
        collected_text = ""
        tool_calls_result = []
        async for item in gen:
            if isinstance(item, dict) and item.get("kind") == "token":
                events.append(item)
            elif isinstance(item, tuple):
                collected_text, tool_calls_result = item

        # The generator should yield token events during iteration
        assert len(events) >= 2
        assert events[0]["kind"] == "token"
        assert events[0]["name"] == "test_agent"
```

Wait — the spec has `_collect_stream` as a regular async function that yields token events AND returns a tuple. This pattern doesn't work well in Python (can't both yield and return easily). Let me restructure: `_collect_stream` should be an async generator that yields events, and the caller tracks accumulated text and tool_calls via a mutable container.

Let me redesign: `_collect_stream` yields events, and the caller collects the final text and tool_calls from the accumulated state.

Actually, let me make _collect_stream a regular async function that takes a callback:

```python
async def _collect_stream(self, stream, name: str, on_event) -> tuple[str, list[dict]]:
    """Consume OpenAI stream, call on_event for each token, return (text, tool_calls)."""
```

Or simpler: make it an async generator that yields ALL items (events + a final sentinel). On second thought, the cleanest approach for the caller (run_agent) is:

```python
async def run_agent(self, ...):
    ...
    text, tool_calls = await self._collect_stream(stream, name, _emit)
    ...

async def _collect_stream(self, stream, name: str, emit) -> tuple[str, list[dict]]:
    """Consume stream, call emit(event_dict) for each token. Returns (text, tool_calls)."""
    thinking_parts = []
    tool_calls_by_index = {}
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            thinking_parts.append(delta.content)
            emit(stream_event("token", name=name, text=delta.content))
        for tc in delta.tool_calls or []:
            idx = tc.index
            if idx not in tool_calls_by_index:
                tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
            entry = tool_calls_by_index[idx]
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["name"] += tc.function.name
                if tc.function.arguments:
                    entry["arguments"] += tc.function.arguments
    tool_calls = [
        {"id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]}
        for tc in tool_calls_by_index.values()
    ]
    return "".join(thinking_parts), tool_calls
```

This is much cleaner. Let me rewrite the plan with this approach.

Actually, the test for _collect_stream is quite complex because of all the mocking needed. Let me simplify: test it indirectly through run_agent(), or test with a very simple mock that only has content (no tool calls).

Let me rewrite Task 5 with a simpler test approach.
```

Let me rewrite Task 5 properly.

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestCollectStream:
    """Test _collect_stream()."""

    @pytest.mark.asyncio
    async def test_collect_stream_emits_tokens_and_returns_text(self):
        """_collect_stream should emit token events and return accumulated text."""
        runtime = PluginRuntime()

        # Build minimal mock stream with content-only chunks
        class Delta:
            def __init__(self, content=None):
                self.content = content
                self.tool_calls = None

        class Choice:
            def __init__(self, delta):
                self.delta = delta

        class Chunk:
            def __init__(self, content):
                self.choices = [Choice(Delta(content))]

        async def mock_stream():
            yield Chunk("Hello")
            yield Chunk(" world")

        events: list[dict] = []

        def emit(event):
            events.append(event)

        text, tool_calls = await runtime._collect_stream(mock_stream(), "agent1", emit)

        assert text == "Hello world"
        assert tool_calls == []
        assert len(events) == 2
        assert events[0] == {"kind": "token", "name": "agent1", "chunk": "Hello"}
        assert events[1]["chunk"] == " world"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestCollectStream -v
```
Expected: FAIL — no `_collect_stream` method

- [ ] **Step 3: 实现 _collect_stream()**

在 `PluginRuntime` 类中添加方法：

```python
    async def _collect_stream(
        self,
        stream: Any,
        name: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> tuple[str, list[dict[str, str]]]:
        """Consume OpenAI streaming response.

        Calls *emit* for each content delta as a stream_event("token") dict.
        Accumulates tool_calls from streaming deltas (OpenAI sends tool call
        fragments over multiple chunks — this reassembles them by index).

        Returns (accumulated_text, tool_calls_list).
        """
        from .runtime import stream_event as _se

        thinking_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                thinking_parts.append(delta.content)
                emit(_se("token", name=name, text=delta.content))
            for tc in delta.tool_calls or []:
                idx = tc.index
                if idx not in tool_calls_by_index:
                    tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                entry = tool_calls_by_index[idx]
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        entry["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["arguments"] += tc.function.arguments

        tool_calls = [
            {"id": e["id"], "name": e["name"], "arguments": e["arguments"]}
            for e in tool_calls_by_index.values()
        ]
        return "".join(thinking_parts), tool_calls
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestCollectStream -v
```
Expected: PASS

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add _collect_stream() for consuming OpenAI streaming responses"
```

---

### Task 6: run_agent() — 主 agent loop

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestRunAgent:
    """Test run_agent() — the built-in LLM agent loop."""

    @pytest.mark.asyncio
    async def test_run_agent_no_tool_calls(self):
        """When LLM returns plain text (no tool_calls), run_agent should yield
        start → tokens → conclusion → end."""
        runtime = PluginRuntime()
        runtime._system_prompt = "You are a helpful assistant."

        # Mock _collect_stream to simulate an LLM that returns plain text
        async def mock_collect(stream, name, emit):
            emit({"kind": "token", "name": name, "chunk": "Hello"})
            emit({"kind": "token", "name": name, "chunk": " world"})
            return "Hello world", []  # no tool calls

        runtime._collect_stream = mock_collect

        # Mock AsyncOpenAI
        class MockClient:
            def __init__(self, **kwargs):
                pass
            @property
            def chat(self):
                return self
            @property
            def completions(self):
                return self
            async def create(self, **kwargs):
                return type("Resp", (), {"choices": []})()

        import src.plugin.sdk.runtime as sdk_mod
        original_openai = getattr(sdk_mod, "AsyncOpenAI", None)
        sdk_mod.AsyncOpenAI = MockClient

        try:
            events = []
            async for event in runtime.run_agent(
                name="test_agent",
                task="say hello",
                model_config={"api_key": "sk-test", "base_url": "https://test", "model": "test"},
            ):
                events.append(event)
        finally:
            if original_openai:
                sdk_mod.AsyncOpenAI = original_openai

        kinds = [e.get("kind") for e in events]
        assert "start" in kinds
        assert "token" in kinds
        assert "conclusion" in kinds
        assert "end" in kinds

    @pytest.mark.asyncio
    async def test_run_agent_with_tool_call(self):
        """When LLM returns a tool_call, run_agent should yield think → tool_result."""
        runtime = PluginRuntime()
        runtime._system_prompt = "You are an agent with tools."

        call_count = [0]

        async def mock_collect(stream, name, emit):
            if call_count[0] == 0:
                call_count[0] += 1
                return "I need to search",
                       [{"id": "call_1", "name": "search", "arguments": '{"q":"test"}'}]
            else:
                emit({"kind": "token", "name": name, "chunk": "Results found."})
                return "Results found.", []

        runtime._collect_stream = mock_collect

        # Mock _execute_tool to return success
        async def mock_execute(name, args):
            return {"success": True, "data": {"results": ["item1"]}, "error": None}
        runtime._execute_tool = mock_execute

        # Mock AsyncOpenAI
        class MockClient:
            def __init__(self, **kwargs):
                pass
            @property
            def chat(self):
                return self
            @property
            def completions(self):
                return self
            async def create(self, **kwargs):
                return type("Resp", (), {"choices": []})()

        import src.plugin.sdk.runtime as sdk_mod
        original_openai = getattr(sdk_mod, "AsyncOpenAI", None)
        sdk_mod.AsyncOpenAI = MockClient

        try:
            events = []
            async for event in runtime.run_agent(
                name="agent",
                task="search something",
                model_config={"api_key": "sk", "base_url": "https://x", "model": "m"},
                max_turns=5,
            ):
                events.append(event)
        finally:
            if original_openai:
                sdk_mod.AsyncOpenAI = original_openai

        kinds = [e.get("kind") for e in events]
        assert "think" in kinds
        assert "tool_result" in kinds
```

Wait — this is getting very complex. The `stream_event` import in _collect_stream creates a circular import risk. Let me simplify.

Actually, looking at this more carefully, `_collect_stream` calls `stream_event()` which is defined in the same module. So there's no circular import — just a regular function call. But I had `from .runtime import stream_event as _se` which is wrong (it's importing from itself). The function `stream_event` is at module level in runtime.py, and `_collect_stream` is a method on `PluginRuntime` in the same module. So it can just call `stream_event(...)` directly.

Let me simplify the _collect_stream implementation:
```python
async def _collect_stream(self, stream, name, emit):
    thinking_parts = []
    tool_calls_by_index = {}
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            thinking_parts.append(delta.content)
            emit(stream_event("token", name=name, text=delta.content))
        for tc in delta.tool_calls or []:
            ...
    return "".join(thinking_parts), tool_calls
```

Now let me also reconsider the run_agent implementation. It needs to:
1. Build system prompt
2. Create AsyncOpenAI client
3. Initialize messages
4. Yield start event
5. Loop: call LLM → collect stream → if tool_calls: execute, yield events → if no tool_calls: yield conclusion, break
6. Yield end event

```python
async def run_agent(self, *, name, task, model_config, max_turns=10):
    from openai import AsyncOpenAI
    
    system_prompt = self._build_full_system_prompt()
    client = AsyncOpenAI(
        api_key=model_config["api_key"],
        base_url=model_config.get("base_url"),
    )
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    
    yield stream_event("start", name=name, task=task)
    
    final_text = ""
    for turn in range(max_turns):
        stream = await client.chat.completions.create(
            model=model_config["model"],
            messages=messages,
            tools=self._build_tool_schemas() or None,
            stream=True,
        )
        
        events_in_turn: list[dict] = []
        def emit(ev):
            events_in_turn.append(ev)
        
        text, tool_calls = await self._collect_stream(stream, name, emit)
        for ev in events_in_turn:
            yield ev
        
        if tool_calls:
            tool_names = ",".join(tc["name"] for tc in tool_calls)
            yield stream_event("think", name=name, detail=f"tool_calls:{tool_names}")
            
            # Add assistant message with tool_calls
            assistant_msg = {"role": "assistant", "content": text or ""}
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ]
            messages.append(assistant_msg)
            
            for tc in tool_calls:
                import json
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = await self._execute_tool(tc["name"], args)
                yield stream_event("tool_result", name=name,
                                   tool_name=tc["name"],
                                   tool_status="ok" if result.get("success") else "error",
                                   tool_summary=str(result.get("data", ""))[:200])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            final_text = text
            if text:
                yield stream_event("conclusion", name=name, text=text)
            break
    
    yield stream_event("end", name=name,
                       result={"status": "completed", "content": final_text})
```

This is quite a lot of code. Let me simplify the plan — the test for run_agent can be done by mocking _collect_stream and _execute_tool. This way we don't need to mock the entire OpenAI client.

Let me rewrite Task 6 with a cleaner approach.
```

OK, let me rewrite the entire plan file more concisely. The task descriptions are getting too verbose.<｜end▁of▁thinking｜>

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="Write">
<｜｜DSML｜｜parameter name="content" string="true"># 插件 Agent Loop 实现计划

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** 在 PluginRuntime 中内置 `run_agent()` 方法，使插件子进程运行完整 LLM agent loop。

**Architecture:** `run_agent()` 是 async generator，内部创建 AsyncOpenAI client、构建系统提示词（含工具列表）、运行 think→act→observe 循环，yield `stream_event()` dict。插件 handler 一行委托。宿主通过 `agent.run` params 传入 `model_config`。

**Spec:** `docs/superpowers/specs/2026-06-08-plugin-agent-loop-design.md`

**依赖（已完成）：** `stream_event()` helper、`JSONRPCStreamChunk` 扩展、`SubAgentStreamEvent`、`SSEAdapter.on_subagent_event()`

---

## 文件结构

| 文件 | 角色 | 操作 |
|------|------|------|
| `src/plugin/sdk/runtime.py` | `run_agent()` + 5 辅助方法 + `_tool_instances` | 修改 (~160行新增) |
| `src/plugin/proxies.py` | ProxyAgent.run() 转发 model_config | 修改 (~3行) |
| `src/agent/api/services/stream_service.py` | 传入 model_config | 修改 (~2行) |
| `plugins/*/entry.py` (5个) | agent.run handler 改用 `run_agent()` | 修改 (~10行/个) |

---

### Task 1: 基础设施 — register_tool 存储实例 + _execute_tool + _build_tool_schemas + _build_full_system_prompt

**Files:** `src/plugin/sdk/runtime.py`, `tests/plugin/test_runtime.py`

这三个辅助方法互相关联且都很简短，合并为一个 Task。

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestPluginAgentLoopHelpers:
    """Test helper methods for the built-in agent loop."""

    def test_register_tool_stores_instance(self):
        runtime = PluginRuntime()
        class T:
            name = "t1"; description = "d"; parameters = {}
            async def execute(self, **kw):
                from src.agent.tools.protocol import ToolResult
                return ToolResult(success=True, data=kw)
        runtime.register_tool(T())
        assert "t1" in runtime._tool_instances
        assert runtime._tool_instances["t1"].name == "t1"

    def test_build_tool_schemas(self):
        runtime = PluginRuntime()
        class T:
            name = "t1"; description = "desc"
            parameters = {"type": "object", "properties": {"x": {"type": "string"}}}
        runtime.register_tool(T())
        schemas = runtime._build_tool_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "t1"

    def test_build_tool_schemas_empty(self):
        assert PluginRuntime()._build_tool_schemas() == []

    def test_build_full_system_prompt(self):
        class P(PluginRuntime):
            def register_capabilities(self):
                return {"capabilities": [], "system_prompt": "你是专家。"}
        p = P()
        p._collect_capabilities()
        class T:
            name = "search"; description = "搜索"; parameters = {}
        p.register_tool(T())
        prompt = p._build_full_system_prompt()
        assert "你是专家" in prompt
        assert "search" in prompt
        assert "搜索" in prompt

    def test_build_full_system_prompt_no_tools(self):
        class P(PluginRuntime):
            def register_capabilities(self):
                return {"capabilities": [], "system_prompt": "无工具。"}
        p = P()
        p._collect_capabilities()
        assert p._build_full_system_prompt() == "无工具。"

    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        runtime = PluginRuntime()
        class T:
            name = "g"; description = ""; parameters = {}
            async def execute(self, **kw):
                from src.agent.tools.protocol import ToolResult
                return ToolResult(success=True, data={"result": kw.get("x")})
        runtime.register_tool(T())
        r = await runtime._execute_tool("g", {"x": 42})
        assert r == {"success": True, "data": {"result": 42}, "error": None}

    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self):
        r = await PluginRuntime()._execute_tool("nope", {})
        assert r == {"success": False, "error": "Unknown tool: nope"}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestPluginAgentLoopHelpers -v
```
Expected: FAIL

- [ ] **Step 3: 实现 4 个改动**

编辑 `src/plugin/sdk/runtime.py`：

**3a. 在 `__init__` 中（`self._pending_caps` 之后）添加：**

```python
        self._tool_instances: dict[str, Any] = {}
        self._system_prompt: str = ""
```

**3b. 在 `register_tool()` 末尾（`self._pending_caps.append(cap)` 之后）添加：**

```python
        self._tool_instances[tool_instance.name] = tool_instance
```

**3c. 在 `_collect_capabilities()` 中，`return caps, system_prompt` 之前添加：**

```python
        self._system_prompt = system_prompt
```

**3d. 在 `PluginRuntime` 类中添加 3 个方法：**

```python
    def _build_tool_schemas(self) -> list[dict[str, Any]]:
        """Build OpenAI function-calling tool schemas from registered tools."""
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tool_instances.items():
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(tool, "description", ""),
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}}),
                },
            })
        return schemas

    def _build_full_system_prompt(self) -> str:
        """Build complete system prompt with auto-generated tool list."""
        parts: list[str] = []
        if self._system_prompt:
            parts.append(self._system_prompt)
        schemas = self._build_tool_schemas()
        if schemas:
            parts.append("\n# 可用工具\n")
            for s in schemas:
                f = s["function"]
                parts.append(f"## {f['name']}\n{f['description']}")
                parts.append(f"参数: {json.dumps(f.get('parameters', {}), ensure_ascii=False)}\n")
        return "\n".join(parts)

    async def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a registered tool by name and return result dict."""
        tool = self._tool_instances.get(tool_name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        try:
            result = await tool.execute(**args)
            return {
                "success": getattr(result, "success", True),
                "data": getattr(result, "data", None),
                "error": getattr(result, "error", None),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestPluginAgentLoopHelpers -v
```
Expected: PASS (7 tests)

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add infrastructure methods for plugin agent loop (tool schemas, system prompt, execution)"
```

---

### Task 2: _collect_stream() — 消费 OpenAI 流式响应

**Files:** `src/plugin/sdk/runtime.py`, `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

```python
class TestCollectStream:
    @pytest.mark.asyncio
    async def test_collect_stream_content_only(self):
        runtime = PluginRuntime()
        class Delta:
            def __init__(self, c=None): self.content = c; self.tool_calls = None
        class Chunk:
            def __init__(self, c): self.choices = [type("C",(),{"delta":Delta(c)})()]
        async def stream():
            yield Chunk("Hello")
            yield Chunk(" world")

        events = []
        text, tcs = await runtime._collect_stream(stream(), "a1", events.append)
        assert text == "Hello world"
        assert tcs == []
        assert len(events) == 2
        assert events[0]["kind"] == "token"
        assert events[0]["chunk"] == "Hello"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestCollectStream -v
```
Expected: FAIL

- [ ] **Step 3: 实现**

```python
    async def _collect_stream(
        self,
        stream: Any,
        name: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> tuple[str, list[dict[str, str]]]:
        """Consume OpenAI streaming response.

        Calls *emit* for each content delta as a stream_event("token") dict.
        Returns (accumulated_text, tool_calls_list).
        """
        thinking_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                thinking_parts.append(delta.content)
                emit(stream_event("token", name=name, text=delta.content))
            for tc in delta.tool_calls or []:
                idx = tc.index
                if idx not in tool_calls_by_index:
                    tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                entry = tool_calls_by_index[idx]
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        entry["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["arguments"] += tc.function.arguments

        tool_calls = [
            {"id": e["id"], "name": e["name"], "arguments": e["arguments"]}
            for e in tool_calls_by_index.values()
        ]
        return "".join(thinking_parts), tool_calls
```

- [ ] **Step 4-6: 测试→提交**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestCollectStream -v  # PASS
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v  # ALL PASS
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add _collect_stream() for consuming OpenAI streaming responses"
```

---

### Task 3: run_agent() — 主 agent loop

**Files:** `src/plugin/sdk/runtime.py`, `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写测试**

```python
class TestRunAgent:
    @pytest.mark.asyncio
    async def test_run_agent_no_tool_calls(self):
        """Validate event sequence when LLM returns plain text."""
        runtime = PluginRuntime()
        runtime._system_prompt = "helper"

        async def fake_collect(stream, name, emit):
            emit(stream_event("token", name=name, text="hi"))
            return "hi", []

        runtime._collect_stream = fake_collect

        events = []
        async for ev in runtime.run_agent(
            name="a", task="hello",
            model_config={"api_key":"k","base_url":"u","model":"m"},
            _client_factory=lambda **kw: type("C",(),{"chat":type("X",(),{"completions":type("Y",(),
                {"create":lambda **kw:None})()})()})(),
        ):
            events.append(ev)

        kinds = [e.get("kind") for e in events]
        assert kinds == ["start", "token", "conclusion", "end"]

    @pytest.mark.asyncio
    async def test_run_agent_with_tool_call(self):
        """Validate think + tool_result events when LLM returns tool_call."""
        runtime = PluginRuntime()
        runtime._system_prompt = "agent"

        call_count = [0]
        async def fake_collect(stream, name, emit):
            if call_count[0] == 0:
                call_count[0] += 1
                return "", [{"id":"c1","name":"search","arguments":'{"q":"x"}'}]
            else:
                emit(stream_event("token", name=name, text="done"))
                return "done", []

        async def fake_execute(name, args):
            return {"success": True, "data": [1], "error": None}

        runtime._collect_stream = fake_collect
        runtime._execute_tool = fake_execute

        events = []
        async for ev in runtime.run_agent(
            name="a", task="search",
            model_config={"api_key":"k","base_url":"u","model":"m"},
            _client_factory=lambda **kw: type("C",(),{"chat":type("X",(),{"completions":type("Y",(),
                {"create":lambda **kw:None})()})()})(),
            max_turns=5,
        ):
            events.append(ev)

        kinds = [e.get("kind") for e in events]
        assert "think" in kinds
        assert "tool_result" in kinds
        assert "end" in kinds
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestRunAgent -v
```
Expected: FAIL

- [ ] **Step 3: 实现 run_agent()**

```python
    async def run_agent(
        self,
        *,
        name: str,
        task: str,
        model_config: dict[str, str],
        max_turns: int = 10,
        _client_factory: Callable[..., Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run an LLM agent loop with tool calling support.

        Yields stream_event()-compatible dicts. Plugin agent.run handlers
        delegate to this method::

            async for event in self.run_agent(name=..., task=..., model_config=...):
                yield event

        Parameters
        ----------
        _client_factory : callable, optional
            Test-only override for the OpenAI client constructor.
        """
        from openai import AsyncOpenAI

        system_prompt = self._build_full_system_prompt()

        factory = _client_factory or AsyncOpenAI
        client = factory(
            api_key=model_config["api_key"],
            base_url=model_config.get("base_url"),
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        yield stream_event("start", name=name, task=task)

        final_text = ""
        for _turn in range(max_turns):
            tool_schemas = self._build_tool_schemas()
            stream = await client.chat.completions.create(
                model=model_config["model"],
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                stream=True,
            )

            turn_events: list[dict[str, Any]] = []

            def _emit(ev: dict[str, Any]) -> None:
                turn_events.append(ev)

            text, tool_calls = await self._collect_stream(stream, name, _emit)
            for ev in turn_events:
                yield ev

            if tool_calls:
                tool_names = ",".join(tc["name"] for tc in tool_calls)
                yield stream_event("think", name=name, detail=f"tool_calls:{tool_names}")

                messages.append({
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ],
                })

                for tc in tool_calls:
                    try:
                        args = json.loads(tc["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    result = await self._execute_tool(tc["name"], args)
                    yield stream_event(
                        "tool_result", name=name,
                        tool_name=tc["name"],
                        tool_status="ok" if result.get("success") else "error",
                        tool_summary=str(result.get("data", ""))[:200],
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                final_text = text
                if text:
                    yield stream_event("conclusion", name=name, text=text)
                break

        yield stream_event("end", name=name,
                           result={"status": "completed", "content": final_text})
```

- [ ] **Step 4: 运行测试确认通过**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py::TestRunAgent -v
```
Expected: PASS (2 tests)

- [ ] **Step 5: 运行全部 runtime 测试确认无回归**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add run_agent() built-in LLM agent loop to PluginRuntime"
```

---

### Task 4: ProxyAgent 转发 model_config + stream_service 传入

**Files:** `src/plugin/proxies.py`, `src/agent/api/services/stream_service.py`

- [ ] **Step 1: 修改 ProxyAgent.run()**

编辑 `src/plugin/proxies.py`。在 `run()` 方法中，`self._client.stream("agent.run", {...})` 调用处添加 `model_config`。找到：

```python
        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
```

改为：

```python
        model_config = kwargs.get("model_config", {})
        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            "model_config": model_config,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
```

- [ ] **Step 2: 修改 stream_service**

编辑 `src/agent/api/services/stream_service.py`。在 `generate_sse_stream` 的 `agent.run()` 调用处添加 `model_config`。从 settings 中提取 LLM 配置：

```python
            model_config = {
                "api_key": getattr(settings, "openai_api_key", ""),
                "base_url": getattr(settings, "openai_base_url", ""),
                "model": model_name or getattr(settings, "openai_model", ""),
            }
            result = await agent.run(
                task=task,
                context=agent_context,
                on_step=adapter.on_step,
                on_token=adapter.on_token,
                on_content_token=adapter.on_content_token,
                on_tool_result=adapter.on_tool_result,
                on_subagent_event=adapter.on_subagent_event,
                model_config=model_config,
                context_manager=context_manager,
                state=prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
            )
```

在 `runner()` 闭包内，找到 agent.run 调用位置（约第 209 行），在其前插入 `model_config = {...}`，在调用参数中加 `model_config=model_config,`。

- [ ] **Step 3: 测试**

```bash
PYTHONPATH=. uv run pytest tests/plugin/test_proxies.py -v
PYTHONPATH=. uv run pytest tests/agent/test_subagent.py -v
```
Expected: ALL PASS

- [ ] **Step 4: 提交**

```bash
git add src/plugin/proxies.py src/agent/api/services/stream_service.py
git commit -m "feat: forward model_config from host to plugin agent.run"
```

---

### Task 5: 更新 5 个插件的 agent.run handler

**Files:** `plugins/format_audit/entry.py`, `plugins/content_audit/entry.py`, `plugins/style_audit/entry.py`, `plugins/plagiarism/entry.py`, `plugins/text_correction/entry.py`

- [ ] **Step 1: 修改每个插件**

每个文件当前是（以 format_audit 为例）：

```python
        @self.on("agent.run")
        async def handle_agent_run(params):
            yield "开始格式审计...\n"
            yield {"status": "completed", "content": params.get("task", "")}
```

改为：

```python
        @self.on("agent.run")
        async def handle_agent_run(params):
            async for event in self.run_agent(
                name=params["agent"],
                task=params["task"],
                model_config=params.get("model_config", {}),
            ):
                yield event
```

对所有 5 个插件做同样修改（替换 stub handler 内容）。

- [ ] **Step 2: 验证每个插件能正常注册**

```bash
# 检查每个插件语法正确
PYTHONPATH=. uv run python -c "import py_compile; py_compile.compile('plugins/format_audit/entry.py', doraise=True)"
PYTHONPATH=. uv run python -c "import py_compile; py_compile.compile('plugins/content_audit/entry.py', doraise=True)"
PYTHONPATH=. uv run python -c "import py_compile; py_compile.compile('plugins/style_audit/entry.py', doraise=True)"
PYTHONPATH=. uv run python -c "import py_compile; py_compile.compile('plugins/plagiarism/entry.py', doraise=True)"
PYTHONPATH=. uv run python -c "import py_compile; py_compile.compile('plugins/text_correction/entry.py', doraise=True)"
```
Expected: no errors

- [ ] **Step 3: 提交**

```bash
git add plugins/format_audit/entry.py plugins/content_audit/entry.py plugins/style_audit/entry.py plugins/plagiarism/entry.py plugins/text_correction/entry.py
git commit -m "feat: use run_agent() in all plugin agent.run handlers"
```

---

## 实现后检查清单

- [ ] `PYTHONPATH=. uv run pytest tests/plugin/test_runtime.py -v` — 全部 runtime 测试通过
- [ ] `PYTHONPATH=. uv run pytest tests/plugin/test_proxies.py -v` — 全部 proxy 测试通过
- [ ] `PYTHONPATH=. uv run pytest tests/agent/test_subagent.py -v` — 全部 subagent 测试通过
- [ ] 5 个插件 entry.py 语法检查通过
- [ ] `PYTHONPATH=. uv run ruff check src/plugin/sdk/runtime.py` — 无 lint 错误
