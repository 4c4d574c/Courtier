# 子代理结论流式 Markdown 显示设计

**日期:** 2026-06-16  
**主题:** 将前端子代理结论输出从整块展示改为流式 Markdown 展示  
**范围:** `tui/`（Vue 3 前端）

---

## 背景

当前 `tui/src/components/StepGroup.vue` 对子代理结论的展示分为两路：

- 运行中（`runStatus === 'running'`）：直接显示 `{{ item.conclusion }}` 纯文本。
- 完成后（`runStatus !== 'running'`）：一次性渲染完整 Markdown。

后端已经通过 `subagent_conclusion` SSE 事件将结论分段推送，前端也已经在 `useAgentSession.ts` 中逐段追加到 `SubagentRun.conclusion`。因此数据流已经是流式的，缺少的是**视觉层的流式呈现**以及**运行中的 Markdown 渲染**。

## 目标

1. 子代理结论在运行期间以字符为单位逐字“打出”。
2. 运行期间即可实时渲染 Markdown，而不是等到完成后再渲染。
3. 避免 Markdown 块在还没完整到达时就被解析成残缺 HTML（例如代码块只打到一半）。
4. 完成后立即切换为完整 Markdown 渲染，无视觉闪烁。

## 非目标

1. 不修改后端 SSE 事件格式或推送逻辑。
2. 不修改 `useAgentSession.ts` 中的事件归一化逻辑。
3. 不在本次改动中引入 HTML 净化（保持与现有 `renderMarkdown` 一致）。
4. 不实现逐字符光标或复杂动画，只保持文本逐步出现。

## 设计

### 新增组件：`StreamingMarkdown.vue`

位置：`tui/src/components/StreamingMarkdown.vue`

职责：接收完整结论字符串和流式状态，按字符逐步揭示文本，并对已完整的 Markdown 块实时渲染。

#### Props

| Prop | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `content` | `string` | - | 子代理完整结论文本（随 SSE 不断追加） |
| `isStreaming` | `boolean` | - | 是否仍在流式接收中 |
| `speed` | `number` | `20` | 每字符间隔毫秒数 |

#### 内部状态

| 状态 | 类型 | 说明 |
|------|------|------|
| `displayLength` | `number` | 当前已经揭示的字符数 |
| `timer` | `number \| null` | `setInterval` 句柄 |

#### 渲染策略

1. 使用 `marked.lexer(content.slice(0, displayLength))` 切分 token。
2. 除最后一个 token 外，其余 token 均视为“完整块”，调用 `marked.parser()` 渲染为 HTML。
3. 最后一个 token 在 `isStreaming === true` 时视为“未完成”，按原样转义后作为纯文本追加；`isStreaming === false` 时视为完整，正常渲染。
4. 当 `isStreaming` 由 `true` 变为 `false` 时，立刻将 `displayLength` 跳到 `content.length`。
5. 组件卸载时清理定时器。

### 新增工具函数：`streamingMarkdown.ts`

位置：`tui/src/utils/streamingMarkdown.ts`

职责：把“块感知 Markdown 渲染”逻辑抽成纯函数，便于单元测试。

#### 导出函数

```typescript
export function renderStreamingMarkdown(
  text: string,
  options?: { isStreaming?: boolean }
): string
```

实现要点：

- 输入为空时返回空字符串。
- 使用 `marked.lexer(text)` 获取 token 列表。
- 遍历 token：
  - 非最后一个 token → 加入 `completeTokens`。
  - 最后一个 token 且 `isStreaming === false` → 加入 `completeTokens`。
  - 最后一个 token 且 `isStreaming === true` → 转义 `token.raw` 后作为纯文本追加。
- 使用 `marked.parser(completeTokens)` 渲染完整块。
- 返回 `完整块 HTML + 未完成纯文本`。

### 修改 `StepGroup.vue`

将现有两行：

```vue
<div v-if="item.conclusion && item.runStatus !== 'running'" class="subagent-conclusion markdown-content" v-html="renderMarkdown(item.conclusion)"></div>
<div v-else-if="item.conclusion && item.runStatus === 'running'" class="subagent-conclusion subagent-conclusion--streaming">{{ item.conclusion }}</div>
```

替换为：

```vue
<StreamingMarkdown
  v-if="item.conclusion"
  class="subagent-conclusion markdown-content"
  :content="item.conclusion"
  :is-streaming="item.runStatus === 'running'"
  :speed="20"
/>
```

并移除 `StepGroup.vue` 中内联的 `renderMarkdown` 函数（逻辑迁移到 `streamingMarkdown.ts`）。

### 数据流

```
SSE subagent_conclusion chunk
  → useAgentSession.ts 追加到 SubagentRun.conclusion
  → StepGroup.vue 传入 StreamingMarkdown.content
  → StreamingMarkdown 按 speed 递增 displayLength
  → streamingMarkdown.renderStreamingMarkdown 实时渲染
  → DOM 更新
```

### 边界情况

| 场景 | 处理 |
|------|------|
| 组件卸载时动画未结束 | `onUnmounted` 清理定时器 |
| `isStreaming` 提前变为 `false` | 立刻显示完整内容，不再逐字动画 |
| `content` 被清空或变短 | `displayLength` 重置为 0，重新动画 |
| 最后一个 token 是代码块且未闭合 | 整个代码块作为纯文本显示，直到闭合或流结束 |
| 最后一个 token 是段落 | 段落整体作为纯文本，完成后才渲染为段落 HTML |

## 测试

### 单元测试

新增 `tui/scripts/test-streaming-markdown.mjs`，沿用现有 `test-tool-calls.mjs` 的 `tsc` + Node assert 模式，覆盖：

1. 空文本返回空字符串。
2. 普通段落文本在流式中显示纯文本，结束后渲染为 `<p>`。
3. 代码块未闭合时显示原始围栏文本，闭合后渲染为 `<pre><code>`。
4. 多段落场景：前面段落已完整渲染为 HTML，最后段落为纯文本。
5. 行内格式（如 `**bold**`）在段落作为最后 token 时不渲染，流结束后渲染为 `<strong>`。

### 集成验证

- `npm run build` 通过 TypeScript 类型检查。
- `npm test` 执行新增单元测试与原有 tool-call 测试。
- 手动启动 dev server，触发子代理任务，观察结论是否逐字出现且 Markdown 块在完整后正确渲染。

## 依赖

无新增依赖。继续使用现有 `marked`。

## 风险与回退

1. **性能风险**：每个字符都调用 `marked.lexer` + `marked.parser`。结论文本通常较短，风险可控；若出现卡顿，可改为按小批量（如每 3-5 个字符）推进。
2. **Markdown 解析差异**：`marked` 版本升级可能导致 token 结构变化。通过单元测试覆盖主要 token 类型可降低风险。
3. **回退方案**：若块感知逻辑出现难以修复的 bug，可降级为直接对 `content.slice(0, displayLength)` 调用 `marked.parse` 的纯流式 Markdown 方案。

## 相关文件

- `tui/src/components/StreamingMarkdown.vue`（新增）
- `tui/src/utils/streamingMarkdown.ts`（新增）
- `tui/src/components/StepGroup.vue`（修改）
- `tui/scripts/test-streaming-markdown.mjs`（新增）
- `tui/package.json`（可选：扩展 test 脚本以包含新测试）
