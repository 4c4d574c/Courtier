# 插件子代理流式推理/内容区分 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让插件子代理的流式行为与本地子代理一致——推理 token 进侧边栏（`subagent_token`），内容 token 进主面板（`subagent_conclusion`），逐 chunk 流式。

**Architecture:** 修改 `runtime.py` 的 `_collect_stream` 和 `run_agent` 两个方法，参照 `model.py:generate_stream_full` 的模式区分 `reasoning_content` 和 `content`。前端 `upsertSubagentRun` 将 conclusion 改为追加模式，`StepGroup.vue` 在运行中显示纯文本、完成后渲染 markdown。

**Tech Stack:** Python (pytest), TypeScript/Vue 3 (vite)

---

## 文件结构

| 文件 | 职责 | 操作 |
|---|---|---|
| `src/plugin/sdk/runtime.py:214-250` | `_collect_stream` — 流式响应消费，区分推理/内容 | 修改 |
| `src/plugin/sdk/runtime.py:367-421` | `run_agent` — agent 循环，去掉结论 yield | 修改 |
| `tests/plugin/test_runtime.py:329-442` | 现有测试适配新返回值和新事件类型 | 修改 |
| `tests/plugin/test_runtime.py` (追加) | `_collect_stream` 推理 content 新测试 | 修改 |
| `tui/src/composables/useAgentSession.ts:73-88` | `upsertSubagentRun` — conclusion 追加 | 修改 |
| `tui/src/components/StepGroup.vue:36` | 结论渲染 — 运行中纯文本 / 完成后 markdown | 修改 |

---

### Task 1: 修复 `_collect_stream` — 区分推理和内容 token

**Files:**
- Modify: `src/plugin/sdk/runtime.py:214-250`

- [ ] **Step 1: 修改 `_collect_stream` 方法**

将整个方法替换为：

```python
    async def _collect_stream(
        self,
        stream: Any,
        name: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> tuple[str, str, list[dict[str, str]]]:
        """Consume OpenAI streaming response.

        Calls *emit* for each reasoning delta as a stream_event("token") dict
        and each content delta as a stream_event("conclusion") dict.
        Returns (content_text, reasoning_text, tool_calls_list).
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta

            reasoning = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or ""
            )
            if reasoning:
                reasoning_parts.append(reasoning)
                emit(stream_event("token", name=name, text=reasoning))

            if delta.content:
                content_parts.append(delta.content)
                emit(stream_event("conclusion", name=name, text=delta.content))

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
        return "".join(content_parts), "".join(reasoning_parts), tool_calls
```

- [ ] **Step 2: 修改 `run_agent` 中调用处和解包**

`run_agent` 第 382 行：
```python
text, tool_calls = await self._collect_stream(stream, name, _emit)
```
改为：
```python
text, _reasoning, tool_calls = await self._collect_stream(stream, name, _emit)
```

`run_agent` 第 418-420 行，删除最终的 conclusion yield：
```python
                else:
                    final_text = text
                    if text:
                        yield stream_event("conclusion", name=name, text=text)
                    break
```
改为：
```python
                else:
                    final_text = text
                    break
```

- [ ] **Step 3: 运行现有测试确认失败**

```bash
uv run pytest tests/plugin/test_runtime.py -v
```
预期：`TestCollectStream` 和 `TestRunAgent` 中的测试失败（因为事件类型和返回值变了）

- [ ] **Step 4: 提交**

```bash
git add src/plugin/sdk/runtime.py
git commit -m "fix: distinguish reasoning and content tokens in plugin sub-agent streaming"
```

---

### Task 2: 更新现有测试

**Files:**
- Modify: `tests/plugin/test_runtime.py:329-442`

- [ ] **Step 1: 更新 `test_run_agent_no_tool_calls`**

将 mock `fake_collect` 的 emit 从 `"token"` 改为 `"conclusion"`，返回值改为 3 元组：

```python
    @pytest.mark.asyncio
    async def test_run_agent_no_tool_calls(self):
        from src.plugin.sdk.runtime import stream_event

        runtime = PluginRuntime()
        runtime._system_prompt = "helper"

        async def fake_collect(stream, name, emit):
            emit(stream_event("conclusion", name=name, text="hi"))
            return "hi", "", []

        runtime._collect_stream = fake_collect

        async def _noop_create(**kw):
            pass

        def make_client(**kw):
            return type("C", (), {
                "chat": type("X", (), {
                    "completions": type("Y", (), {
                        "create": staticmethod(_noop_create)
                    })()
                })()
            })()

        events = []
        async for ev in runtime.run_agent(
            name="a", task="hello",
            model_config={"api_key":"k","base_url":"u","model":"m"},
            _client_factory=make_client,
        ):
            events.append(ev)

        kinds = [e.get("kind") for e in events]
        assert kinds == ["start", "conclusion", "end"]
        assert events[0]["task"] == "hello"
        assert events[-1]["result"]["status"] == "completed"
```

- [ ] **Step 2: 更新 `test_run_agent_with_tool_call`**

```python
    @pytest.mark.asyncio
    async def test_run_agent_with_tool_call(self):
        from src.plugin.sdk.runtime import stream_event

        runtime = PluginRuntime()
        runtime._system_prompt = "agent"

        call_count = [0]
        async def fake_collect(stream, name, emit):
            if call_count[0] == 0:
                call_count[0] += 1
                return "", "", [{"id":"c1","name":"search","arguments":'{"q":"x"}'}]
            else:
                emit(stream_event("conclusion", name=name, text="done"))
                return "done", "", []

        async def fake_execute(name, args):
            return {"success": True, "data": [1], "error": None}

        runtime._collect_stream = fake_collect
        runtime._execute_tool = fake_execute

        async def _noop_create(**kw):
            pass

        def make_client(**kw):
            return type("C", (), {
                "chat": type("X", (), {
                    "completions": type("Y", (), {
                        "create": staticmethod(_noop_create)
                    })()
                })()
            })()

        events = []
        async for ev in runtime.run_agent(
            name="a", task="search",
            model_config={"api_key":"k","base_url":"u","model":"m"},
            _client_factory=make_client,
            max_turns=5,
        ):
            events.append(ev)

        kinds = [e.get("kind") for e in events]
        assert "start" in kinds
        assert "think" in kinds
        assert "tool_result" in kinds
        assert "conclusion" in kinds
        assert "end" in kinds
```

- [ ] **Step 3: 更新 `test_collect_stream_content_only`**

```python
class TestCollectStream:
    @pytest.mark.asyncio
    async def test_collect_stream_content_only(self):
        runtime = PluginRuntime()

        class Delta:
            def __init__(self, c=None): self.content = c; self.tool_calls = None

        class Choice:
            def __init__(self, delta): self.delta = delta

        class Chunk:
            def __init__(self, c): self.choices = [Choice(Delta(c))]

        async def stream():
            yield Chunk("Hello")
            yield Chunk(" world")

        events = []
        content, reasoning, tcs = await runtime._collect_stream(stream(), "a1", events.append)
        assert content == "Hello world"
        assert reasoning == ""
        assert tcs == []
        assert len(events) == 2
        assert events[0]["kind"] == "conclusion"
        assert events[0]["chunk"] == "Hello"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/plugin/test_runtime.py -v
```
预期：全部通过

- [ ] **Step 5: 提交**

```bash
git add tests/plugin/test_runtime.py
git commit -m "test: update plugin runtime tests for reasoning/content distinction"
```

---

### Task 3: 添加 `_collect_stream` 推理内容测试

**Files:**
- Modify: `tests/plugin/test_runtime.py`（在 `TestCollectStream` 类中追加）

- [ ] **Step 1: 添加 `test_collect_stream_reasoning_content` 测试**

在 `test_collect_stream_content_only` 方法后面追加：

```python
    @pytest.mark.asyncio
    async def test_collect_stream_reasoning_content(self):
        """reasoning_content emits as token, content emits as conclusion."""
        runtime = PluginRuntime()

        events = []

        class Delta:
            def __init__(self, reasoning=None, content=None):
                self.reasoning_content = reasoning
                self.content = content
                self.tool_calls = None

        class Choice:
            def __init__(self, delta): self.delta = delta

        class Chunk:
            def __init__(self, delta): self.choices = [Choice(delta)]

        async def stream():
            yield Chunk(Delta(reasoning="Let me think..."))
            yield Chunk(Delta(reasoning=" about this."))
            yield Chunk(Delta(content="The answer is 42."))

        content, reasoning, tcs = await runtime._collect_stream(stream(), "a2", events.append)

        assert content == "The answer is 42."
        assert reasoning == "Let me think... about this."
        assert tcs == []

        assert len(events) == 3
        # First two are reasoning → "token"
        assert events[0]["kind"] == "token"
        assert events[0]["chunk"] == "Let me think..."
        assert events[1]["kind"] == "token"
        assert events[1]["chunk"] == " about this."
        # Third is content → "conclusion"
        assert events[2]["kind"] == "conclusion"
        assert events[2]["chunk"] == "The answer is 42."

    @pytest.mark.asyncio
    async def test_collect_stream_reasoning_via_reasoning_attr(self):
        """Some providers use delta.reasoning instead of delta.reasoning_content."""
        runtime = PluginRuntime()

        events = []

        class Delta:
            def __init__(self):
                self.reasoning = "fallback reasoning"
                self.reasoning_content = None
                self.content = None
                self.tool_calls = None

        class Choice:
            def __init__(self, delta): self.delta = delta

        class Chunk:
            def __init__(self, delta): self.choices = [Choice(delta)]

        async def stream():
            yield Chunk(Delta())

        content, reasoning, tcs = await runtime._collect_stream(stream(), "a3", events.append)

        assert reasoning == "fallback reasoning"
        assert content == ""
        assert len(events) == 1
        assert events[0]["kind"] == "token"
        assert events[0]["chunk"] == "fallback reasoning"
```

- [ ] **Step 2: 运行新测试确认通过**

```bash
uv run pytest tests/plugin/test_runtime.py::TestCollectStream -v
```
预期：3 个测试全部通过

- [ ] **Step 3: 提交**

```bash
git add tests/plugin/test_runtime.py
git commit -m "test: add reasoning_content streaming tests for _collect_stream"
```

---

### Task 4: 前端 — `upsertSubagentRun` conclusion 改为追加

**Files:**
- Modify: `tui/src/composables/useAgentSession.ts:78-79`

- [ ] **Step 1: 修改 `upsertSubagentRun`**

将第 78-79 行：
```typescript
    if (existing) {
      Object.assign(existing, patch)
```
改为：
```typescript
    if (existing) {
      // conclusion 字段追加（逐 chunk 流式到达），其他字段覆盖
      if (patch.conclusion !== undefined) {
        existing.conclusion = (existing.conclusion ?? '') + patch.conclusion
      }
      const { conclusion: _c, ...rest } = patch
      Object.assign(existing, rest)
```

- [ ] **Step 2: 提交**

```bash
git -C /home/lmwl/Documents/docaudit/tui add src/composables/useAgentSession.ts
git -C /home/lmwl/Documents/docaudit/tui commit -m "fix: append subagent conclusion text instead of overwriting"
```

---

### Task 5: 前端 — `StepGroup.vue` 运行中纯文本、完成后 markdown

**Files:**
- Modify: `tui/src/components/StepGroup.vue:36`

- [ ] **Step 1: 修改结论渲染逻辑**

将第 36 行：
```html
        <div v-if="item.conclusion" class="subagent-conclusion markdown-content" v-html="renderMarkdown(item.conclusion)"></div>
```
改为：
```html
        <!-- Running: plain text. Completed: rendered markdown -->
        <div v-if="item.conclusion && item.runStatus !== 'running'" class="subagent-conclusion markdown-content" v-html="renderMarkdown(item.conclusion)"></div>
        <div v-else-if="item.conclusion && item.runStatus === 'running'" class="subagent-conclusion subagent-conclusion--streaming">{{ item.conclusion }}</div>
```

- [ ] **Step 2: 添加流式纯文本样式（可选，提升可读性）**

检查 `tui/src/style.css` 是否需要新增 `.subagent-conclusion--streaming` 样式。当前 `.subagent-conclusion` 已有基本样式（padding、font-size、border-left），纯文本模式下这些样式足够。如需要，追加：

```css
.subagent-conclusion--streaming {
  white-space: pre-wrap;
}
```

- [ ] **Step 3: 提交**

```bash
git -C /home/lmwl/Documents/docaudit/tui add src/components/StepGroup.vue
git -C /home/lmwl/Documents/docaudit/tui commit -m "fix: show subagent conclusion as plain text during streaming, markdown after completion"
```

---

### Task 6: 运行全部测试确认

- [ ] **Step 1: 运行后端测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/plugin/test_runtime.py -v
```
预期：全部通过

- [ ] **Step 2: 运行前端类型检查**

```bash
cd /home/lmwl/Documents/docaudit/tui && npx tsc --noEmit --pretty false 2>&1 | head -20 || true
```
预期：无新增类型错误
