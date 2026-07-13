# 子代理流式传输 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使子代理（进程内和插件）的思考、工具调用、结论实时流式传输到前端。

**Architecture:** 定义统一的 `SubAgentStreamEvent` dataclass 贯穿所有层；插件路径通过扩展 `JSONRPCStreamChunk` 用 `kind` 字段区分事件类型，`ProxyAgent` 解析后回调 `on_subagent_event`；进程内路径由 `SubAgentRunner` 拦截标准回调转换；`SSEAdapter` 新增 `on_subagent_event` 映射为 `subagent_*` SSE 事件。

**Tech Stack:** Python 3.12+, Pydantic, asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-06-08-subagent-streaming-design.md`

---

## 文件结构

| 文件 | 角色 | 操作 |
|------|------|------|
| `src/agent/agents/subagent/events.py` | SubAgentStreamEvent dataclass | **新建** |
| `src/plugin/protocol.py` | JSONRPCStreamChunk 新字段 | 修改 |
| `src/plugin/sdk/runtime.py` | stream_event() helper + _send_chunk 扩展 | 修改 |
| `src/plugin/proxies.py` | ProxyAgent 解析 chunk → on_subagent_event | 修改 |
| `src/agent/agents/subagent/config.py` | _CallbackHolder 新增 on_subagent_event 槽位 | 修改 |
| `src/agent/agents/subagent/runner.py` | 进程内回调拦截 → SubAgentStreamEvent | 修改 |
| `src/agent/api/sse_adapter.py` | on_subagent_event → SSE 映射 | 修改 |
| `src/agent/api/services/stream_service.py` | 传递 on_subagent_event 接线 | 修改 |

---

### Task 1: 创建 SubAgentStreamEvent dataclass

**Files:**
- Create: `src/agent/agents/subagent/events.py`
- Create: `tests/agent/test_subagent_events.py`

- [ ] **Step 1: 编写测试**

```python
"""Tests for SubAgentStreamEvent."""
import pytest
from src.agent.agents.subagent.events import SubAgentStreamEvent


class TestSubAgentStreamEvent:
    def test_create_token_event(self):
        event = SubAgentStreamEvent(
            kind="token", subagent_name="parser", text="分析中..."
        )
        assert event.kind == "token"
        assert event.subagent_name == "parser"
        assert event.text == "分析中..."
        assert event.detail is None
        assert event.tool_name is None

    def test_create_think_event(self):
        event = SubAgentStreamEvent(
            kind="think", subagent_name="parser",
            detail="tool_calls:parse_document"
        )
        assert event.kind == "think"
        assert event.detail == "tool_calls:parse_document"

    def test_create_tool_result_event(self):
        event = SubAgentStreamEvent(
            kind="tool_result", subagent_name="parser",
            tool_name="parse_document", tool_status="ok",
            tool_duration=1.5, tool_summary="解析完成"
        )
        assert event.kind == "tool_result"
        assert event.tool_name == "parse_document"
        assert event.tool_status == "ok"
        assert event.tool_duration == 1.5
        assert event.tool_summary == "解析完成"

    def test_create_start_event(self):
        event = SubAgentStreamEvent(
            kind="start", subagent_name="parser", task="解析文档"
        )
        assert event.kind == "start"
        assert event.task == "解析文档"

    def test_create_end_event(self):
        event = SubAgentStreamEvent(
            kind="end", subagent_name="parser",
            result={"status": "completed", "content": "done"}
        )
        assert event.kind == "end"
        assert event.result == {"status": "completed", "content": "done"}

    def test_create_conclusion_event(self):
        event = SubAgentStreamEvent(
            kind="conclusion", subagent_name="parser", text="解析完成。"
        )
        assert event.kind == "conclusion"
        assert event.text == "解析完成。"

    def test_event_is_frozen(self):
        event = SubAgentStreamEvent(kind="token", subagent_name="p")
        with pytest.raises(Exception):
            event.kind = "think"

    def test_defaults_are_none(self):
        event = SubAgentStreamEvent(kind="token", subagent_name="p")
        assert event.text is None
        assert event.detail is None
        assert event.tool_name is None
        assert event.tool_status is None
        assert event.tool_duration is None
        assert event.tool_summary is None
        assert event.task is None
        assert event.result is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/agent/test_subagent_events.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.agents.subagent.events'`

- [ ] **Step 3: 实现 SubAgentStreamEvent**

创建 `src/agent/agents/subagent/events.py`：

```python
"""SubAgentStreamEvent — unified stream event for sub-agent activity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class SubAgentStreamEvent:
    """贯穿所有层的统一子代理事件类型。

    kind 区分 6 种事件：
    - start: 子代理启动（携带 task）
    - token: 思考令牌，流式传输
    - think: 工具调用宣告
    - tool_result: 工具执行结果
    - conclusion: 结论令牌，流式传输
    - end: 子代理结束（携带最终结果）
    """
    kind: Literal["token", "think", "tool_result", "conclusion", "start", "end"]
    subagent_name: str
    text: str | None = None          # kind=token, conclusion
    detail: str | None = None        # kind=think (e.g. "tool_calls:parse")
    tool_name: str | None = None     # kind=tool_result
    tool_status: str | None = None   # "ok" | "error"
    tool_duration: float | None = None
    tool_summary: str | None = None
    task: str | None = None          # kind=start
    result: Any = None               # kind=end (AgentResult dict)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/agent/test_subagent_events.py -v
```
Expected: PASS (8 tests)

- [ ] **Step 5: 提交**

```bash
git add src/agent/agents/subagent/events.py tests/agent/test_subagent_events.py
git commit -m "feat: add SubAgentStreamEvent dataclass for sub-agent streaming"
```

---

### Task 2: 扩展 JSONRPCStreamChunk 协议

**Files:**
- Modify: `src/plugin/protocol.py`
- Modify: `tests/plugin/test_protocol.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_protocol.py` 末尾追加：

```python
class TestJSONRPCStreamChunkExtended:
    """Test new sub-agent streaming fields on JSONRPCStreamChunk."""

    def test_legacy_chunk_without_new_fields_still_valid(self):
        chunk = JSONRPCStreamChunk(id=1, chunk="hello", status="continue")
        assert chunk.id == 1
        assert chunk.chunk == "hello"
        assert chunk.status == "continue"
        assert chunk.kind is None
        assert chunk.subagent_name is None

    def test_new_fields_default_to_none(self):
        chunk = JSONRPCStreamChunk(id=1, status="continue")
        assert chunk.kind is None
        assert chunk.subagent_name is None
        assert chunk.tool_name is None
        assert chunk.tool_status is None
        assert chunk.tool_duration is None
        assert chunk.tool_summary is None
        assert chunk.task is None

    def test_token_chunk_with_kind(self):
        chunk = JSONRPCStreamChunk(
            id=1, kind="token", subagent_name="parser",
            chunk="分析中...", status="continue"
        )
        assert chunk.kind == "token"
        assert chunk.subagent_name == "parser"

    def test_tool_result_chunk_full(self):
        chunk = JSONRPCStreamChunk(
            id=1, kind="tool_result", subagent_name="parser",
            tool_name="parse", tool_status="ok",
            tool_duration=1.5, tool_summary="done",
            status="continue"
        )
        assert chunk.tool_name == "parse"
        assert chunk.tool_status == "ok"
        assert chunk.tool_duration == 1.5
        assert chunk.tool_summary == "done"

    def test_serialization_roundtrip(self):
        chunk = JSONRPCStreamChunk(
            id=1, kind="think", subagent_name="p",
            chunk="tool_calls:x", status="continue"
        )
        json_str = chunk.model_dump_json()
        restored = JSONRPCStreamChunk.model_validate_json(json_str)
        assert restored.kind == "think"
        assert restored.subagent_name == "p"

    def test_legacy_json_excludes_none_new_fields(self):
        chunk = JSONRPCStreamChunk(id=1, chunk="text", status="continue")
        json_str = chunk.model_dump_json()
        assert '"kind"' not in json_str
        assert '"subagent_name"' not in json_str
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/plugin/test_protocol.py::TestJSONRPCStreamChunkExtended -v
```
Expected: FAIL — `kind` is not a valid field

- [ ] **Step 3: 扩展 JSONRPCStreamChunk**

编辑 `src/plugin/protocol.py` 的 `JSONRPCStreamChunk` 类。找到：

```python
class JSONRPCStreamChunk(BaseModel):
    """A streaming response chunk for long-running operations."""

    id: int
    chunk: str | None = None
    result: Any = None
    status: Literal["continue", "end"]

    @property
    def is_end(self) -> bool:
        return self.status == "end"
```

改为：

```python
class JSONRPCStreamChunk(BaseModel):
    """A streaming response chunk for long-running operations."""

    id: int
    chunk: str | None = None
    result: Any = None
    status: Literal["continue", "end"]

    # Sub-agent streaming event fields (all optional, default None for backward compat)
    kind: Literal["token", "think", "tool_result", "conclusion", "start", "end"] | None = None
    subagent_name: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_duration: float | None = None
    tool_summary: str | None = None
    task: str | None = None

    @property
    def is_end(self) -> bool:
        return self.status == "end"
```

- [ ] **Step 4: 运行新测试确认通过**

```bash
uv run pytest tests/plugin/test_protocol.py::TestJSONRPCStreamChunkExtended -v
```
Expected: PASS (6 tests)

- [ ] **Step 5: 运行全部 protocol 测试和完整测试套件确认无回归**

```bash
uv run pytest tests/plugin/test_protocol.py -v
uv run pytest tests/ -x -q
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/protocol.py tests/plugin/test_protocol.py
git commit -m "feat: extend JSONRPCStreamChunk with sub-agent streaming fields"
```

---

### Task 3: Plugin SDK — stream_event() helper 和 _send_chunk 扩展

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `tests/plugin/test_runtime.py`

- [ ] **Step 1: 编写 stream_event() 单元测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestStreamEventHelper:
    """Test the stream_event() helper function."""

    def test_stream_event_token(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("token", name="p", text="hello")
        assert result["kind"] == "token"
        assert result["name"] == "p"
        assert result["chunk"] == "hello"
        assert "tool_name" not in result

    def test_stream_event_think(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("think", name="p", detail="tool_calls:x")
        assert result["kind"] == "think"
        assert result["detail"] == "tool_calls:x"

    def test_stream_event_tool_result(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("tool_result", name="p",
                              tool_name="parse", tool_status="ok",
                              tool_duration=1.5, tool_summary="done")
        assert result["kind"] == "tool_result"
        assert result["tool_name"] == "parse"
        assert result["tool_status"] == "ok"
        assert result["tool_duration"] == 1.5
        assert result["tool_summary"] == "done"

    def test_stream_event_start(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("start", name="p", task="parse task")
        assert result["kind"] == "start"
        assert result["task"] == "parse task"

    def test_stream_event_end(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("end", name="p",
                              result={"status": "completed"})
        assert result["kind"] == "end"
        assert result["result"] == {"status": "completed"}

    def test_stream_event_conclusion(self):
        from src.plugin.sdk.runtime import stream_event
        result = stream_event("conclusion", name="p", text="done")
        assert result["kind"] == "conclusion"
        assert result["chunk"] == "done"
```

- [ ] **Step 2: 编写 _send_chunk 结构化事件集成测试**

在 `tests/plugin/test_runtime.py` 末尾追加：

```python
class TestSendChunkWithStreaming:
    """Test _send_chunk with new sub-agent streaming fields."""

    @pytest.mark.asyncio
    async def test_agent_run_yields_structured_events_on_stdout(self):
        """插件 agent.run handler yield 结构化 dict 时应产生正确 JSONL。"""
        runtime = PluginRuntime()

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        class TestWriter:
            def write(self, data: str):
                output_lines.append(data)
            def flush(self):
                pass

        runtime._reader = input_queue
        runtime._writer = TestWriter()

        @runtime.on("agent.run")
        async def handle_agent_run(params):
            yield {"kind": "start", "name": "parser", "task": params.get("task", "")}
            yield {"kind": "token", "name": "parser", "chunk": "thinking..."}
            yield {"kind": "end", "name": "parser", "result": {"status": "completed"}}

        input_queue.put_nowait(json.dumps({
            "id": 1, "method": "agent.run",
            "params": {"agent": "parser", "task": "test"}
        }))
        input_queue.put_nowait("")

        await runtime.run()

        # 过滤出 id=1 的响应行
        chunks = [
            json.loads(l) for l in output_lines
            if '"id": 1' in l and '"method"' not in l
        ]
        assert len(chunks) >= 3

        # chunk 0: start (kind spread to top level, chunk contains detail)
        assert chunks[0]["kind"] == "start"
        assert chunks[0]["subagent_name"] == "parser"
        assert chunks[0]["task"] == "test"
        assert chunks[0]["status"] == "continue"

        # chunk 1: token
        assert chunks[1]["kind"] == "token"
        assert chunks[1]["chunk"] == "thinking..."
        assert chunks[1]["status"] == "continue"

        # chunk 2: end
        assert chunks[2]["kind"] == "end"
        assert chunks[2]["status"] == "end"
        assert chunks[2]["result"]["status"] == "completed"
```

- [ ] **Step 3: 运行测试确认失败**

```bash
uv run pytest tests/plugin/test_runtime.py::TestStreamEventHelper -v
```
Expected: FAIL — `stream_event` not defined

- [ ] **Step 4: 实现 stream_event() helper**

在 `src/plugin/sdk/runtime.py` 末尾（`_send_line` 方法之后，模块顶层）添加：

```python
def stream_event(
    kind: str,
    *,
    name: str = "",
    text: str | None = None,
    detail: str | None = None,
    tool_name: str | None = None,
    tool_status: str | None = None,
    tool_duration: float | None = None,
    tool_summary: str | None = None,
    task: str | None = None,
    result: Any = None,
) -> dict[str, Any]:
    """Build a structured dict for yield in agent.run async generator handlers.

    The returned dict is yielded by the handler. PluginRuntime._send_chunk()
    maps its keys to JSONRPCStreamChunk fields for transmission to the host.
    """
    payload: dict[str, Any] = {"kind": kind, "name": name}
    if text is not None:
        payload["chunk"] = text
    if detail is not None:
        payload["detail"] = detail
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if tool_status is not None:
        payload["tool_status"] = tool_status
    if tool_duration is not None:
        payload["tool_duration"] = tool_duration
    if tool_summary is not None:
        payload["tool_summary"] = tool_summary
    if task is not None:
        payload["task"] = task
    if result is not None:
        payload["result"] = result
    return payload
```

- [ ] **Step 5: 扩展 _send_chunk**

编辑 `src/plugin/sdk/runtime.py` 的 `_send_chunk` 方法。找到：

```python
    def _send_chunk(
        self,
        req_id: int,
        chunk: str | dict | None = None,
        *,
        status: str = "continue",
        result: Any = None,
    ) -> None:
        """Send a streaming response chunk."""
        payload: dict[str, Any] = {"id": req_id, "status": status}
        if chunk is not None:
            if isinstance(chunk, str):
                payload["chunk"] = chunk
            else:
                payload["chunk"] = json.dumps(chunk, ensure_ascii=False)
        if result is not None:
            payload["result"] = result
        self._send_line(json.dumps(payload, ensure_ascii=False))
```

改为：

```python
    def _send_chunk(
        self,
        req_id: int,
        chunk: str | dict | None = None,
        *,
        status: str = "continue",
        result: Any = None,
    ) -> None:
        """Send a streaming response chunk.

        When *chunk* is a dict with a ``kind`` key, its fields are spread
        into the top-level JSONRPCStreamChunk payload so the host routes by
        kind directly.  Plain str or dict without kind is handled as before.
        """
        payload: dict[str, Any] = {"id": req_id, "status": status}
        if isinstance(chunk, dict) and "kind" in chunk:
            data = chunk
            if "chunk" in data:
                payload["chunk"] = data["chunk"]
            if "detail" in data:
                payload["chunk"] = data["detail"]
            payload["kind"] = data.get("kind")
            if "name" in data:
                payload["subagent_name"] = data["name"]
            if "tool_name" in data:
                payload["tool_name"] = data["tool_name"]
            if "tool_status" in data:
                payload["tool_status"] = data["tool_status"]
            if "tool_duration" in data:
                payload["tool_duration"] = data["tool_duration"]
            if "tool_summary" in data:
                payload["tool_summary"] = data["tool_summary"]
            if "task" in data:
                payload["task"] = data["task"]
        elif chunk is not None:
            if isinstance(chunk, str):
                payload["chunk"] = chunk
            else:
                payload["chunk"] = json.dumps(chunk, ensure_ascii=False)
        if result is not None:
            payload["result"] = result
        self._send_line(json.dumps(payload, ensure_ascii=False))
```

- [ ] **Step 6: 运行新测试确认通过**

```bash
uv run pytest tests/plugin/test_runtime.py::TestStreamEventHelper -v
uv run pytest tests/plugin/test_runtime.py::TestSendChunkWithStreaming -v
```
Expected: ALL PASS

- [ ] **Step 7: 运行全部 runtime 测试确认无回归**

```bash
uv run pytest tests/plugin/test_runtime.py -v
```
Expected: ALL PASS

- [ ] **Step 8: 提交**

```bash
git add src/plugin/sdk/runtime.py tests/plugin/test_runtime.py
git commit -m "feat: add stream_event() helper and extend _send_chunk for structured sub-agent streaming"
```

---

### Task 4: ProxyAgent 解析结构化 chunk 并调用 on_subagent_event

**Files:**
- Modify: `src/plugin/proxies.py`
- Modify: `tests/plugin/test_proxies.py`

- [ ] **Step 1: 编写测试**

在 `tests/plugin/test_proxies.py` 的 `TestProxyAgent` 类中添加以下两个测试方法。

第一个测试（结构化 chunk → on_subagent_event）：

```python
    @pytest.mark.asyncio
    async def test_run_with_structured_chunks_calls_on_subagent_event(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        agent_spec = {"name": "my_agent", "role": "auditor"}
        received_events: list[SubAgentStreamEvent] = []
        mock_client = MockClient(stream_chunks=[
            JSONRPCStreamChunk(
                id=1, kind="start", subagent_name="my_agent",
                task="audit doc", status="continue"
            ),
            JSONRPCStreamChunk(
                id=1, kind="token", subagent_name="my_agent",
                chunk="thinking...", status="continue"
            ),
            JSONRPCStreamChunk(
                id=1, kind="think", subagent_name="my_agent",
                chunk="tool_calls:search", status="continue"
            ),
            JSONRPCStreamChunk(
                id=1, kind="tool_result", subagent_name="my_agent",
                tool_name="search", tool_status="ok",
                tool_duration=0.5, tool_summary="found",
                status="continue"
            ),
            JSONRPCStreamChunk(
                id=1, kind="conclusion", subagent_name="my_agent",
                chunk="all good", status="continue"
            ),
            JSONRPCStreamChunk(
                id=1, kind="end", subagent_name="my_agent",
                status="end",
                result={"status": "completed", "content": "all good"}
            ),
        ])

        async def collect(event):
            received_events.append(event)

        proxy = ProxyAgent(mock_client, agent_spec, on_subagent_event=collect)
        result = await proxy.run(task="audit doc")

        assert result.status == "completed"
        assert len(received_events) == 6

        assert received_events[0].kind == "start"
        assert received_events[0].task == "audit doc"
        assert received_events[1].kind == "token"
        assert received_events[1].text == "thinking..."
        assert received_events[2].kind == "think"
        assert received_events[2].detail == "tool_calls:search"
        assert received_events[3].kind == "tool_result"
        assert received_events[3].tool_name == "search"
        assert received_events[4].kind == "conclusion"
        assert received_events[4].text == "all good"
        assert received_events[5].kind == "end"
```

第二个测试（旧格式 chunk 无回调 → 行为不变）：

```python
    @pytest.mark.asyncio
    async def test_run_with_legacy_chunks_no_callback_still_works(self):
        agent_spec = {"name": "my_agent", "role": "auditor"}
        mock_client = MockClient(stream_chunks=[
            JSONRPCStreamChunk(id=1, chunk="分析中...", status="continue"),
            JSONRPCStreamChunk(id=1, chunk="完成", status="continue"),
            JSONRPCStreamChunk(
                id=1, status="end",
                result={"status": "completed", "content": "文档合规"},
            ),
        ])
        proxy = ProxyAgent(mock_client, agent_spec)
        result = await proxy.run(task="审计文档")

        assert result.status == "completed"
        assert result.content == "文档合规"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent::test_run_with_structured_chunks_calls_on_subagent_event -v
```
Expected: FAIL — `ProxyAgent.__init__() got an unexpected keyword argument 'on_subagent_event'`

- [ ] **Step 3: 修改 ProxyAgent**

编辑 `src/plugin/proxies.py`。

先在文件头部添加 import：

```python
from collections.abc import Callable, Awaitable
```

修改 `__init__` 方法签名（`proxy.py` 第 146 行附近）。将：

```python
    def __init__(self, client: _JSONRPCClientLike, agent_spec: dict[str, Any]) -> None:
        self._client = client
        self.name: str = agent_spec["name"]
        self.display_name: str | None = agent_spec.get("display_name")
        self.role: str = agent_spec.get("role", "")
        self._agent_name = agent_spec["name"]
```

改为：

```python
    def __init__(
        self,
        client: _JSONRPCClientLike,
        agent_spec: dict[str, Any],
        on_subagent_event: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self.name: str = agent_spec["name"]
        self.display_name: str | None = agent_spec.get("display_name")
        self.role: str = agent_spec.get("role", "")
        self._agent_name = agent_spec["name"]
        self._on_subagent_event = on_subagent_event
```

修改 `run` 方法（`proxies.py` 第 158 行附近）。将：

```python
    async def run(self, task: str | None = None, **kwargs: Any) -> AgentResult:
        """Forward run() to the plugin subprocess via streaming JSON-RPC.

        Chunks from ``agent.run`` are collected and assembled into an
        AgentResult.  The final chunk's ``result`` payload is expected to
        contain ``status`` and ``content`` fields.
        """
        content_parts: list[str] = []
        final_result: dict[str, Any] = {}
        total_size = 0

        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
            if chunk.chunk is not None:
                total_size += len(chunk.chunk.encode("utf-8"))
                if total_size > _MAX_RESPONSE_SIZE:
                    raise PluginRPCError(
                        -1,
                        f"Agent '{self._agent_name}' response exceeds max size ({_MAX_RESPONSE_SIZE} bytes)",
                    )
                content_parts.append(chunk.chunk)
            if chunk.is_end and chunk.result is not None:
                final_result = chunk.result if isinstance(chunk.result, dict) else {}

        status = final_result.get("status", "completed")
        content = final_result.get("content") or ("".join(content_parts) or None)

        return AgentResult(
            status=status,
            content=content,
            data=final_result.get("data", {}),
            termination_reason=final_result.get("termination_reason"),
        )
```

改为：

```python
    async def run(self, task: str | None = None, **kwargs: Any) -> AgentResult:
        """Forward run() to the plugin subprocess via streaming JSON-RPC.

        When *self._on_subagent_event* is set, each structured chunk (kind
        field present) is converted to a SubAgentStreamEvent and dispatched
        through that callback.  Legacy chunks (no kind) are accumulated as
        before.
        """
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        content_parts: list[str] = []
        final_result: dict[str, Any] = {}
        total_size = 0

        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
            # Dispatch structured sub-agent event if callback is set
            if chunk.kind is not None and self._on_subagent_event is not None:
                await self._on_subagent_event(SubAgentStreamEvent(
                    kind=chunk.kind,
                    subagent_name=chunk.subagent_name or self._agent_name,
                    text=chunk.chunk,
                    detail=chunk.chunk if chunk.kind == "think" else None,
                    tool_name=chunk.tool_name,
                    tool_status=chunk.tool_status,
                    tool_duration=chunk.tool_duration,
                    tool_summary=chunk.tool_summary,
                    task=chunk.task,
                    result=chunk.result if chunk.is_end else None,
                ))

            if chunk.chunk is not None:
                total_size += len(chunk.chunk.encode("utf-8"))
                if total_size > _MAX_RESPONSE_SIZE:
                    raise PluginRPCError(
                        -1,
                        f"Agent '{self._agent_name}' response exceeds max size ({_MAX_RESPONSE_SIZE} bytes)",
                    )
                content_parts.append(chunk.chunk)
            if chunk.is_end and chunk.result is not None:
                final_result = chunk.result if isinstance(chunk.result, dict) else {}

        status = final_result.get("status", "completed")
        content = final_result.get("content") or ("".join(content_parts) or None)

        return AgentResult(
            status=status,
            content=content,
            data=final_result.get("data", {}),
            termination_reason=final_result.get("termination_reason"),
        )
```

- [ ] **Step 4: 运行新测试确认通过**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent -v
```
Expected: ALL 6 tests PASS

- [ ] **Step 5: 运行全部 proxies 测试确认无回归**

```bash
uv run pytest tests/plugin/test_proxies.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/plugin/proxies.py tests/plugin/test_proxies.py
git commit -m "feat: ProxyAgent dispatches SubAgentStreamEvent via on_subagent_event callback"
```

---

### Task 5: _CallbackHolder 新增 on_subagent_event 槽位

**Files:**
- Modify: `src/agent/agents/subagent/config.py`
- Modify: `src/agent/agents/subagent/runner.py`

- [ ] **Step 1: 修改 _CallbackHolder**

编辑 `src/agent/agents/subagent/config.py`。找到 `_CallbackHolder.__init__`（第 75 行附近）：

```python
    def __init__(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None,
    ) -> None:
        self._on_step = on_step
        self._on_token = on_token
        self._on_content_token = on_content_token
        self._on_tool_result = on_tool_result
```

改为：

```python
    def __init__(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None,
        on_subagent_event: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._on_step = on_step
        self._on_token = on_token
        self._on_content_token = on_content_token
        self._on_tool_result = on_tool_result
        self._on_subagent_event = on_subagent_event

    @property
    def on_subagent_event(self) -> Callable[..., Awaitable[None]] | None:
        return self._on_subagent_event
```

- [ ] **Step 2: 修改 SubAgentRunner.set_callbacks**

编辑 `src/agent/agents/subagent/runner.py`。找到 `set_callbacks` 方法（第 91 行附近）：

```python
    def set_callbacks(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None,
    ) -> None:
        self._callback_holder = _CallbackHolder(
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
        )
```

改为：

```python
    def set_callbacks(
        self,
        on_step: Callable[[str, str], Awaitable[None]] | None,
        on_token: Callable[[str], Awaitable[None]] | None,
        on_content_token: Callable[[str], Awaitable[None]] | None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None,
        on_subagent_event: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._callback_holder = _CallbackHolder(
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
            on_subagent_event=on_subagent_event,
        )
```

- [ ] **Step 3: 运行现有测试确认无回归**

```bash
uv run pytest tests/agent/test_subagent.py -v
uv run pytest tests/ -x -q
```
Expected: ALL PASS

- [ ] **Step 4: 提交**

```bash
git add src/agent/agents/subagent/config.py src/agent/agents/subagent/runner.py
git commit -m "feat: add on_subagent_event slot to _CallbackHolder and SubAgentRunner.set_callbacks"
```

---

### Task 6: SubAgentRunner 拦截进程内回调 → SubAgentStreamEvent

**Files:**
- Modify: `src/agent/agents/subagent/runner.py`
- Modify: `tests/agent/test_subagent.py`

- [ ] **Step 1: 编写测试**

在 `tests/agent/test_subagent.py` 末尾追加：

```python
class TestSubAgentRunnerStreaming:
    """Test SubAgentRunner intercepts callbacks and emits SubAgentStreamEvent."""

    @pytest.mark.asyncio
    async def test_dispatch_emits_subagent_events_for_in_process_agent(self):
        from src.agent.agents.subagent.runner import SubAgentRunner
        from src.agent.agents.subagent.config import SubAgentConfig, FailureStrategy
        from src.agent.agents.subagent.events import SubAgentStreamEvent
        from src.agent.agents.base import AgentResult
        from src.agent.tools.protocol import ToolResult

        received: list[SubAgentStreamEvent] = []

        async def on_event(event):
            received.append(event)

        class MockAgent:
            name = "test_sub"
            display_name = "Test Sub"
            role = "tester"

            async def run(self, task=None, **kwargs):
                on_step = kwargs.get("on_step")
                on_token = kwargs.get("on_token")
                on_tool_result = kwargs.get("on_tool_result")
                on_content_token = kwargs.get("on_content_token")

                if on_step:
                    await on_step("think", "tool_calls:echo")
                if on_token:
                    await on_token("hello")
                if on_step:
                    await on_step("act", "")
                if on_tool_result:
                    tr = ToolResult(success=True, data={"result": "ok"})
                    await on_tool_result("echo", tr, "Echo done")
                if on_step:
                    await on_step("observe", "")
                if on_content_token:
                    await on_content_token("done")

                return AgentResult(status="completed", content="done")

        agent_config = SubAgentConfig(
            agent=MockAgent(),
            failure_strategy=FailureStrategy.TOLERANT,
            max_retries=0,
            description="test",
        )

        runner = SubAgentRunner()
        runner.define("test_sub", agent_config)
        runner.set_callbacks(
            on_step=None, on_token=None,
            on_content_token=None, on_tool_result=None,
            on_subagent_event=on_event,
        )

        result = await runner.dispatch("test_sub", task="task")
        assert result.success is True
        assert len(received) >= 3

        kinds = [e.kind for e in received]
        assert "token" in kinds
        assert "think" in kinds
        assert "tool_result" in kinds
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentRunnerStreaming -v
```
Expected: FAIL — received events list is empty（on_subagent_event 未被调用）

- [ ] **Step 3: 修改 SubAgentRunner.dispatch 和 dispatch_structured 方法**

编辑 `src/agent/agents/subagent/runner.py`。

先添加 import（在文件顶部）：

```python
from .events import SubAgentStreamEvent
```

在 `SubAgentRunner` 类中添加一个辅助方法，创建包装回调（同时调用原始回调和 on_subagent_event）：

```python
    def _wrap_for_subagent_stream(
        self,
        name: str,
        on_step: Callable | None,
        on_token: Callable | None,
        on_tool_result: Callable | None,
        on_content_token: Callable | None,
    ) -> dict[str, Any]:
        """Wrap standard callbacks to also emit SubAgentStreamEvent.

        When the callback holder has on_subagent_event set, standard callbacks
        are wrapped so that each callback invocation also produces a
        SubAgentStreamEvent.  The original callback (if set) is still called
        after the event is emitted.
        """
        cb = self._callback_holder
        if cb is None or cb.on_subagent_event is None:
            return {
                "on_step": on_step,
                "on_token": on_token,
                "on_tool_result": on_tool_result,
                "on_content_token": on_content_token,
            }

        async def _on_step(event: str, detail: str) -> None:
            if event == "think":
                await cb.on_subagent_event(SubAgentStreamEvent(
                    kind="think", subagent_name=name, detail=detail,
                ))
            if on_step is not None:
                await on_step(event, detail)

        async def _on_token(token: str) -> None:
            await cb.on_subagent_event(SubAgentStreamEvent(
                kind="token", subagent_name=name, text=token,
            ))
            if on_token is not None:
                await on_token(token)

        async def _on_tool_result(
            tool_name: str, result: Any, summary: str
        ) -> None:
            success = getattr(result, "success", True)
            await cb.on_subagent_event(SubAgentStreamEvent(
                kind="tool_result", subagent_name=name,
                tool_name=tool_name,
                tool_status="ok" if success else "error",
                tool_summary=summary,
            ))
            if on_tool_result is not None:
                await on_tool_result(tool_name, result, summary)

        async def _on_content_token(token: str) -> None:
            await cb.on_subagent_event(SubAgentStreamEvent(
                kind="conclusion", subagent_name=name, text=token,
            ))
            if on_content_token is not None:
                await on_content_token(token)

        return {
            "on_step": _on_step,
            "on_token": _on_token,
            "on_tool_result": _on_tool_result,
            "on_content_token": _on_content_token,
        }
```

修改 `dispatch` 方法中的 `_run` 闭包。将回调创建部分从：

```python
        def _run():
            scoped_store = self._build_scoped_store(artifact_store)
            run_coro = agent.run(
                task=task,
                context=context,
                context_manager=context_manager,
                on_step=cb.make_on_step(name) if cb else None,
                on_token=cb.make_on_token(name) if cb else None,
                on_content_token=cb.make_on_content_token(name) if cb else None,
                on_tool_result=cb.make_on_tool_result(name) if cb else None,
                audit_logger=audit_logger,
                artifact_store=scoped_store,
            )
```

改为：

```python
        def _run():
            scoped_store = self._build_scoped_store(artifact_store)
            base_on_step = cb.make_on_step(name) if cb else None
            base_on_token = cb.make_on_token(name) if cb else None
            base_on_content_token = cb.make_on_content_token(name) if cb else None
            base_on_tool_result = cb.make_on_tool_result(name) if cb else None

            # Emit SubAgentStreamEvent start
            if cb and cb.on_subagent_event:
                import asyncio as _asyncio
                _asyncio.ensure_future(cb.on_subagent_event(SubAgentStreamEvent(
                    kind="start", subagent_name=name, task=task,
                )))

            wrapped = self._wrap_for_subagent_stream(
                name,
                base_on_step, base_on_token,
                base_on_tool_result, base_on_content_token,
            )

            run_coro = agent.run(
                task=task,
                context=context,
                context_manager=context_manager,
                on_step=wrapped["on_step"],
                on_token=wrapped["on_token"],
                on_content_token=wrapped["on_content_token"],
                on_tool_result=wrapped["on_tool_result"],
                audit_logger=audit_logger,
                artifact_store=scoped_store,
            )
```

同样修改 `dispatch_structured` 方法中的 `_run` 闭包（相同逻辑）。

对于 `dispatch` 的 `_run` 中的 end 事件，在 `_execute_with_retry` 返回之前需要发射 end 事件。在 `_execute_with_retry` 中不方便注入，所以改为在 `dispatch` 和 `dispatch_structured` 中在 result 返回前发射 end 事件。

实际上，更简洁的做法是在 dispatch 方法中，在 await result 之后发射 end 事件。但由于 _execute_with_retry 包裹了 run_fn，我们可以在 dispatch 中包裹 end 事件发射：

在 `dispatch` 方法末尾，将：

```python
        return await self._execute_with_retry(
            config, name, _run, context_manager=context_manager
        )
```

改为：

```python
        result = await self._execute_with_retry(
            config, name, _run, context_manager=context_manager
        )

        # Emit end event
        if cb and cb.on_subagent_event:
            import asyncio as _asyncio
            _asyncio.ensure_future(cb.on_subagent_event(SubAgentStreamEvent(
                kind="end", subagent_name=name,
                result={"status": "completed" if result.success else "error",
                        "content": result.data.get("content", "") if result.data else ""},
            )))

        return result
```

同样修改 `dispatch_structured` 方法的对应位置。

**注意**：`SubAgentStreamEvent.kind` 的 Literal 类型不接受任意字符串，需要确保传入的值在 Literal 中。`chunk.kind` 应在构建 SubAgentStreamEvent 时做类型转换。

在 ProxyAgent.run() 中已使用 `kind=chunk.kind`，由于 chunk.kind 可能为 None（但我们已经检查了 `chunk.kind is not None`），这里可以工作，但 mypy 可能报类型错误。在实现时加上 `# type: ignore[arg-type]` 注释。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentRunnerStreaming -v
```
Expected: PASS

- [ ] **Step 5: 运行全部测试确认无回归**

```bash
uv run pytest tests/ -x -q
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/agent/agents/subagent/runner.py tests/agent/test_subagent.py
git commit -m "feat: SubAgentRunner intercepts callbacks and emits SubAgentStreamEvent for in-process agents"
```

---

### Task 7: SSEAdapter 新增 on_subagent_event → SSE 映射

**Files:**
- Modify: `src/agent/api/sse_adapter.py`
- Modify: `tests/agent/api/test_sse_adapter.py`

- [ ] **Step 1: 编写测试**

在 `tests/agent/api/test_sse_adapter.py` 末尾追加：

```python
class TestSSEAdapterSubAgentEvents:
    """Test SSEAdapter.on_subagent_event maps to correct SSE event types."""

    @pytest.mark.asyncio
    async def test_start_event_emits_subagent_start_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="start", subagent_name="parser", task="解析文档"
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_start"
        assert data["name"] == "parser"
        assert data["task"] == "解析文档"

    @pytest.mark.asyncio
    async def test_token_event_emits_subagent_token_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="token", subagent_name="parser", text="分析中..."
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_token"
        assert data["name"] == "parser"
        assert data["text"] == "分析中..."

    @pytest.mark.asyncio
    async def test_think_event_emits_subagent_think_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="think", subagent_name="parser", detail="tool_calls:echo"
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_think"
        assert data["name"] == "parser"
        assert data["detail"] == "tool_calls:echo"

    @pytest.mark.asyncio
    async def test_tool_result_event_emits_subagent_tool_result_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="tool_result", subagent_name="parser",
            tool_name="parse", tool_status="ok",
            tool_duration=1.5, tool_summary="done"
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_tool_result"
        assert data["name"] == "parser"
        assert data["tool_name"] == "parse"
        assert data["status"] == "ok"
        assert data["duration"] == 1.5
        assert data["summary"] == "done"

    @pytest.mark.asyncio
    async def test_conclusion_event_emits_subagent_conclusion_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="conclusion", subagent_name="parser", text="解析完成"
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_conclusion"
        assert data["name"] == "parser"
        assert data["text"] == "解析完成"

    @pytest.mark.asyncio
    async def test_end_event_emits_subagent_end_sse(self):
        from src.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, create_test_store(), "s1")

        await adapter.on_subagent_event(SubAgentStreamEvent(
            kind="end", subagent_name="parser",
            result={"status": "completed", "content": "done"}
        ))

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_end"
        assert data["name"] == "parser"
        assert data["result"] == {"status": "completed", "content": "done"}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/agent/api/test_sse_adapter.py::TestSSEAdapterSubAgentEvents -v
```
Expected: FAIL — `SSEAdapter has no attribute 'on_subagent_event'`

- [ ] **Step 3: 实现 SSEAdapter.on_subagent_event**

在 `src/agent/api/sse_adapter.py` 的 `SSEAdapter` 类中添加方法。放在 `on_tool_result` 方法之后（第 218 行附近）：

```python
    async def on_subagent_event(self, event: Any) -> None:
        """Handle a SubAgentStreamEvent by emitting the corresponding SSE event.

        Maps each event kind to a ``subagent_*`` SSE event type so the
        frontend can render sub-agent activity distinctly from parent events.
        """
        await self._check_pause()

        # Import here to avoid circular dependency
        from ..agents.subagent.events import SubAgentStreamEvent

        if not isinstance(event, SubAgentStreamEvent):
            return

        kind = event.kind
        if kind == "start":
            await self._emit_sse({
                "type": "subagent_start",
                "name": event.subagent_name,
                "task": event.task,
            })
        elif kind == "token":
            await self._emit_sse({
                "type": "subagent_token",
                "name": event.subagent_name,
                "text": event.text,
            })
        elif kind == "think":
            await self._emit_sse({
                "type": "subagent_think",
                "name": event.subagent_name,
                "detail": event.detail,
            })
        elif kind == "tool_result":
            await self._emit_sse({
                "type": "subagent_tool_result",
                "name": event.subagent_name,
                "tool_name": event.tool_name,
                "status": event.tool_status,
                "duration": event.tool_duration,
                "summary": event.tool_summary,
            })
        elif kind == "conclusion":
            await self._emit_sse({
                "type": "subagent_conclusion",
                "name": event.subagent_name,
                "text": event.text,
            })
        elif kind == "end":
            await self._emit_sse({
                "type": "subagent_end",
                "name": event.subagent_name,
                "result": event.result,
            })
```

- [ ] **Step 4: 运行新测试确认通过**

```bash
uv run pytest tests/agent/api/test_sse_adapter.py::TestSSEAdapterSubAgentEvents -v
```
Expected: PASS (6 tests)

- [ ] **Step 5: 运行全部 sse_adapter 测试确认无回归**

```bash
uv run pytest tests/agent/api/test_sse_adapter.py -v
```
Expected: ALL PASS

- [ ] **Step 6: 提交**

```bash
git add src/agent/api/sse_adapter.py tests/agent/api/test_sse_adapter.py
git commit -m "feat: add on_subagent_event to SSEAdapter mapping sub-agent events to SSE"
```

---

### Task 8: stream_service + OrchestratorAgent 接线

**Files:**
- Modify: `src/agent/api/services/stream_service.py`
- Modify: `src/agent/agents/orch.py`
- Modify: `src/agent/agents/subagent/runner.py`（ProxyAgent 的 on_subagent_event 注入）

- [ ] **Step 1: 修改 stream_service — 传入 on_subagent_event**

编辑 `src/agent/api/services/stream_service.py`。在 `generate_sse_stream` 函数的 `runner()` 闭包中，找到 `agent.run()` 调用（第 223 行），添加 `on_subagent_event=adapter.on_subagent_event`：

找到：

```python
            result = await agent.run(
                task=task,
                context=agent_context,
                on_step=adapter.on_step,
                on_token=adapter.on_token,
                on_content_token=adapter.on_content_token,
                on_tool_result=adapter.on_tool_result,
                context_manager=context_manager,
                state=prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
            )
```

改为：

```python
            result = await agent.run(
                task=task,
                context=agent_context,
                on_step=adapter.on_step,
                on_token=adapter.on_token,
                on_content_token=adapter.on_content_token,
                on_tool_result=adapter.on_tool_result,
                on_subagent_event=adapter.on_subagent_event,
                context_manager=context_manager,
                state=prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
            )
```

- [ ] **Step 2: 修改 OrchestratorAgent.run — 接收并传递 on_subagent_event**

编辑 `src/agent/agents/orch.py`。

在方法签名中添加 `on_subagent_event` 参数。找到第 153-165 行的 `run` 方法签名：

```python
    async def run(
        self,
        task: str,
        context: dict[str, str] | None = None,
        on_step: Callable[[str, str], Awaitable[None]] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None = None,
        context_manager: Any = None,
        state: AgentState | None = None,
        audit_logger: Any = None,
        artifact_store: Any = None,
    ) -> AgentResult:
```

改为：

```python
    async def run(
        self,
        task: str,
        context: dict[str, str] | None = None,
        on_step: Callable[[str, str], Awaitable[None]] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None = None,
        on_subagent_event: Callable[..., Awaitable[None]] | None = None,
        context_manager: Any = None,
        state: AgentState | None = None,
        audit_logger: Any = None,
        artifact_store: Any = None,
    ) -> AgentResult:
```

在 `set_callbacks` 调用处（第 178-183 行），添加 `on_subagent_event`：

```python
        self._subagent_runner.set_callbacks(
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
            on_subagent_event=on_subagent_event,
        )
```

- [ ] **Step 3: SubAgentRunner.dispatch 注入 ProxyAgent 的 on_subagent_event**

编辑 `src/agent/agents/subagent/runner.py`。

在 `dispatch` 方法（以及 `dispatch_structured` 方法）的 `_run` 闭包中，`agent.run()` 调用之前，添加：

```python
            # For plugin-based agents (ProxyAgent), inject the on_subagent_event
            # callback at dispatch time since the ProxyAgent is created at
            # registration time by ExtensionRegistry, not at dispatch time.
            if cb and cb.on_subagent_event:
                if hasattr(agent, '_on_subagent_event'):
                    agent._on_subagent_event = cb.on_subagent_event

            run_coro = agent.run(
```

需要将 import 编辑加上 `SubAgentStreamEvent`（如果尚未导入）。在文件顶部已有 `from .events import SubAgentStreamEvent`（在 Task 6 中添加），确认存在。

找到 `dispatch` 方法中 `_run` 函数内 `run_coro = agent.run(` 所在行（修改后约第 277 行），在它之前插入 3 行。

在 `dispatch_structured` 方法中做同样修改（其 `_run` 内 `run_coro = agent.run(` 所在行前插入同样代码）。

- [ ] **Step 4: 运行端到端测试**

```bash
uv run pytest tests/ -x -q
```
Expected: ALL PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent/api/services/stream_service.py src/agent/agents/orch.py src/agent/agents/subagent/runner.py
git commit -m "feat: wire on_subagent_event through stream_service, OrchestratorAgent, and ProxyAgent"
```

---

## 实现后检查清单

- [ ] `uv run pytest tests/ -x -q` — 全部测试通过
- [ ] 旧格式 chunk（无 kind 字段）的插件行为不变
- [ ] 工具类插件（不涉及 agent.run）不受影响
- [ ] `uv run ruff check src/` — 无 lint 错误
