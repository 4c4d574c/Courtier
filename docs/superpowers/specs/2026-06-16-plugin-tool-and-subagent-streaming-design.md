# 插件工具调用与子代理思考过程流式呈现设计

**日期:** 2026-06-16  
**主题:** 让插件中所有工具调用和子代理思考过程都在前端流式呈现  
**范围:** `docaudit-agent/`（后端协议与事件） + `tui/`（前端组件与状态）

---

## 背景

当前系统里：

- **插件工具**（`ProxyTool.execute`）是请求-响应模式：宿主发一次 JSON-RPC `tool.execute`，插件执行完返回一个结果，然后宿主 emit 单个 `tool_result` SSE 事件。执行期间没有任何进度事件。
- **插件子代理**（`ProxyAgent.run`）已经是流式：通过 JSON-RPC stream 发送 `subagent_start/token/think/tool_result/conclusion/end` 事件。
- **前端** `ToolCard` 只能显示静态状态（`pending/running/done/error`），running 时只有“执行中”文字，没有进度内容。
- 子代理结论已通过 `StreamingMarkdown` 流式显示在主面板，但思考过程只出现在侧边栏，且不是流式块渲染。

## 目标

1. **所有工具调用流式化**：插件工具 + 进程内技能工具在执行期间都能向前端推送进度。
2. **前端工具卡片流式呈现**：running 状态实时显示进度文本/计时器，markdown detail 用 `StreamingMarkdown` 逐字显示。
3. **子代理思考过程在主面板流式呈现**：把 `subagent_token` / `subagent_think` 同时汇总到子代理卡片内，用 `StreamingMarkdown` 显示。
4. 修改完成后，运行前后端并人工验证流式效果。

## 非目标

1. 不改 LLM token 生成流式（`token`/`usage` 事件保持原样）。
2. 不改造非工具类的外部调用。
3. 不改 verdict 的渲染方式。
4. 不引入新的依赖库（DOMPurify、Vitest 等）。

## 设计

### 1. 后端工具协议改造

#### 1.1 `ToolProtocol` 签名变更

```python
# docaudit-agent/src/agent/tools/protocol.py
from typing import Protocol, Callable, Any

class ToolProgress(TypedDict):
    status: str  # "running" | "done" | "error"
    message: str
    detail: dict | None

OnToolProgress = Callable[[ToolProgress], None]

class ToolProtocol(Protocol):
    name: str

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        **kwargs: Any
    ) -> ToolResult:
        ...
```

所有现有工具的 `execute` 方法都必须增加 `*, on_progress: OnToolProgress` 形参。

#### 1.2 默认/最小实现模式

对于没有真实进度可报的工具，在函数开头调用一次 `on_progress({"status": "running", "message": f"开始执行 {self.name}...", "detail": None})`，在返回前再调用一次 `{"status": "done", "message": "执行完成", "detail": None}`。

这样所有工具都满足新协议，同时真实有进度的工具可以多次调用 `on_progress`。

#### 1.3 宿主调用层注入回调

在 `ToolRegistry.execute()` 或 agent loop 调用工具的地方：

1. 先调用 `SSEAdapter.on_tool_start(tool_name)`，emit SSE `tool_start`。
2. 把 `on_progress` 回调绑定到 `SSEAdapter.on_tool_progress`，emit SSE `tool_progress`。
3. 工具返回后调用 `SSEAdapter.on_tool_result(result)`，emit SSE `tool_result`。

#### 1.4 `ProxyTool.execute()` 流式化

```python
# docaudit-agent/src/plugin/proxies.py
async def execute(self, *, on_progress: OnToolProgress, **kwargs) -> ToolResult:
    args = self._strip_host_only_kwargs(kwargs)

    # 1. 发送开始进度
    on_progress({"status": "running", "message": f"调用 {self.name}...", "detail": None})

    # 2. 尝试流式调用
    response = await self._client.call("tool.execute", {"tool": self.name, "args": args})
    #    ↑ 这里先保持 request/response，因为插件 SDK 目前 tool.execute 是同步函数。
    #      若插件返回的 dict 里包含 "progress_chunks"，可逐个回调 on_progress。

    return ToolResult(...)
```

> **说明**：如果要把插件侧也改成真正的 async generator 流式，需要同时改 `PluginRuntime._handle_request_async()` 和 `JSONRPCClient`，工作量大。第一阶段采用“宿主侧在调用前后 emit start/progress/done”即可让前端有流式动画；第二阶段再考虑让插件内部也 yield progress。

#### 1.5 SSE 事件新增

在 `docaudit-agent/src/agent/api/sse_adapter.py` 新增：

```python
async def on_tool_start(self, name: str) -> None:
    await self._emit({"type": "tool_start", "name": name})

async def on_tool_progress(self, name: str, progress: ToolProgress) -> None:
    await self._emit({"type": "tool_progress", "name": name, "progress": progress})
```

`on_tool_result` 保持现有逻辑，emit `tool_result`。

### 2. 前端工具流式呈现

#### 2.1 `ToolResult` 类型扩展

```typescript
// tui/src/types/agent.ts
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
  progress?: string        // ← 新增：当前进度文本
  startTime?: number       // ← 新增：用于实时计时
}
```

#### 2.2 `useAgentSession.ts` 新增事件处理

```typescript
case 'tool_start': {
  const step = currentStep()
  if (!step) break
  const tool = step.tools.find(t => t.name === event.name && t.status === 'pending')
  if (tool) {
    tool.status = 'running'
    tool.startTime = Date.now()
  }
  break
}

case 'tool_progress': {
  const step = currentStep()
  if (!step || !event.name || !event.progress) break
  const tool = step.tools.find(t => t.name === event.name && t.status === 'running')
  if (tool) {
    tool.progress = event.progress.message
    if (event.progress.detail) {
      tool.detail = normalizeToolDetail(event.progress.detail)
    }
  }
  break
}
```

`tool_result` 处理逻辑与现有合并，但不再简单 `splice` 替换，而是字段级合并：保留 `progress`、`startTime`，覆盖 `status`、`duration`、`summary`、`detail`。

#### 2.3 `ToolCardCollapsed.vue` 改造

- running 状态时，优先显示 `tool.progress`；没有 progress 时显示“执行中…”。
- running 状态时，显示实时计时器：`((Date.now() - tool.startTime) / 1000).toFixed(1)s`。
- 添加 CSS 脉冲动画（复用 `subagent-status-dot` 样式或新增 `.tool-status-pulse`）。

#### 2.4 `ToolCardExpanded.vue` 改造

- 当 `tool.detail?.type === 'markdown'` 且 `tool.status === 'running'` 时，使用 `StreamingMarkdown` 组件渲染 `tool.detail.content`。
- 完成后切换为静态 markdown 渲染（或继续用 StreamingMarkdown 但 `isStreaming=false`）。

### 3. 子代理思考过程在主面板流式呈现

#### 3.1 `SubagentRun` 扩展

```typescript
export interface SubagentRun {
  name: string
  task: string
  status: 'running' | 'completed' | 'error'
  conclusion?: string
  error?: string
  thoughts?: string   // ← 新增：汇总思考文本
}
```

#### 3.2 `useAgentSession.ts` 改造

现有 `subagent_token` / `subagent_think` 只 append 到侧边栏 thought。增加：

```typescript
case 'subagent_token':
case 'subagent_think': {
  if (!event.name || !event.text) break
  appendThought(`[${event.name}] ${event.text}`)  // 保持侧边栏
  upsertSubagentRun(event.name, { thoughts: event.text })  // 追加到主面板
  break
}
```

`upsertSubagentRun` 对 `thoughts` 做字符串追加（与 `conclusion` 相同逻辑）。

#### 3.3 `StepGroup.vue` 改造

在子代理结论上方新增 thinking 区域：

```vue
<StreamingMarkdown
  v-if="item.thoughts"
  class="subagent-thoughts markdown-content"
  :content="item.thoughts"
  :is-streaming="item.runStatus === 'running'"
  :speed="20"
/>
```

> 视觉区分：thoughts 区域使用更淡的颜色/更小的字号，避免与 conclusion 混淆。

### 4. 数据流

```
后端工具执行
  → ToolRegistry.execute 注入 on_progress
    → on_tool_start        → SSE "tool_start"
    → on_progress          → SSE "tool_progress" (多次)
    → on_tool_result       → SSE "tool_result"
      → 前端 useAgentSession
        → 更新 ToolResult.status/progress/detail
          → ToolCard 实时渲染进度 + StreamingMarkdown

后端子代理执行
  → ProxyAgent.run / SubAgentRunner
    → subagent_token/think → SSE "subagent_token" / "subagent_think"
      → 前端 useAgentSession
        → appendThought + upsertSubagentRun.thoughts
          → StepGroup 内 StreamingMarkdown 流式显示
```

### 5. 边界情况

| 场景 | 处理 |
|------|------|
| 工具未实现真实进度 | 调用方在 start/done 各发一次 progress，前端至少看到“开始/完成”。 |
| `tool_progress` 在 `tool_result` 之后到达 | 忽略，因为 tool 已 `done`/`error`。 |
| 子代理没有 thoughts | thoughts 区域不渲染。 |
| 子代理 thoughts 很长 | `StreamingMarkdown` 按字符流式，性能与结论一致。 |
| 页面刷新 | 已有历史数据不会重新流式，只显示最终状态。 |

### 6. 测试

#### 6.1 单元测试

- `tui/src/utils/streamingMarkdown.ts`：保持现有测试，新增 detail markdown 流式场景（可复用 StreamingMarkdown 组件逻辑）。
- 新增 `tui/scripts/test-tool-progress.mjs`：测试 `normalizeToolResult` 对 `progress` 字段的保留，以及工具状态合并逻辑。

#### 6.2 后端单元/集成测试

- 更新 `docaudit-agent/tests/agent/tools/` 中现有工具的 mock，确保新签名通过。
- 新增测试：调用工具时 `on_progress` 被调用至少两次（start + done）。

#### 6.3 手动验证（用户强制要求）

1. 启动后端：`cd docaudit-agent && PYTHONPATH=src uv run python -m uvicorn src.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000`
2. 启动插件（确保 plugins/ 目录下插件已启动）。
3. 启动前端：`cd tui && npm run dev`
4. 触发一个会调用插件工具的审核任务。
5. 观察：
   - 工具卡片 running 时显示进度文本和实时计时器。
   - markdown detail 是否逐字出现。
   - 子代理卡片内是否流式显示思考过程。
   - 子代理结论是否继续流式显示。

## 相关文件

- `docaudit-agent/src/agent/tools/protocol.py`
- `docaudit-agent/src/agent/tools/registry.py`
- `docaudit-agent/src/plugin/proxies.py`
- `docaudit-agent/src/agent/api/sse_adapter.py`
- `docaudit-agent/src/agent/api/services/stream_service.py`
- `tui/src/types/agent.ts`
- `tui/src/composables/useAgentSession.ts`
- `tui/src/utils/toolCalls.ts`
- `tui/src/components/ToolCardCollapsed.vue`
- `tui/src/components/ToolCardExpanded.vue`
- `tui/src/components/StepGroup.vue`
- `tui/src/components/StreamingMarkdown.vue`

## 风险

1. **工具协议签名变更**需要更新所有现有工具实现（包括插件和进程内技能），工作量大。
2. **插件协议**若要从 request/response 改为 true streaming，需要改 `PluginRuntime` + `JSONRPCClient`，建议分阶段：先做宿主侧 start/progress/done，再做插件侧真实流式。
3. **前端状态合并**从替换改为合并，可能影响现有 `tool_result` 处理逻辑，需充分测试。

## 推荐节奏

建议分两阶段：

**第一阶段（本需求）**：完成宿主侧 `tool_start/progress/result` + 前端工具卡片流式 + 子代理 thinking 主面板流式。所有工具实现最小改造（只加 `on_progress` 参数并在起止调用）。

**第二阶段（可选）**：让插件工具内部也能 yield progress，实现真正的细粒度进度流式。
