# 插件工具调用与子代理思考过程流式呈现实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让插件/进程内所有工具调用在执行期间向前端流式推送进度，同时把子代理思考过程也流式显示在主面板。

**架构：** 后端把 `ToolProtocol.execute()` 改为必须接收 `on_progress` 回调，宿主在调用前后 emit `tool_start`/`tool_progress`/`tool_result` SSE 事件；前端 `useAgentSession` 增量更新 `ToolResult.progress/detail`，`ToolCard` 实时渲染进度文本/计时器/流式 detail；`StepGroup` 用 `StreamingMarkdown` 渲染子代理 thoughts。

**技术栈：** Python 3.12 + FastAPI + `pydantic` / Vue 3 + TypeScript + `marked`

---

## 文件清单

### 后端

| 文件 | 职责 |
|------|------|
| `docaudit-agent/src/agent/tools/protocol.py` | 定义 `ToolProgress` 类型、`OnToolProgress` 回调类型、修改 `ToolProtocol.execute` 签名 |
| `docaudit-agent/src/agent/api/sse_adapter.py` | 新增 `on_tool_start` / `on_tool_progress` SSE 事件方法 |
| `docaudit-agent/src/agent/tools/registry.py` | `ToolRegistry.execute()` 注入 `on_progress` 并调用 SSEAdapter |
| `docaudit-agent/src/agent/core/loop_phases.py` | 在 agent loop 调用工具时传入 `on_progress` |
| `docaudit-agent/src/agent/tools/builtin/echo.py` | 更新内置工具签名并发送起止 progress |
| `docaudit-agent/src/agent/tools/builtin/get_artifact.py` | 同上 |
| `docaudit-agent/src/agent/tools/builtin/list_artifacts.py` | 同上 |
| `docaudit-agent/src/agent/tools/builtin/persist_output.py` | 同上 |
| `docaudit-agent/src/agent/agents/subagent/tool.py` | `_SubAgentTool.execute()` 接受 `on_progress` |
| `docaudit-agent/src/plugin/proxies.py` | `ProxyTool.execute()` 接受 `on_progress` 并在起止发送 progress |

### 前端

| 文件 | 职责 |
|------|------|
| `tui/src/types/agent.ts` | `ToolResult` 新增 `progress?`、`startTime?`；`SubagentRun` 新增 `thoughts?` |
| `tui/src/composables/useAgentSession.ts` | 处理 `tool_start`、`tool_progress`，合并 `tool_result`；`subagent_token`/`think` 同时更新 `SubagentRun.thoughts` |
| `tui/src/utils/toolCalls.ts` | 确保 `normalizeToolResult` 保留新增字段 |
| `tui/src/components/ToolCardCollapsed.vue` | running 时显示 `progress` 文本和实时计时器 |
| `tui/src/components/ToolCardExpanded.vue` | running 时 markdown detail 用 `StreamingMarkdown` 渲染 |
| `tui/src/components/StepGroup.vue` | 子代理卡片内新增 thoughts 流式显示区域 |
| `tui/scripts/test-tool-progress.mjs` | 新增：测试工具 progress 合并逻辑 |

---

## 任务 1：后端协议改造

**文件：**
- 修改：`docaudit-agent/src/agent/tools/protocol.py`

### 步骤 1：修改 `ToolProtocol` 签名

在 `protocol.py` 中新增：

```python
from typing import Callable, TypedDict

class ToolProgress(TypedDict):
    status: str  # "running" | "done" | "error"
    message: str
    detail: dict | None

OnToolProgress = Callable[[ToolProgress], None]
```

然后把 `ToolProtocol.execute` 改为：

```python
class ToolProtocol(Protocol):
    name: str

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        **kwargs: Any,
    ) -> ToolResult:
        ...
```

### 步骤 2：运行 mypy / 类型检查

```bash
cd /home/lmwl/Documents/docaudit/agent/docaudit-agent
uv run mypy src/agent/tools/protocol.py
```

**预期：** 无类型错误。

### 步骤 3：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/docaudit-agent
git add src/agent/tools/protocol.py
git commit -m "feat(agent): add ToolProgress and on_progress callback to ToolProtocol"
```

---

## 任务 2：SSEAdapter 新增工具事件

**文件：**
- 修改：`docaudit-agent/src/agent/api/sse_adapter.py`

### 步骤 1：新增事件方法

在 `SSEAdapter` 类中 `on_tool_result` 附近新增：

```python
async def on_tool_start(self, name: str) -> None:
    await self._emit({"type": "tool_start", "name": name})

async def on_tool_progress(self, name: str, progress: ToolProgress) -> None:
    await self._emit({
        "type": "tool_progress",
        "name": name,
        "progress": progress,
    })
```

确认 `ToolProgress` 已导入。

### 步骤 2：运行类型检查

```bash
cd /home/lmwl/Documents/docaudit/agent/docaudit-agent
uv run mypy src/agent/api/sse_adapter.py
```

### 步骤 3：Commit

```bash
git add src/agent/api/sse_adapter.py
git commit -m "feat(agent): emit tool_start and tool_progress SSE events"
```

---

## 任务 3：ToolRegistry 注入进度回调

**文件：**
- 修改：`docaudit-agent/src/agent/tools/registry.py`

### 步骤 1：修改 `execute` 方法

假设当前 `execute` 签名类似 `async def execute(self, name: str, **kwargs)`. 改为：

```python
async def execute(
    self,
    name: str,
    *,
    on_tool_start: Callable[[str], Awaitable[None]] | None = None,
    on_tool_progress: Callable[[str, ToolProgress], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> ToolResult:
    tool = self._tools.get(name)
    if tool is None:
        raise ToolNotFoundError(f"Tool not found: {name}")

    if on_tool_start:
        await on_tool_start(name)

    def on_progress(progress: ToolProgress) -> None:
        if on_tool_progress:
            asyncio.create_task(on_tool_progress(name, progress))

    result = await tool.execute(on_progress=on_progress, **kwargs)
    return result
```

如果 `execute` 当前签名不同，按实际结构调整，核心要求是：**调用 `tool.execute(..., on_progress=...)` 并在调用前后触发 SSE 回调**。

### 步骤 2：运行类型检查

```bash
uv run mypy src/agent/tools/registry.py
```

### 步骤 3：Commit

```bash
git add src/agent/tools/registry.py
git commit -m "feat(agent): inject on_progress into tool execution"
```

---

## 任务 4：Agent Loop 传入 SSE 回调

**文件：**
- 修改：`docaudit-agent/src/agent/core/loop_phases.py`

### 步骤 1：找到工具调用位置

找到调用 `tool_registry.execute(...)` 的代码行。把它改为：

```python
result = await tool_registry.execute(
    tool_name,
    on_tool_start=adapter.on_tool_start,
    on_tool_progress=adapter.on_tool_progress,
    **tool_args,
)
```

如果当前通过 `callbacks` 对象持有 adapter，则使用 `callbacks.on_tool_start` 等形式。

### 步骤 2：运行类型检查

```bash
uv run mypy src/agent/core/loop_phases.py
```

### 步骤 3：Commit

```bash
git add src/agent/core/loop_phases.py
git commit -m "feat(agent): wire SSE callbacks into tool execution loop"
```

---

## 任务 5：改造所有内置工具

**文件：**
- 修改：`docaudit-agent/src/agent/tools/builtin/echo.py`
- 修改：`docaudit-agent/src/agent/tools/builtin/get_artifact.py`
- 修改：`docaudit-agent/src/agent/tools/builtin/list_artifacts.py`
- 修改：`docaudit-agent/src/agent/tools/builtin/persist_output.py`

### 步骤 1：更新每个工具的 `execute` 签名

以 `echo.py` 为例，把：

```python
async def execute(self, **kwargs) -> ToolResult:
```

改为：

```python
async def execute(self, *, on_progress: OnToolProgress, **kwargs) -> ToolResult:
```

并在函数开头和返回前调用：

```python
on_progress({"status": "running", "message": f"开始执行 {self.name}...", "detail": None})
# ... 原有逻辑 ...
on_progress({"status": "done", "message": "执行完成", "detail": None})
return result
```

对 `get_artifact.py`、`list_artifacts.py`、`persist_output.py` 重复同样修改。

### 步骤 2：运行测试

```bash
uv run pytest tests/agent/tools/ -v
```

**预期：** 所有工具测试通过。

### 步骤 3：Commit

```bash
git add src/agent/tools/builtin/
git commit -m "feat(agent): update builtin tools to accept on_progress"
```

---

## 任务 6：改造 _SubAgentTool

**文件：**
- 修改：`docaudit-agent/src/agent/agents/subagent/tool.py`

### 步骤 1：更新签名并发送起止 progress

把 `execute` 改为接收 `*, on_progress: OnToolProgress`，在调用 runner 前后调用 `on_progress`。

```python
async def execute(self, *, on_progress: OnToolProgress, **kwargs) -> ToolResult:
    on_progress({"status": "running", "message": f"启动子代理 {self._config.name}...", "detail": None})
    result = await self._runner.dispatch(kwargs, self._config)
    on_progress({"status": "done", "message": f"子代理 {self._config.name} 完成", "detail": None})
    return result
```

### 步骤 2：运行类型检查

```bash
uv run mypy src/agent/agents/subagent/tool.py
```

### 步骤 3：Commit

```bash
git add src/agent/agents/subagent/tool.py
git commit -m "feat(agent): update SubAgentTool to accept on_progress"
```

---

## 任务 7：改造 ProxyTool

**文件：**
- 修改：`docaudit-agent/src/plugin/proxies.py`

### 步骤 1：更新 `ProxyTool.execute`

把：

```python
async def execute(self, **kwargs) -> ToolResult:
    args = self._strip_host_only_kwargs(kwargs)
    response = await self._client.call("tool.execute", {"tool": self.name, "args": args})
    return self._wrap_response(response)
```

改为：

```python
async def execute(self, *, on_progress: OnToolProgress, **kwargs) -> ToolResult:
    args = self._strip_host_only_kwargs(kwargs)
    on_progress({"status": "running", "message": f"调用插件工具 {self.name}...", "detail": None})
    response = await self._client.call("tool.execute", {"tool": self.name, "args": args})
    on_progress({"status": "done", "message": f"插件工具 {self.name} 返回结果", "detail": None})
    return self._wrap_response(response)
```

### 步骤 2：运行类型检查

```bash
uv run mypy src/plugin/proxies.py
```

### 步骤 3：Commit

```bash
git add src/plugin/proxies.py
git commit -m "feat(plugin): update ProxyTool to accept on_progress"
```

---

## 任务 8：后端测试修复与补充

**文件：**
- 查找并修改：`docaudit-agent/tests/agent/tools/` 下所有使用 `tool.execute(...)` 的测试

### 步骤 1：更新测试中的工具调用

把所有测试里直接调用 `tool.execute(...)` 的地方改为 `tool.execute(on_progress=lambda p: None, ...)`。

例如：

```python
# 旧
result = await echo_tool.execute(message="hello")

# 新
result = await echo_tool.execute(on_progress=lambda p: None, message="hello")
```

### 步骤 2：新增 progress 回调测试

为某个工具（如 `EchoTool`）新增测试，验证 `on_progress` 被调用至少两次：

```python
async def test_echo_tool_reports_progress():
    tool = EchoTool()
    progress_calls = []
    result = await tool.execute(
        on_progress=lambda p: progress_calls.append(p),
        message="hello",
    )
    assert result.status == "done"
    assert len(progress_calls) >= 2
    assert progress_calls[0]["status"] == "running"
    assert progress_calls[-1]["status"] == "done"
```

### 步骤 3：运行全部后端测试

```bash
uv run pytest -m "not integration"
```

### 步骤 4：Commit

```bash
git add tests/
git commit -m "test(agent): update tests for new on_progress protocol"
```

---

## 任务 9：前端类型扩展

**文件：**
- 修改：`tui/src/types/agent.ts`

### 步骤 1：扩展 `ToolResult`

```typescript
export interface ToolResult {
  id: string
  name: string
  skill?: string
  status: 'pending' | 'running' | 'done' | 'error'
  callKind: 'tool' | 'subagent_run'
  callScope: 'parent' | 'subagent'
  subagentName: string | null
  duration?: number
  summary?: string
  detail?: ToolDetail
  progress?: string      // ← 新增
  startTime?: number     // ← 新增
}
```

### 步骤 2：扩展 `SubagentRun`

```typescript
export interface SubagentRun {
  name: string
  task: string
  status: 'running' | 'completed' | 'error'
  conclusion?: string
  error?: string
  thoughts?: string      // ← 新增
}
```

### 步骤 3：运行类型检查

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npx vue-tsc --noEmit
```

### 步骤 4：Commit

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
git add src/types/agent.ts
git commit -m "feat(tui): add progress/startTime to ToolResult and thoughts to SubagentRun"
```

---

## 任务 10：前端工具事件处理

**文件：**
- 修改：`tui/src/composables/useAgentSession.ts`

### 步骤 1：新增 `tool_start` 处理

在 `handleEvent` 中新增：

```typescript
case 'tool_start': {
  const step = currentStep()
  if (!step || !event.name) break
  const tool = step.tools.find(
    t => t.name === event.name && t.status === 'pending'
  )
  if (tool) {
    tool.status = 'running'
    tool.startTime = Date.now()
  }
  break
}
```

### 步骤 2：新增 `tool_progress` 处理

```typescript
case 'tool_progress': {
  const step = currentStep()
  if (!step || !event.name || !event.progress) break
  const tool = step.tools.find(
    t => t.name === event.name && t.status === 'running'
  )
  if (tool) {
    tool.progress = event.progress.message
    if (event.progress.detail) {
      tool.detail = normalizeToolDetail(event.progress.detail)
    }
  }
  break
}
```

`normalizeToolDetail` 是已有的或需新增的函数，把后端 detail dict 转成 `ToolDetail`。

### 步骤 3：改造 `tool_result` 处理为合并

把原来 `splice` 替换整个 tool 的逻辑改为字段级合并：

```typescript
case 'tool_result': {
  const step = findStepForToolResult(event)
  if (!step) break
  const tool = step.tools.find(t =>
    (t.status === 'running' || !t.summary) && t.name === event.name
  )
  if (!tool) break

  tool.status = event.status ?? 'done'
  tool.summary = event.summary ?? tool.summary
  tool.detail = event.detail_data ?? tool.detail
  tool.duration = event.duration ?? tool.duration
  tool.progress = event.status === 'done' || event.status === 'error'
    ? undefined
    : tool.progress
  break
}
```

### 步骤 4：运行类型检查

```bash
npx vue-tsc --noEmit
```

### 步骤 5：Commit

```bash
git add src/composables/useAgentSession.ts
git commit -m "feat(tui): handle tool_start/progress and merge tool_result incrementally"
```

---

## 任务 11：前端子代理 thoughts 处理

**文件：**
- 修改：`tui/src/composables/useAgentSession.ts`

### 步骤 1：合并 `subagent_token` 和 `subagent_think` 处理

把两个 case 合并或分别修改为：

```typescript
case 'subagent_token':
case 'subagent_think': {
  if (!event.name || !event.text) break
  appendThought(`[${event.name}] ${event.text}`)
  upsertSubagentRun(event.name, { thoughts: event.text })
  break
}
```

### 步骤 2：更新 `upsertSubagentRun` 支持 thoughts 追加

与 `conclusion` 相同逻辑：

```typescript
if (patch.thoughts !== undefined) {
  existing.thoughts = (existing.thoughts ?? '') + patch.thoughts
}
```

### 步骤 3：运行类型检查

```bash
npx vue-tsc --noEmit
```

### 步骤 4：Commit

```bash
git add src/composables/useAgentSession.ts
git commit -m "feat(tui): accumulate subagent thoughts for main panel streaming"
```

---

## 任务 12：ToolCardCollapsed 实时进度

**文件：**
- 修改：`tui/src/components/ToolCardCollapsed.vue`

### 步骤 1：显示 progress 文本和实时计时器

在 running 状态的 meta 区域：

```vue
<span v-if="tool.status === 'running'" class="tool-status-running">
  {{ tool.progress || '执行中…' }}
  <span class="tool-duration-live">{{ liveDuration }}s</span>
</span>
```

添加 computed / interval：

```typescript
import { computed, ref, onUnmounted } from 'vue'

const liveDuration = ref(tool.duration ?? 0)
let timer: ReturnType<typeof setInterval> | null = null

if (tool.status === 'running' && tool.startTime) {
  timer = setInterval(() => {
    liveDuration.value = (Date.now() - tool.startTime!) / 1000
  }, 100)
}

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
```

### 步骤 2：运行类型检查

```bash
npx vue-tsc --noEmit
```

### 步骤 3：Commit

```bash
git add src/components/ToolCardCollapsed.vue
git commit -m "feat(tui): show live progress text and duration in ToolCardCollapsed"
```

---

## 任务 13：ToolCardExpanded 流式 detail

**文件：**
- 修改：`tui/src/components/ToolCardExpanded.vue`

### 步骤 1：导入 StreamingMarkdown 并在 running 时使用

```vue
<script setup lang="ts">
import StreamingMarkdown from './StreamingMarkdown.vue'
// ... existing imports ...
</script>
```

在模板中：

```vue
<StreamingMarkdown
  v-if="tool.detail?.type === 'markdown' && tool.status === 'running'"
  class="tool-detail-markdown"
  :content="tool.detail.content"
  :is-streaming="true"
  :speed="20"
/>
<div
  v-else-if="tool.detail?.type === 'markdown'"
  class="tool-detail-markdown"
  v-html="renderMarkdown(tool.detail.content)"></div>
```

### 步骤 2：运行类型检查

```bash
npx vue-tsc --noEmit
```

### 步骤 3：Commit

```bash
git add src/components/ToolCardExpanded.vue
git commit -m "feat(tui): stream markdown detail while tool is running"
```

---

## 任务 14：StepGroup 显示子代理 thoughts

**文件：**
- 修改：`tui/src/components/StepGroup.vue`

### 步骤 1：在结论上方新增 thoughts 区域

在子代理 task 和 conclusion 之间插入：

```vue
<StreamingMarkdown
  v-if="item.thoughts"
  class="subagent-thoughts markdown-content"
  :content="item.thoughts"
  :is-streaming="item.runStatus === 'running'"
  :speed="20"
/>
```

### 步骤 2：确保 `SubagentToolDisplayItem` 有 thoughts 字段

在 `tui/src/utils/toolCalls.ts` 的 `buildStepToolGroups` 中注入：

```typescript
item.thoughts = run.thoughts
```

如果 TypeScript 报错，先给 `SubagentToolDisplayItem` 接口加 `thoughts?: string`。

### 步骤 3：运行类型检查

```bash
npx vue-tsc --noEmit
```

### 步骤 4：Commit

```bash
git add src/components/StepGroup.vue src/utils/toolCalls.ts
git commit -m "feat(tui): stream subagent thoughts inside StepGroup"
```

---

## 任务 15：前端测试补充

**文件：**
- 创建：`tui/scripts/test-tool-progress.mjs`
- 修改：`tui/package.json`

### 步骤 1：创建测试脚本

```javascript
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const rootDir = resolve(scriptDir, '..')
const outDir = resolve(rootDir, '.tmp/tool-progress-test')

rmSync(outDir, { recursive: true, force: true })
mkdirSync(outDir, { recursive: true })

try {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, 'node_modules/typescript/bin/tsc'),
      '--target',
      'ES2020',
      '--module',
      'ES2020',
      '--moduleResolution',
      'bundler',
      '--strict',
      '--skipLibCheck',
      '--outDir',
      outDir,
      '--rootDir',
      resolve(rootDir, 'src'),
      resolve(rootDir, 'src/utils/toolCalls.ts'),
    ],
    { cwd: rootDir, stdio: 'inherit' }
  )

  const { normalizeToolResult } = await import(
    pathToFileURL(resolve(outDir, 'utils/toolCalls.js')).href
  )

  const tool = normalizeToolResult({
    id: 'tool-1',
    name: 'audit_format',
    status: 'running',
    progress: '正在解析段落...',
    startTime: 1000,
  })

  assert.equal(tool.progress, '正在解析段落...')
  assert.equal(tool.startTime, 1000)
  assert.equal(tool.status, 'running')

  console.log('toolProgress verification passed')
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
```

### 步骤 2：更新 package.json

```json
"test": "node scripts/test-tool-calls.mjs && node scripts/test-streaming-markdown.mjs && node scripts/test-tool-progress.mjs"
```

### 步骤 3：运行测试

```bash
npm test
```

### 步骤 4：Commit

```bash
git add scripts/test-tool-progress.mjs package.json
git commit -m "test(tui): add tool progress field preservation test"
```

---

## 任务 16：前后端联调与人工验证

### 步骤 1：启动后端

```bash
cd /home/lmwl/Documents/docaudit/agent/docaudit-agent
cp .env.example .env  # 如果还没有 .env
# 编辑 .env 填入真实或可用的 MySQL/MinIO/ES/LLM 凭证
PYTHONPATH=src uv run python -m uvicorn src.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

### 步骤 2：启动前端

```bash
cd /home/lmwl/Documents/docaudit/agent/tui
npm run dev
```

### 步骤 3：触发一个会调用插件工具的审核任务

通过前端输入一个会触发多步工具调用和子代理的任务。

### 步骤 4：观察并核对

| 检查项 | 预期现象 |
|--------|----------|
| 工具卡片 pending → running | 状态标签从“等待中”变为“执行中…” |
| 工具卡片 running | 显示 progress 文本，计时器实时递增 |
| 工具返回 markdown detail | running 时 detail 逐字流式出现 |
| 子代理卡片 | 显示“审计中…” + task |
| 子代理 thoughts | 主面板子代理卡片内出现流式思考文本 |
| 子代理 conclusion | 继续流式显示 |
| 工具/子代理完成 | 状态变为 done，流式停止，显示完整 Markdown |

### 步骤 5：修复问题并 commit

如果验证中发现 bug，按单个问题修复并 commit：

```bash
git add .
git commit -m "fix(tui/agent): <具体问题>"
```

---

## 自检

- **规格覆盖度：**
  - 后端工具协议改造 → 任务 1
  - SSE 事件 → 任务 2
  - 宿主注入回调 → 任务 3
  - Agent loop 接入 → 任务 4
  - 内置工具改造 → 任务 5
  - SubAgentTool 改造 → 任务 6
  - ProxyTool 改造 → 任务 7
  - 后端测试 → 任务 8
  - 前端类型扩展 → 任务 9
  - 前端工具事件处理 → 任务 10
  - 前端子代理 thoughts → 任务 11
  - ToolCardCollapsed 进度 → 任务 12
  - ToolCardExpanded 流式 detail → 任务 13
  - StepGroup thoughts → 任务 14
  - 前端测试 → 任务 15
  - 人工验证 → 任务 16
- **占位符扫描：** 无 TODO、无“后续实现”、所有步骤含实际代码或命令。
- **类型一致性：** `OnToolProgress` / `ToolProgress` 在后端任务 1-7 中一致；`progress` / `startTime` / `thoughts` 在前端任务 9-14 中一致。
