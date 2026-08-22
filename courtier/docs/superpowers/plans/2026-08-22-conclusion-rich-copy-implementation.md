# 最终结论复制（富文本双格式 + 引用来源附录）实现计划

> **For agentic workers:** 按 Phase 推进，Task 内 Step 用 `- [x]` 跟踪，逐 Task 约定式提交。实施中发现与计划的偏差，记录到文末「实施记录」章节。

**Goal:** 前端为每轮最终结论（审核结论 assistant 块）提供复制能力：一次复制同时写入富文本（text/html）与纯文本（text/plain）两种剪贴板格式；正文中的 `[[n]]` 引用标记保留为可见编号，并在复制内容末尾附上编号对应的来源清单。
**Architecture:** 新增纯函数构建器 `buildConclusionCopy`（Markdown 正文标记规范化 + 来源附录 → `{ text, html }`），HTML 走既有 `renderMarkdown`（marked + DOMPurify）管线；剪贴板写入封装降级链（ClipboardItem 双格式 → writeText 纯文本 → execCommand 纯文本）。`AssistantMessage.vue` 增加 hover 动作行，交互复用 `UserMessage.vue` 的「复制/已复制」模式。纯前端改动，后端零改动。
**Tech Stack:** Vue 3 + TS + scoped CSS；Node assert 自定义脚本测试（esbuild 打包后断言）。
**关联文档:** 复制交互先例见 `webui/src/components/chat/UserMessage.vue`；引用编号语义见 `webui/src/utils/chatMessages.ts`（`citationsForTurn`）与 `webui/src/types/agent.ts`（`CitationHit`）。

## 需求确认（2026-08-22 与用户核对，全部拍板）

1. 复制格式：**富文本 + 纯文本双格式**——粘贴到 Word/邮件保留标题、列表、表格样式；不支持 text/html 的目标自动落到纯文本层。
2. 引用处理：**正文保留引用标记 + 文末附来源清单**。计划取值：正文 `[[n]]` 规范化为 `[n]` 可见编号（不带链接），末尾以「参考来源」小节列出 `[n] 标题（docType）`；编号取 byNumber 绝对编号升序（允许跳号）。
3. 交互位置：**沿用用户消息样式**——结论下方 hover 显示「复制」，点击后短暂变「已复制」（约 1.5s），触屏设备（hover:none）常显。
4. 覆盖范围：**仅最终结论**（每轮的审核结论块）；步骤中间结论与子代理结论不加。
5. 运行中（流式未结束）隐藏复制按钮，流式结束后出现；与编辑入口运行中隐藏同理。

> **待复核的三个实现取值**（需求方向已定，具体形态由计划拍板，审批时可否决）：
> a) 正文标记形式取 `[n]` 而非原样 `[[n]]`（外部粘贴更干净，语义不变）。
> b) 来源条目取「标题（docType）」，不含 chunkText/highlight 摘要（保持附录紧凑）。
> c) 纯文本层取 Markdown 源文本而非去格式纯文本（无损、实现简单；粘贴到记事本会带 #、* 等记号，富文本目标是主要使用路径）。

## 背景与现状问题

| # | 事实 | 证据位置 |
|---|------|----------|
| 1 | 最终结论 = assistant item，内容为 Markdown 原文经 StreamingMarkdown 渲染；助手消息目前无任何复制入口 | `webui/src/utils/chatMessages.ts:225-249`、`webui/src/components/chat/AssistantMessage.vue` |
| 2 | 结论正文可含 `[[n]]` 引用标记，渲染时转为 citation-link；citations 已作为 prop 传入 AssistantMessage（citation-click 链路已通） | `webui/src/components/StreamingMarkdown.vue:22-37`、`webui/src/components/chat/ChatMessage.vue:10` |
| 3 | 用户气泡已有 hover「复制/已复制」实现（Clipboard API + execCommand 降级）；文案常量 CHAT_COPY/CHAT_COPIED 已存在可复用 | `webui/src/components/chat/UserMessage.vue:81-105`、`webui/src/constants/messages.ts:78-79` |
| 4 | Markdown→HTML 渲染管线现成：marked.parse + DOMPurify.sanitize，出站即安全 HTML | `webui/src/utils/markdown.ts` |
| 5 | 引用编号 = search_documents 命中的绝对编号（citationOffset 累计），可能非连续，附录须按真实编号呈现而非重新计数 | `webui/src/utils/chatMessages.ts:205-223` |
| 6 | ClipboardItem 双格式写入需 secure context（https/localhost）；内网 plain-http 场景需降级链兜底（UserMessage 已有同款约束） | `webui/src/components/chat/UserMessage.vue:88-99` |
| 7 | 前端测试为 scripts/test-*.mjs 自定义断言脚本，package.json test 显式串接，新增脚本须追加进链 | `webui/package.json:10` |

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `webui/src/utils/conclusionCopy.ts` | `buildConclusionCopy(content, citations?)` 纯函数（标记规范化 + 来源附录 → `{ text, html }`）；`writeRichClipboard({html, text})` 降级链写入 | 新增 | 1.1 |
| `webui/scripts/test-conclusion-copy.mjs` | 构建器断言测试（esbuild 打包 src 后跑 node:assert） | 新增 | 1.1 |
| `webui/package.json` | test 脚本链追加新测试脚本 | 修改 | 1.1 |
| `webui/src/constants/messages.ts` | `CHAT_CONCLUSION_SOURCES: "参考来源"` | 修改 | 2.1 |
| `webui/src/components/chat/AssistantMessage.vue` | hover 动作行 + 复制逻辑（流式中隐藏） | 修改 | 2.1 |

## Phase 1：复制内容构建器

### Task 1.1: conclusionCopy 工具 + 单测

**Files:** `webui/src/utils/conclusionCopy.ts`、`webui/scripts/test-conclusion-copy.mjs`、`webui/package.json`
**依赖:** 无

- [x] **Step 1:** `buildConclusionCopy(content: string, citations?: CitationIndex): { text: string; html: string }`：
  - 正文标记规范化：`[[数字]]` 全局替换为 `[数字]`；
  - citations 且 list 非空时，末尾追加空行 + 「参考来源」标题行 + 按编号升序每号一行 `[n] 标题（docType）`（缺 title 回退 documentId/resourceId，再缺则「未命名文档」；无 docType 不带括号）；
  - 无 citations / 空 list：不加附录；
  - text = 规范化后的完整 Markdown 文本；html = `renderMarkdown(同一段文本)`。
- [x] **Step 2:** `writeRichClipboard(payload: { html: string; text: string }): Promise<boolean>`（返回是否成功写入 html 层）：
  1. `navigator.clipboard.write([new ClipboardItem({ "text/html": Blob(html), "text/plain": Blob(text) })])`；
  2. 异常 → `navigator.clipboard.writeText(text)`；
  3. 再异常 → UserMessage 同款隐藏 textarea + `execCommand("copy")`（仅纯文本）。
- [x] **Step 3:** 测试覆盖：无引用时原文透传且 html 含渲染标签；`[[2]]` → `[2]`；跳号编号附录正确（如仅有 [3] 一条时不重排为 1）；缺 title 回退占位；citations 缺省/空 list 不加附录；html 无 script 注入。

## Phase 2：结论区交互

### Task 2.1: AssistantMessage 复制按钮

**Files:** `webui/src/components/chat/AssistantMessage.vue`、`webui/src/constants/messages.ts`
**依赖:** Task 1.1

- [x] **Step 1:** messages.ts 增 `CHAT_CONCLUSION_SOURCES`，构建器改从常量取标题文案。（另增 `CITATION_UNTITLED` 占位文案）
- [x] **Step 2:** AssistantMessage.vue 增动作行：结构样式复刻 `.user-message-actions`（hover 揭示 + focus-within + `@media (hover: none)` 常显，布局零位移）；按钮文案复用 CHAT_COPY/CHAT_COPIED；`isStreaming` 或 content 为空时整行不渲染。
- [x] **Step 3:** 点击 → `buildConclusionCopy(content, citations)` → `writeRichClipboard`；copied 态 1.5s 自动复位（含定时器清理）。
- [ ] **Step 4:** 验证：`npm test` 全绿、`npm run build` 通过 ✅；本地 dev server 用真实已完成会话人工核验 Word/记事本两侧粘贴效果（待用户执行）。

## 实施记录（2026-08-22）

- Task 1.1、Task 2.1 已实施，提交 `d1d052e`（构建器 + 单测）、`5ffc515`（结论复制按钮）。
- **偏差 1**：`webui/src/utils/markdown.ts`（计划文件职责表未列）——`sanitizeHtml` 增加非 DOM 环境容忍。Node 测试打包下 dompurify 导出的是未绑定 window 的裸工厂（实测无 `sanitize` 成员），现按运行时探测降级为直通；浏览器端行为不变。
- **偏差 2**：`constants/messages.ts` 的常量随 Task 1.1 提交（构建器引用，保证首个提交即可构建），与计划归属 Task 2.1 Step 1 不同；另增 `CITATION_UNTITLED: "未命名文档"` 占位文案。
- **测试调整**：「html 无 script 注入」断言在 Node 端不可测（消毒不生效，由浏览器端 DOMPurify 管线保障），改为断言渲染结构（h2/strong/li 等）。
- **顺手加固**：AssistantMessage 复制定时器补 `onBeforeUnmount` 清理（UserMessage 先例未清理）。
- Task 2.1 Step 4 中 `npm test` 全绿、`vue-tsc` 构建通过已完成；真实会话中 Word/记事本两侧粘贴效果的人工核验待用户执行。
