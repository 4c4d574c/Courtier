# WebUI 流式渲染性能优化 — 实施方案（先测量 + 低风险修复）

## 背景与目标

用户体感：**流式输出时界面卡顿、会话越长越卡**（中等规模单轮任务即可复现）；未在 dev/prod 间对比过。经代码侦察，主要成本集中在 token 流式路径：每个 token 触发整份消息列表重建 + markdown 整树 DOM 替换 + 双重滚动写入，且成本随会话长度超线性增长。

目标：流式输出期间主线程不再出现明显长任务，滚动/输入保持跟手；单 token 渲染成本不随历史消息增长而显著上升。**不动架构**（无虚拟滚动、无状态管理重构），以测量定位 + 低风险针对性修复推进。

## 对齐结论（已与用户确认，2026-08-28）

1. 卡顿场景：流式输出时 + 会话越长越卡。**不含**"打开/切换历史会话"与"首屏加载"（相关项列为备选，本期不做）。
2. 环境：dev/prod 未对比 → 测量阶段先补一次对比，量化 dev 固有开销，后续以生产构建为准绳。
3. 典型规模：中等单轮任务 → 不需要虚拟化/窗口化。
4. 深度：先测量确认瓶颈，再做低风险修复；结构性改造不在本期。

## 侦察结论（已验证事实）

- SSE 每 token 一个事件，后端无批处理（`runtime.py:605`、`sse_adapter.py:443`）；前端 `useAgentSession.ts:79-91` 同步逐条处理。
- **每 token 全量重建消息列表**：`buildChatMessages`（`utils/chatMessages.ts:290`）遍历所有 turn 的所有 step；`thoughtsForStep`（`utils/sessionUtils.ts:35`）对每个 step 全量 filter+sort 整个 `session.thoughts`（O(steps×thoughts)/token），非首 step 还要为上一 step 重复推导；`mergeThoughtTokens`（`sessionUtils.ts:11`）用 `[...slice, {...}]` 展开为 O(n²)；`citationsForTurn`（`chatMessages.ts:205`）每次重建新分配 Map+数组。结果：每个 token 所有 item 对象全新 → `ChatArea.vue:23-33` v-for 全量 diff，所有 `ChatMessage` 重渲染。
- **Markdown 整树替换**：`StreamingMarkdown.vue` 单个 `v-html`，`sanitizedHtml` computed 每次内容变化 = `renderStreamingHtml` + DOMPurify 全文消毒；已有 rAF 批处理（~60/s）与尾部 span 快路径（`streamingMarkdown.ts:140`），但出现结构字符（`` ` * [ 换行 `` 等）即全文重 lexer；且快路径产出的也是**新字符串** → v-html 每帧整体 `innerHTML` 重解析、整棵子树 DOM 重建，长回答尾部每帧成本递增。
- **自动滚动双写 + 无节流读取**：`ChatArea.vue:72-81` watch 每 token `nextTick` 写 `scrollTop`；`directives/autoScroll.ts:33-39` MutationObserver（含 characterData）每次 DOM 变更再写一次；`useAutoScroll.ts` scroll 监听无节流。
- **无效失效**：`turnVersion.value++`（`handlers/token.ts:32`、`state.ts:86`、`complete.ts:28`）仅定义/透传（`useAgentSession.ts:41,66,530`），无任何消费者——纯死churn。
- **step patch 级联**：每个 `tool_start/tool_progress/observe/subagent_*` 事件 splice 新 step 对象 → 全列表重建 → 该 step 所有 `ToolCard`（含已完成）重新 normalize + 重渲染（`utils/toolCalls.ts` buildStepToolGroups）。
- **每运行中工具卡独立 100ms setInterval**（`ToolCardCollapsed.vue:95`），k 个并行工具 = k 个 10Hz 定时器，与流式重渲染叠加。
- **子代理树**：`SubagentNode.vue` 递归组件；每个 `subagent_token` 沿路径 immutable spread（`composables/subagentTree.ts`）+ `patchLastStep` → 又一次全列表重建。
- **动画**：流式标题 `breathe-text` 文字颜色动画（`components.css:585,669-679`）持续重绘，与每 token 重绘叠加；`pulse-dot` 等多个 infinite 动画运行期并存；整个聊天容器 `aria-live="polite"`（`ChatArea.vue:2`）。
- dev 模式每 SSE 事件 `console.debug`（`useAgentSession.ts:83-85,329-331`）——仅 dev 体感。
- 侧边栏健康（20 行上限 + 全局事件低频）；路由懒加载齐全；包体 ~280KB 非运行时问题。

## 测量方案（Phase 0 — 修复前先做）

1. **dev vs prod 对比**（一次性）：`npm run dev` 与 `npm run build && npm run preview` 各录一段流式 Performance profile，量化 dev 固有开销；此后以 prod 为准绳。
2. **基线 profile（prod）**：Chrome DevTools Performance 面板录制一次真实 docaudit 任务的完整流式过程，统计：长任务（>50ms）数量与分布、scripting 占比、随会话增长的每帧成本趋势。
3. **重建成本增长曲线**（轻量插桩，临时代码不入库）：在 dev 下用 `performance.now()` 包住 `buildChatMessages`，按 `session.thoughts.length` 分桶记录单次耗时，验证超线性增长假设。
4. 可选：record 一次"多工具并行"片段，量化 setInterval 级联占比。

产出：一段简短测量记录（数字 + 结论）附到本文件"实施状态"，据此微调 P1 优先级。

### Phase 0 测量记录（2026-08-28，无头基准 `webui/scripts/bench-streaming-perf.mjs`）

A. `buildChatMessages` 单次调用成本（模拟一个 token 到达后重建；合成会话 = 单轮 × N 步 × 每步 2 条思考）：

| steps | thoughts | raw ms/次 | reactive() ms/次 |
|------:|---------:|----------:|-----------------:|
| 5     | 10       | 0.020     | 0.134            |
| 10    | 20       | 0.041     | 0.333            |
| 20    | 40       | 0.091     | 1.089            |
| 40    | 80       | 0.273     | 3.987            |
| 80    | 160      | 0.917     | **14.107**       |

B. `thoughtsForStep` 单次调用（raw）：80 步/160 thoughts 时 12.4µs；× 每步都调用 = raw 全量扫描约 1ms/次重建，reactive 下约 15ms——**占 A-reactive 的大头**。

C. `renderStreamingHtml` JS 侧单次更新：40KB 文档全量 re-lex 也仅 ~0.13ms（快路径 0.13ms）——**JS 解析不是瓶颈**，其代价在浏览器端整棵 `v-html` 子树的 innerHTML 重解析 + 布局/绘制（无头测不到，转浏览器实测）。

结论（优先级修订）：
1. **响应式代理税是放大器主体**（15×）：`session` 深层 `reactive()` 下，每 token 重建对每个 thought 的多属性读取被代理税逐次放大，O(steps×thoughts) 的 `thoughtsForStep` 是主导项 → **P1-2 升级为高优先**（改为每轮一次 O(T) 分组 + 组内 merge，保持语义不变）。
2. P1-1（token 批量落 state）依然最高杠杆：频率 reduction 对上述所有成本线性生效。
3. P1-3（历史 turn item 身份稳定）直接消除 Vue 对历史消息的 VDOM 重 diff（无头测不到的另一半，浏览器实测确认）。
4. P2-7 维持：价值在 DOM 重建而非 JS 解析。
5. 中等单轮（20-40 步）下每 token 的 reactive 重建 + 全列表 VDOM diff 量级在数毫秒到十几毫秒，与流式 token 频率相乘即可解释"越聊越卡"。

### Phase 0 浏览器实测记录（2026-08-28，Vite dev + 真实运行）

复用历史会话文档发起一次真实审核任务（9 步、子代理方式），页面 attach 后用 PerformanceObserver(longtask) + rAF 帧间隔采样：

- attach + 流式 + 收尾窗口内捕获 **7 个长任务，52–128ms**（PerformanceObserver longtask 阈值 50ms），集中于 attach 回放与流式密集段。
- 帧间隔 P50=4ms（多数时间空闲）、P95=21ms、P99=25ms、max=133ms。
- 局限：该次运行步数偏少、完成快，重结论流式段覆盖较薄；修复后用同场景复测对比。dev 模式的每事件 `console.debug`（P1-6）在该窗口内同样活跃。

## 修复方案（Phase 1 起逐项实施，每项独立可回退）

### P1 — 流式热路径（预期解决大部分卡顿）

1. **token 到达按帧批量落 state**：`useAgentSession` 的 SSE 处理中缓冲连续 `token` 增量，rAF/短定时器（~50ms leading+trailing）统一 flush 进 `session.thoughts`；任何非 token 事件、`complete`/`error`/stop、断线前必须先 flush（保持事件顺序与最终一致性）。重建频率直接降 1-2 个数量级。
2. **消除 O(steps×thoughts) 全量扫描**：`thoughtsForStep` 增量索引化（按 `stepIndex` 在 append 时分桶）或按 (turn, step) 记忆化，历史 step 不重算；`mergeThoughtTokens` 改 push 变异为 O(n)。
3. **历史 turn 的 item 身份稳定**：`buildChatMessages` 对已完成 turn 缓存 item 数组（WeakMap + 廉价指纹），仅运行中 turn 参与重建 → v-for 对历史消息零 diff。（若 P1-1 批量后测量已达标，可降级为可选。）
4. **自动滚动单写者**：收敛为唯一一处 rAF 节流的滚动写入；`v-auto-scroll` 的 MutationObserver 与 ChatArea watch 二选一；scroll 监听加节流。
5. **删除 `turnVersion`** 死失效链（确认无消费者后移除）。
6. **dev-only console.debug 降噪**（每事件一条 → 仅错误/关键事件）。

### P2 — 渲染放大器

7. **StreamingMarkdown 尾部快路径直写 DOM**：仅尾部文本增长时，直接改写既有 `.streaming-trailing` span 的 textContent 并跳过 v-html 字符串更新（结构字符到达才走整树替换）；DOMPurify 仅在整树替换时执行。
8. **工具卡时长定时器治理**：100ms → rAF 或 ≥500ms，仅 running 状态运行，页面隐藏时暂停；避免 k 卡 × 10Hz。
9. **step patch 级联收敛**：`buildStepToolGroups` 对已完成工具记忆化，step patch 不再重算全组。
10. **动画瘦身**：流式标题呼吸动画改 opacity/transform 或仅在流式首段启用；`aria-live` 从整个容器移到摘要级状态元素。

### P3 — 备选（本期默认不做，测量后视需要升级）

- attach/replay 事件批处理（用户未选"打开历史会话"场景）。
- 首屏字体加载策略（self-host、subset、swap）。
- marked/dompurify manualChunks 拆包。
- markdown 块级增量解析（仅重 lex 末块）——改动较大，P1/P2 后仍不达标才评估。

## 明确不做（本期）

- 虚拟滚动 / 列表窗口化（中等规模会话收益低、回归风险高）。
- session 深层 reactive → shallowRef 化的整体重构。

## 验收标准

- prod 构建 + 中等规模会话（含长结论 + 多 step + 子代理）流式全程：Performance 面板无 >100ms 长任务（或较基线数量级下降），滚动/输入跟手。
- 单 token 成本曲线趋平：随 thoughts 增长无显著上升（对照 Phase 0 曲线）。
- 功能无回归：`npm test` 全绿；手工验证流式渲染、引用点击、子代理树、历史会话打开、结论复制。
- 每项修复独立提交（conventional commits），实施偏差记录于本文件。

## 实施状态（滚动更新）

- 2026-08-28：完成代码侦察与需求对齐，方案成文，待批准后进入 Phase 0 测量。
- 2026-08-28：Phase 0 完成（无头基准 + 浏览器基线），结论修订：响应式代理税 × O(steps×thoughts) 扫描是重建成本主体；markdown JS 解析本身可忽略，其代价在 DOM 重建。
- 2026-08-28：P1 全部落地（`5d4ddeb` token 帧批处理、`d4d768a` 单轮一次分组、`44db237` 历史 turn item 缓存、`b0aa6ed` 滚动单写者+贴底门控、`e4e8a0d` 删 turnVersion、`0ef6645` dev 日志降噪）。
- 2026-08-28：P2 全部落地（`efa64b7` 尾部 span 直写 textContent、`3c14f62` 工具卡定时器 2Hz+隐藏暂停、`ee35f1c` normalizeToolResult 输入身份记忆化、`a8ee878` breathe 改 opacity + aria-live 收窄为状态级 sr-only 播报）。
- 2026-08-28：最终验证通过。
  - `npm test` 15 套件全绿；`npm run build`（vue-tsc）通过。
  - 无头基准（单流式轮 + 历史 turn 缓存）：80 步历史 + 流式尾，单 token 重建 14.1ms → **0.29ms**（reactive），且随历史规模走平。
  - 浏览器同场景复测（同文档同任务、9 步、attach + 完整流式窗口，Vite dev）：长任务（>50ms）从 **7 个（52–128ms）降到 2 个（93/53ms）**，最大帧间隔 133ms → 104ms；完成后截图核对渲染完整性无异常。
  - 偏差记录：P1-2 的方案从"增量索引"改为"每轮一次分组 + 循环携带上步文本集"（语义构造性等价，测试零改动）；P2-8 定时器取 2Hz（500ms）而非 rAF（rAF 会提高频率）；P2-9 依据 P1-3 后的级联分析收窄为 normalizeToolResult 输入身份记忆化（全组记忆化在 P1-3 后基本不命中）。P3 备选项（attach 批处理、字体加载、manualChunks）未启用——attach 回放已随 P1-1 的统一管线获得批处理，其余两项与"流式卡顿"目标无关。
