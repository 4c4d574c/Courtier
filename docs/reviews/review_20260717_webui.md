# 代码审查报告

## 基本信息

- **审查日期**：2026-07-17
- **审查模块**：WebUI 前端核心（`courtier/webui/src/`，Vue 3 + TypeScript）
- **审查范围**：
  - 主审：`composables/useAgentSession.ts`、`composables/sessionEventHandlers/`（index/state/tool/subagent/complete/finalize/utils/types + handlers/ 全部 12 个）、`api/client.ts`、`utils/toolCalls.ts`、`utils/streamingMarkdown.ts`
  - 交叉：`views/HomeView.vue`、`composables/useAutoScroll.ts`、`tsconfig.json`
- **代码版本**：HEAD `4acd4e1` + 工作区未提交改动（webui 有大面积未提交变更，审查基于工作区实况）
- **审查人**：Kimi Code（AI agent）
- **总耗时**：约 25 分钟
- **环境检查**：
  - ✅ 类型检查与构建：`npm run build`（vue-tsc + vite build）通过；`tsconfig` 启用 `strict: true`、`noUnusedLocals`、`noUnusedParameters`
  - ✅ 测试：`npm test`（10 个 Node 断言脚本）全部通过
  - ⚠️ 工作区存在大面积未提交改动，本次审查非固定 commit
  - ⚠️ webui 无 ESLint 配置——前端无风格/规则门禁，仅有 vue-tsc 类型门

---

## 文件概览

| 文件路径 | 总行数 | 问题数 🔴🟡🟢 | 整体评价 | 备注 |
|---------|-------|--------------|---------|------|
| `api/client.ts` | 330 | 0/3/2 | 需关注 | token 入 URL；401 刷新竞争 |
| `composables/useAgentSession.ts` | 262 | 0/1/0 | 清晰 | connect 异步竞态 |
| `sessionEventHandlers/index.ts` | 97 | 0/0/0 | 清晰 | 纯分发，职责单一 |
| `sessionEventHandlers/state.ts` | 273 | 0/0/0 | 清晰 | 1-based turnIndex 约定一致 |
| `sessionEventHandlers/tool.ts` | 143 | 0/0/0 | 清晰 | subagent wrapper 路由完整 |
| `sessionEventHandlers/subagent.ts` + `handlers/subagent.ts` | 242 | 0/0/0 | 清晰 | pendingSubagents 缓冲设计合理 |
| `sessionEventHandlers/handlers/{think,act,observe,token,...}.ts` | ~250 | 0/0/2 | 清晰 | 字符串协议（迁移期） |
| `sessionEventHandlers/{complete,finalize,utils,types}.ts` | 226 | 0/0/1 | 清晰 | finalize 覆盖全部异常终态 |
| `utils/toolCalls.ts` | 309 | 0/0/0 | 清晰 | 归一化与展示分组逻辑干净 |
| `utils/streamingMarkdown.ts` | 250 | 0/1/0 | 清晰 | 3 处 `as any` 断言 |
| `views/HomeView.vue`（交叉） | 175 | 0/1/2 | 需关注 | unmount 不断开 SSE |
| `composables/useAutoScroll.ts`（交叉） | 51 | 0/0/1 | 清晰 | v-if 下监听可能不挂载 |

---

## 详细审查记录

### 1. `api/client.ts`

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| 293-307 | 🟡 | 安全 | `token` 拼进 `/api/sessions?...&token=...` | JWT 出现在 URL 中，会进入浏览器历史、服务器访问日志、反向代理日志；EventSource 不支持自定义 header 是现实约束，但这是已知的凭证泄露面 | 短期：确保 access token 短 TTL、后端访问日志 scrub query；根治：改用一次性 SSE ticket（POST 换取一次性 ticket 放入 query）或 cookie-only 会话 | 待修复 |
| 52-76 | 🟡 | 逻辑 | 401 → 直接发 refresh | 多个并发请求同时 401 时各自发起 refresh；若后端做 refresh token 轮换，先到请求使旧 token 失效，后到的 refresh 失败 → 用户被意外登出 | 用模块级单例 Promise 去重：`refreshPromise ??= doRefresh().finally(() => refreshPromise = null)` | 待修复 |
| 191-253 | 🟡 | 契约 | `throw new Error(\`Failed: ${res.status}\`)` | admin/profile 全部写操作丢失后端 `detail` 错误文案（如"用户名已存在""当前密码错误"），UI 只能显示状态码；`login`/`register` 已有 detail 解析，同一文件两套标准 | 抽出共享的 `parseErrorDetail(res)` helper，统一错误格式 | 待修复 |
| 83-100 | 🟢 | 一致性 | `request()` 带 10s 超时 | 仅 `post/stop/deleteSession` 经过它；`postJson/uploadFile/listUsers` 等无超时，弱网下永远挂起 | 超时逻辑下沉到 `authFetch` 或统一走 `request` | 可选 |
| 116-122 | 🟢 | 类型 | `as [string, string][]` | 类型断言，但前置 filter 已排除 undefined/null，断言安全 | 无需处理（合规断言） | 已确认 |

### 2. `composables/useAgentSession.ts`

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| 94-129 | 🟡 | 异步 | `api.createEventSource(...).then(es => { ... eventSource.value = es; })` | EventSource 在 Promise 回调中才赋值：若 `.then` 到达前调用了 `disconnect()`/`stop()`/`newSession()`，`disconnect()` 对 `null` 空操作，随后旧 es 仍被赋值并接收事件 → 僵尸流；快速连续两次 `connect()` 同理，第一个流被覆盖且永不 close，两路事件写入同一 session 状态造成串扰 | 引入 generation 计数：`connect()` 递增代际，`.then` 中校验代际仍有效才赋值，否则 `es.close()`；或同步创建 EventSource（`createEventSource` 本是同步构造再包 Promise） | 待修复 |

#### 函数级审查

**函数名：`connect`**
- 行数范围：53-137
- 职责描述：重置会话状态、推入用户 turn、建立 SSE 连接并接线事件处理

审查项：
- □ 输入校验：`JSON.parse(e.data)` 有 try/catch 兜底，解析失败置 errorMessage 但不中断流，合理
- □ 边界条件：新会话/续会话两条重置路径完整；1-based `currentTurnIndex` 与 `state.ts:49` 的 `turns[turnIndex - 1]` 约定自洽（已交叉验证）
- □ 异常处理：`.catch` 覆盖连接失败；`es.onerror` 在终态时正确 close；重连计数只在 onmessage 归零，语义正确
- □ 副作用：见批注表（异步赋值竞态）
- □ 返回值：void，状态经 reactive 输出，一致

**函数名：`stop`**
- 行数范围：193-202
- 审查结论：远端失败有兜底日志、本地状态照样终结，设计合理；`disconnect()` 同样受上述竞态影响（同一根因，不单列）

### 3. `composables/sessionEventHandlers/`（事件处理层）

#### 逐行批注

| 文件:行号 | 级别 | 类别 | 问题描述 | 建议修改 | 状态 |
|-----------|------|------|---------|---------|------|
| `handlers/think.ts:19,42` | 🟢 | 结构 | `detail` 字符串协议（`"tool_calls: a, b"` / `"text_response"`）——前端解析依赖后端 on_step 字符串格式，与后端同款迁移期临时协议 | 事件总线迁移完成后改结构化负载（工具名列表直接放 payload） | 记录债务 |
| `handlers/think.ts:50,56` | 🟢 | 冗余 | `s.observedSinceLastStep = false` 在 else 分支内与分支外重复赋值 | 删除分支内那次 | 可选 |
| `utils.ts:61` | 🟢 | 逻辑 | `findMatchingToolIndex`：状态为 done 但无 summary 的工具仍会被后续同名结果匹配（`status !== "running" && tool.summary` 才跳过）——后端目前总发 summary，属潜在误匹配 | 跳过条件加 `tool.status === "done" \|\| tool.status === "error"` | 可选 |

#### 正向评价

- 模块化清晰：每种事件一个 handler，`index.ts` 纯 switch 分发，无上帝函数
- `finalize.ts` 对所有异常终态（error/stopped/连接中断）统一收尾 pending/running 操作，并双向同步 `session.steps` 与 `turn.steps`，无遗漏路径
- `pendingSubagents` 缓冲（subagent_start 先于 step 存在时）是典型的乱序事件正确处理
- `handlers/usage.ts` 与 `complete.ts` 的 tokens 语义自洽：usage 事件增量累加、complete 事件以服务端权威总量覆盖——配合后端 `drop_oldest` 背压可能丢 usage 的场景，最终值仍正确
- 类型守卫（`isToolDetail`/`isRecord`/`asToolStatus`）集中在 `utils.ts`，符合 Skill 倡导的"类型守卫替代断言"

### 4. `utils/toolCalls.ts` + `utils/streamingMarkdown.ts`

#### 逐行批注

| 文件:行号 | 级别 | 类别 | 问题描述 | 建议修改 | 状态 |
|-----------|------|------|---------|---------|------|
| `streamingMarkdown.ts:34-35,150,179` | 🟡 | 类型 | 三处 `(token as any)` / `(lastBlock as any).tokens`——绕过类型系统访问 marked Token 的 `tokens`/`raw` 字段 | marked 导出 `Tokens.Generic` 等判别联合，可用自定义 `interface BlockWithTokens extends Token { tokens: Token[] }` 收窄替代 | 待修复 |

#### 函数级审查（streamingMarkdown）

**函数名：`renderStreamingHtml`**
- 行数范围：97-189
- 职责描述：流式渲染 markdown，缓存上一次解析结果，仅尾随文本增量时原地 patch

审查项：
- □ 输入校验：`typeof text !== "string"` 防御、空文本清缓存，完整
- □ 边界条件：增量判定要求新字符不含结构字符（`STRUCTURAL_RE`），否则全量重解析——正确防止"半个 `**` 被缓存成永久文本"的经典流式渲染 bug；`text` 缩短（非 startsWith）也回落全量 ✓
- □ 异常处理：marked 解析异常未捕获——异常会冒泡到调用方（组件渲染）；marked 对任意字符串输入不抛错，风险低，建议备注
- □ 副作用：仅内部 cache，无全局状态（`streamingMarked` 实例只读使用）✓
- □ 返回值：HTML 字符串；`renderer.html` 全量转义 + 下游 DOMPurify 双重 XSS 防护 ✓

**正向评价**：trailing span 原地 patch + 结构字符检测是教科书级的流式 markdown 实现，`renderStreamingParts` 的 legacy 包装保持旧测试兼容，工程处理得当。

### 5. `views/HomeView.vue` + `useAutoScroll.ts`（交叉）

#### 逐行批注

| 文件:行号 | 级别 | 类别 | 问题描述 | 建议修改 | 状态 |
|-----------|------|------|---------|---------|------|
| `HomeView.vue:172-174` | 🟡 | 副作用 | `onUnmounted` 只 `revokeUploadedFiles()`，**未断开 EventSource**：流式进行中导航到 /profile、/admin 后，SSE 继续接收事件并写入已卸载组件持有的旧 session 对象（闭包引用使其无法 GC）；回到首页时 `useAgentSession()` 是全新状态，旧流成为孤儿，运行中的会话在前端"消失"（只能靠历史记录手动 restore） | `onUnmounted` 中调用 `disconnect()`；如需"后台继续跑"的产品语义，则把 session 状态提升到模块级单例并在 remount 时复用，同时补 `es.close()` 的兜底 | 待修复 |
| `HomeView.vue:143-157` | 🟢 | 死代码 | `handleFork`/`handleRewind` 的 try/catch 永不触发——`forkSession`/`rewindSession` 内部已捕获所有异常并写入 `session.errorMessage`；且外层 catch 写入的是另一个错误通道 `uploadError`，两通道语义混乱 | 删除外层 try/catch，统一错误通道 | 可选 |
| `HomeView.vue:104,107` | 🟢 | 冗余 | `uploading.value = false` 在 catch 与 finally 中各一次 | 删 catch 内那次 | 可选 |
| `useAutoScroll.ts:26-30` | 🟢 | 边界 | `mount()` 时 `containerRef.value` 若因 v-if 未渲染则为 null，scroll 监听永不挂载，`isAtBottom` 恒 true → 总是强制滚动 | 调用方在 v-if 元素出现后再 mount，或内部用 watch 等 ref 就绪 | 可选 |

---

## 问题汇总

### 🔴 阻塞项

无。前端核心事件链路在异常终态覆盖、乱序事件缓冲、XSS 防护、类型严格性上处理到位，未发现会导致崩溃/数据丢失/确定安全漏洞的问题。

### 🟡 警告项（本周内修复）

1. `client.ts:293-307`：JWT 经 URL query 传递 → 泄露面进入各类日志；短期 scrub 日志 + 短 TTL，根治换一次性 SSE ticket
2. `client.ts:52-76`：401 自动刷新无并发去重 → refresh 轮换场景可能误登出用户 → 单例 Promise 去重
3. `client.ts:191-253`：admin/profile 写操作丢失后端 `detail` 错误文案 → 统一 `parseErrorDetail`
4. `useAgentSession.ts:94-129`：`connect()` 的 EventSource 异步赋值竞态 → 僵尸流/双串流状态串扰 → 代际校验或同步赋值
5. `HomeView.vue:172`：路由离开时不断开 SSE → 连接泄漏 + 状态孤儿 → unmount 调 `disconnect()`（或改单例 + 复用策略）
6. `streamingMarkdown.ts:34-35,150,179`：3 处 `as any` → 用判别接口收窄

### 🟢 建议项（下次迭代处理）

1. `handlers/think.ts:50,56`：`observedSinceLastStep` 重复赋值 → 删一处
2. `handlers/think.ts:19,42`：on_step 字符串协议（同后端迁移期产物）→ 迁移完成后结构化
3. `utils.ts:61`：`findMatchingToolIndex` 对"done 无 summary"工具的潜在误匹配 → 收紧跳过条件
4. `client.ts:83-100`：超时不统一（仅部分方法有 10s）→ 下沉到 `authFetch`
5. `HomeView.vue:143-157`：fork/rewind 外层死 catch + 双错误通道 → 删除并统一
6. `HomeView.vue:104,107`：`uploading=false` 重复赋值
7. `useAutoScroll.ts:26-30`：v-if 下监听不挂载的边缘 → watch ref 就绪

---

## 技术债务记录

- **SSE 认证方式**：token-in-query 是 EventSource 限制的妥协方案，长期应迁移到一次性 ticket 或 cookie 会话（见 🟡-1）
- **on_step 字符串协议**（`think`/`act`/`usage` 的 detail 字符串）：前后端共享的迁移期临时协议，前端多处 `startsWith`/`split` 解析依赖其格式，后端事件总线迁移完成时需同步结构化（见后端审查报告同名条目）
- **webui 无 ESLint**：目前只有 vue-tsc 类型门，建议补 `eslint + eslint-plugin-vue`（rules: vue/recommended + no-async-in-mounted 等），纳入 CI
- **会话状态生命周期**：session 状态绑定在 HomeView 组件实例上，路由切换即孤儿化（见 🟡-5）；如需"后台运行+回来续看"，应提升为模块级单例并设计重连续流方案
- `composables/sessionEventHandlers` 与后端 `sse_adapter` 的事件字段依赖（camelCase + snake_case 双读兼容）无 schema 契约文档，建议随事件总线迁移补一份事件契约表

## 审查者自检

- [x] 每个 🟡 都有明确的修复方案和验证方式（client 层问题可用 msw/fetch mock 单测；connect 竞态可用"连续 connect + 断言旧流 close"用例；unmount 泄漏可用路由切换 + 断言 es.close）
- [x] 无超大文件需拆分（最大 336 行的 ChatSidebar.vue 未在主审范围，列入下次）
- [x] 发现了跨文件问题（前后端共享字符串协议；token query 的前后端约定）
- [x] 记录了"下次要注意"的债务（ChatSidebar.vue 336 行、components/ 展示层、views/ 管理页）
- [x] 已确认修复后的验证方式（`npm test` + `npm run build` 回归；竞态/泄漏需新增针对用例）

## 附录：本次审查发现的典型模式

- **模式 1：Promise 异步赋值的资源句柄**（`createEventSource().then(es => ref.value = es)`）——任何"先异步获取资源再存 ref"的模式都有"存之前已被清理"的竞态窗口；应同步创建或代际校验。
- **模式 2：组件实例持有的长连接/长状态**——EventSource、定时器等资源的生命周期必须与组件 onUnmounted 严格配对，或提升为模块级单例并显式管理。
- **模式 3：迁移期字符串协议**——前后端各解析一端，测试锁定但无 schema；每一处 `startsWith("tool_calls:")` 都是迁移完成时的改动点。
- **模式 4：同文件两套错误处理标准**（login/register 解析 detail、其余只抛状态码）——API 客户端层的错误格式应单点统一。


---

# 修复与回归验证记录（2026-07-17 同日）

**修复人**：Kimi Code（AI agent）

## 回归结果

- **webui**：`npm test`（10 个断言脚本）全部通过；`npm run build`（vue-tsc strict + vite）通过
- **后端**：`pytest -m "not integration"` → **958 passed**（955 + 新增 3 项 cookie 认证测试）；`ruff check` 涉及文件全绿

## 逐项状态

### 🟡 警告项

| # | 问题 | 状态 | 修复方式 |
|---|------|------|---------|
| 1 | JWT 经 URL query 传递 | ✅ 已根治 | 采用 cookie 方案（免新增 ticket 端点）：① 后端 login（含 no-DB fallback 路径）/refresh 设置 `access_token` httpOnly cookie（SameSite=Strict、path=/api、max_age=900s、Secure 按 scheme 判定），`_resolve_token` 增加 cookie 回退（header > query > cookie），logout 清除；② 前端 `createEventSource` 移除 `?token=` 参数，改 `withCredentials: true`。验证：新增 `TestAccessCookie` 3 项（登录设 cookie、无 header 仅 cookie 可开 SSE 流、登出清 cookie）。附带：`es.onerror` 时在重连前 fire-and-forget `refreshToken()`，缓解 access cookie（15min TTL）先于长流过期导致的重连失败 |
| 2 | 401 刷新无并发去重 | ✅ 已修复 | `refreshAccessToken()` 模块级单例 Promise，并发 401 共享一次刷新——后端存在 refresh token 轮换 + 重用检测（撤销整个 token 族），此竞争原本会导致整族被撤销、用户被强制登出 |
| 3 | 写操作丢失后端 detail | ✅ 已修复 | 新增 `parseErrorDetail(res, fallback)`，login/register/admin/profile/upload/sessions 共 11 处统一接入 |
| 4 | `connect()` EventSource 异步赋值竞态 | ✅ 已修复 | `connectGeneration` 代际计数：`connect()`/`disconnect()` 均递增，Promise 回调中校验代际，过期的 EventSource 立即 `close()` 不挂接 |
| 5 | 路由离开不断开 SSE | ✅ 已修复 | `useAgentSession` 导出 `disconnect`；`HomeView.onUnmounted` 调用之（在 `revokeUploadedFiles` 之前） |
| 6 | streamingMarkdown 3 处 `as any` | ✅ 已修复 | `hasInlineTokens` 改用 `{ tokens?: unknown }` 收窄做类型守卫；守卫通过后直接访问 `lastBlock.tokens`/`lastBlock.raw` |

### 🟢 建议项

| # | 问题 | 状态 | 说明 |
|---|------|------|------|
| 1 | think.ts `observedSinceLastStep` 重复赋值 | ✅ 已删除 | — |
| 2 | on_step 字符串协议 | ⏸️ 维持债务 | 与后端迁移计划绑定，前后端须同步改，见后端审查报告移除清单 |
| 3 | `findMatchingToolIndex` 误匹配边缘 | ✅ 已修复 | 仅 `running`/`pending` 可匹配，已完成工具不再吞并后续同名结果 |
| 4 | 超时不统一 | ✅ 已修复 | 超时下沉 `authFetch`（默认 10s），`uploadFile` 单独 120s；`request()` 简化为委托 |
| 5 | fork/rewind 死 catch + 双错误通道 | ✅ 已删除 | 错误统一走 `session.errorMessage` |
| 6 | `uploading=false` 重复赋值 | ✅ 已删除 | finally 统一负责 |
| 7 | `useAutoScroll` v-if 下监听不挂载 | ✅ 已修复 | `mount()` 在 ref 为空时用一次性 `watch` 等元素出现后挂载 |

## 改动文件清单

- 后端：`courtier/agent/api/middleware/auth.py`（cookie 回退）、`courtier/agent/api/routes/auth.py`（access cookie 设置/清除）、`tests/agent/api/test_routes.py`（+3 测试）
- 前端：`src/api/client.ts`（🟡-1/2/3、🟢-4）、`src/composables/useAgentSession.ts`（🟡-4/5）、`src/views/HomeView.vue`（🟡-5、🟢-5/6）、`src/utils/streamingMarkdown.ts`（🟡-6）、`src/composables/sessionEventHandlers/handlers/think.ts`（🟢-1）、`src/composables/sessionEventHandlers/utils.ts`（🟢-3）、`src/composables/useAutoScroll.ts`（🟢-7）

## 剩余债务（不变）

- on_step 字符串协议（🟢-2，随后端事件总线迁移处理）
- webui 无 ESLint 配置（建议补 `eslint-plugin-vue` 纳入 CI）
- 会话状态的"后台运行 + 重连续看"产品语义未实现（当前为路由离开即断流，重进需从历史恢复——行为已明确且无泄漏）
- 下次建议审查范围：`ChatSidebar.vue`（336 行）、`components/` 展示层、`views/` 管理页


---

# 遗留债务清理记录（2026-07-17 第三批）

## 清理结果总览

| 债务项 | 状态 | 结果 |
|--------|------|------|
| on_step 字符串协议（think/act） | ✅ 已结构化 | 总线层新增 `think.tool_calls` / `think.text_response` 结构化事件；SSE 双轨（保留 legacy `detail` 字符串 + 新增 `toolCalls`/`textResponse`/`tools` 结构化字段）；前端优先消费结构化字段、字符串回退。**过程中实证并修复了一个生产 bug（见下）** |
| webui 无 ESLint | ✅ 已落地 | eslint 9 flat config（`js.recommended` + `typescript-eslint.recommended` + `vue/essential`），`npm run lint` 挂入 `npm test` 门禁；首次扫描发现的 1 个真实错误已修复（`StructuredData.vue` computed 内副作用——重构为纯 computed 派生） |
| "后台运行+重连续看"产品语义 | ⏸️ 不改 | 属产品功能而非债务；当前语义（路由离开即断流、历史记录恢复）已明确且无泄漏 |
| 下轮建议审查范围（ChatSidebar/components/views） | 📌 转下轮 | 属新审查周期范围，非本次债务 |

**最终回归**：后端 `pytest -m "not integration"` **959 passed**（+1 项新回归测试）；ruff 全绿；mypy 涉及文件零错误；webui `npm test`（含新 ESLint 门禁）与 `npm run build` 全绿。

## 重大发现：父级工具卡片在生产中静默丢失（已修复）

清理字符串协议时实证了一个此前两轮审查均未发现的**生产环境 bug**：

- **现象**：模型发起父级工具调用时，前端收到的 think 事件是 `{"detail": "text_response"}` 而非工具名列表 → 前端建空占位步骤 → 后续 `tool_result` 在 `findMatchingToolIndex` 中找不到匹配工具 → **结果被静默丢弃，父级工具卡片从不渲染**。
- **根因**：`loop.py` 的 `waiting_for_tool` 状态转换 reason 是裸字符串 `"tool_calls"`（无工具名），sse_adapter 的 `_handle_think` 只认 `"tool_calls: <names>"` 格式 → 前缀不匹配落入 text_response 分支。字符串协议两端格式漂移，且被订阅者静默吞掉。
- **为何未被发现**：编排器主流程走 Skill/子代理（subagent_run 事件路径不受影响），父级直接工具调用（如内置 `get_artifact`/`list_artifacts`）占比小；`test_event_bus_sse_integration.py` 手工构造的 reason 恰好带名字（`"tool_calls: check_format"`），掩盖了生产值不带名字的事实。
- **实证方式**：用 `agent_loop` + EventBus + SSEAdapter 跑最小 trace，对比修复前后 SSE 事件序列（修复前 `think: text_response`，修复后 `think: tool_calls:echo + toolCalls:["echo"]`）。
- **修复**：见上表第一项。新增回归测试 `test_agent_loop_emits_structured_think_tool_calls` 锁定该链路。

## 改动文件清单

- 后端：`core/events.py`（+2 事件类型）、`core/loop.py`（结构化发布 + act 携带 tools）、`api/sse_adapter.py`（消费结构化事件、`_handle_think_tool_calls` 拆分、SSE 双轨字段）、`tests/agent/api/test_event_bus_sse_integration.py`（更新 + 新增回归测试）
- 前端：`src/types/agent.ts`（+3 可选字段）、`sessionEventHandlers/handlers/think.ts`、`handlers/act.ts`（结构化优先 + 字符串回退）、`src/components/StructuredData.vue`（computed 副作用修复）、`eslint.config.js`（新增）、`package.json`（lint 脚本 + test 门禁）、devDependencies（eslint/@eslint/js/typescript-eslint/eslint-plugin-vue/globals）

## 字符串协议残留状态

- ✅ think/act：总线层已结构化，SSE 双轨，前端优先结构化——协议的结构化通道已通；legacy `detail` 字符串字段保留至所有消费方确认切换后可删
- ⏸️ usage：`think_phase` → `on_step("usage", "p,c")` 的 legacy 桥（loop.py 内部），移除清单见第二批记录
- ⏸️ SSE `detail` 字符串字段：双轨保留期，建议下个版本确认无 legacy 消费方后删除（届时同步清理 think.ts/act.ts 的字符串回退分支）
