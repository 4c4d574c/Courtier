# 插件子代理流式推理/内容区分

**日期**: 2026-06-10
**状态**: 已确认

## 问题

插件子代理（`PluginRuntime.run_agent()` / `_collect_stream`）的流式处理存在两个与本地子代理不一致的问题：

1. **推理 token 丢失** — `_collect_stream` 未检查 `delta.reasoning_content`，推理模型的 CoT 思考过程完全不可见
2. **内容 token 类型错误** — `delta.content` 以 `"token"` 类型发出，而本地子代理以 `"conclusion"` 类型发出

## 目标

让插件子代理的流式行为与本地子代理完全一致：

| delta 字段 | 流式事件类型 | 前端展示 |
|---|---|---|
| `delta.reasoning_content` | `"token"`（逐 chunk） | 侧边栏「思考」面板，可折叠 |
| `delta.content` | `"conclusion"`（逐 chunk） | 主面板，子代理状态栏下方 |

## 变更范围

### 后端：`src/plugin/sdk/runtime.py`

**`_collect_stream` 方法：**

- 新增 `delta.reasoning_content` / `delta.reasoning` 检测（参照 `model.py:generate_stream_full` 414-422行）
- 推理内容以 `stream_event("token", ...)` 发出
- `delta.content` 改为 `stream_event("conclusion", ...)` 发出
- 返回值从 `(text, tool_calls)` 扩展为 `(content_text, reasoning_text, tool_calls)`

**`run_agent` 方法：**

- 去掉末尾的单次 `yield stream_event("conclusion", ...)`（内容已逐 chunk 流式发出）
- 适配 `_collect_stream` 新的三元组返回值

### 前端：`/home/lmwl/Documents/docaudit/tui`

**`src/composables/useAgentSession.ts` — `upsertSubagentRun`：**

- `conclusion` 字段改为追加（字符串拼接），其他字段保持覆盖行为
- 因为 `subagent_conclusion` 事件现在逐 chunk 到达，每个 chunk 需要拼接到已有结论上

**`src/components/StepGroup.vue`：**

- 流式过程中：`conclusion` 以纯文本追加显示（不做 markdown 解析）
- `subagent_end` 后：将完整文本渲染为 markdown（`marked.parse()`）
- 实现方式：在 subagent 状态为 `'running'` 时用 `v-text` 显示纯文本，状态变为 `'completed'` 时切换为 `v-html="renderMarkdown(...)"`

## 不变

- 前端 SSE 事件类型名称不变（`subagent_token`、`subagent_conclusion` 等）
- `SubAgentStreamEvent` 数据类不变
- `SubAgentRunner._wrap_for_subagent_stream` 不变
- JSON-RPC 传输层不变
