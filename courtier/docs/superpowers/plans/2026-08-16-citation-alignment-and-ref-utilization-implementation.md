# 会话引用错位与 $ref 利用率修复 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。Phase 0 是用户可见 bug 的修复，最优先；Phase 间无代码依赖，但 Phase 1 Step 3 依赖 Step 1 先行。

**Goal:** 修复会话 `sess_8146c3be5274` 暴露的四类问题——(a) 并行同名工具调用的引用标记 `[[n]]` 指向错误文档（前端结果↔卡片反向配对）；(b) 子代理调用文本工具时无法直传 `$ref`，被迫多花两轮 LLM 读回；(c) ES 结果索引的 ref id 跨会话互相覆盖；(d) `ArtifactStore.load()` 跨进程读不回 ES 数据，引用表/citations 会静默缺失。

**Architecture:** 改动分四层：主循环事件层（`loop.py`/`loop_phases.py` 为 `think.tool_calls`/`tool.start`/`tool.result` 事件补 `tool_call_id` 与 `citationOffset`）；SSE 适配层（`sse_adapter.py` 透传字段、`ToolInfo` 持久化、并行同名工具的计时键修正）；前端会话层（`webui` 卡片按 id 配对、citations 按绝对编号建索引映射）；结果存储层（`cache_store.py` 文本字段适配与摘要采样、`es_backend.py` 序号防冲突、组合 store 的 `load()` ES 回退）。

**Tech Stack:** Python 3.12+ / uv / pytest；Vue 3 + TS / npm test（`webui/scripts/` 自定义断言脚本）；Elasticsearch 8.x。

**关联文档:** `docs/superpowers/plans/2026-08-16-search-fixes-and-utilization-implementation.md`（引用编号机制 `[[n]]`/`citation_index` 由该计划引入，本计划不动编号算法本身）；`docs/superpowers/specs/2026-05-28-cache-store-ref-params.md`（`$ref` 参数解析契约）。

---

## 背景与现状问题（sess_8146c3be5274 实测证据）

| # | 问题 | 根因与证据位置 | 阶段 |
|---|------|----------------|------|
| 1 | **引用标记指向错误文档**：一轮并行 4 次 `search_documents`（同名），模型侧编号完全正确（调用 1→1~10、2→11~19、4→20~23，5 个 `[[n]]` 逐一核对无误），但 UI 上全部错位 | SSE `tool.start`/`tool.result` 事件只带工具名（`loop.py` 的 `_on_tool_start(tool_name)`/`_on_tool_result(tool_name,...)` 签名即无 id；`sse_adapter.py` 载荷同）；前端 `findMatchingToolIndex`（`sessionEventHandlers/utils.ts:54-81`）在 running 同名卡片里**从最后一个往前**配对 → 调用 1 的结果落到卡片 4，依次反向；`citationsForTurn`（`chatMessages.ts:198`）按 `step.tools` 列表顺序合并 citations → 合并序 = 调用 4,2,1，与模型编号序（1,2,4）不重合。工具卡片的摘要展示同样张冠李戴（第一条查询的卡显示"共 4 条命中"） | 0 |
| 2 | **并行同名工具的时长错乱**：adapter 用工具名做计时键 | `sse_adapter.py:682` `self._tool_start_times = {name: time}`（同名覆盖）与 `:388` `start = self._tool_start_times.get(tool_name, now)` | 0 |
| 3 | **correct_text 收到的是 1490 字内联全文而非 `$ref`**，且前后各多一轮 `get_artifact` 读回（子代理 5 轮中 2 轮是纯读回） | `_load_and_adapt`（`cache_store.py:630-633`）对 string 参数解析出 dict 时 `json.dumps` 整个对象——模型若直传 `$ref:convert_document:1` 会收到 `{"markdown":...,"format":...}` JSON 串，故保守地读回再粘贴 | 1 |
| 4 | **correct_text 结果读回的根因：key_excerpts 采样无益**——摘取了 `results[0].source`/`results[0].target` 各 ~500 字全文开头（两者 99% 相同），模型真正要的纠错对照表（`errors[]` 紧凑字段）被截断丢弃 | `cache_store.py:815` `_sample_field_paths` 采样器：优先取大字符串字段、无去重、无每条预算 | 1 |
| 5 | **ES ref id 跨会话覆盖**：ref 序号是每 store 实例从 1 计数（`cache_store.py:380`），ES 写入直接用裸 ref id 做 `_id`（`es_backend.py:138`）——磁盘文件名有防冲突后缀（`cache_store.py:391-405`）而 ES 没有；实测索引中 `$ref:search_documents:7` 是六天前旧会话残留，本会话的 `:1~:3` 纯属时序运气未被覆盖 | 任何后续会话写自己的 `$ref:tool:1` 即覆盖前一会话文档；会话续跑（跨进程）读到错数据 | 2 |
| 6 | **`load()` 跨进程失效**：组合 store 的同步 `load()` 只走磁盘/ref_map（`store.py:133-140` 委托 `_backend.load` → 磁盘 CacheStore），ES 里的数据读不回（已实测：新进程带 ES 后端加载本会话三个 ref 全返回 None） | `loop.py` 的 `_build_citations_payload`/`_annotate_search_citations` 优先 `load()`，仅当属性不存在才走异步 `read()`——**返回 None 不触发回退**；进程重启后的会话续跑场景引用表与 citations 将整体缺失 | 2 |

**目标形态：** 工具结果按 `tool_call_id` 精确配对到卡片与 citations；`[[n]]` 解析改为后端下发的绝对编号映射（offset 兜底合并序）；文本参数直传 `$ref` 自动提取文本字段；correct_text 类结果的摘要直接呈现紧凑对照表；ES ref 序号全局单调不冲突；`load()` 磁盘未命中回退 ES。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/agent/core/loop.py` | `think.tool_calls` 事件带 ids；`_on_tool_start/_on_tool_result` 带 `tool_call_id`；citations 载荷带 `citationOffset`；引用构建器读回兜底 | 修改 | 0/2 |
| `courtier/agent/core/loop_phases.py` | `execute_tools_phase` 把 `tool_call.id` 传入回调 | 修改 | 0 |
| `courtier/agent/api/sse_adapter.py` | 载荷透传 `toolCallId`/`toolCallIds`/`citationOffset`；`ToolInfo` 增字段并持久化；计时键改 id | 修改 | 0 |
| `courtier/agent/api/models.py`、`courtier/agent/tools/protocol.py` | `ToolInfo.tool_call_id` 字段 | 修改 | 0 |
| `webui/src/types/agent.ts`、`types/chat.ts` | 事件/卡片类型增 `toolCallId`、think 事件增 `toolCallIds` | 修改 | 0 |
| `webui/src/composables/sessionEventHandlers/handlers/think.ts` | 建卡片时记录 `toolCallId` | 修改 | 0 |
| `webui/src/composables/sessionEventHandlers/handlers/toolLifecycle.ts`、`handlers/utils.ts` | start/result 按 id 优先配对 | 修改 | 0 |
| `webui/src/utils/chatMessages.ts` | `citationsForTurn` → 绝对编号索引映射（offset 优先、合并序兜底） | 修改 | 0 |
| `webui/scripts/*.mjs`（新测试脚本） | 并行同名工具配对与引用映射断言 | **新建** | 0 |
| `courtier/agent/core/cache_store.py` | string 参数 dict→文本字段适配；摘要采样去重/预算/紧凑优先 | 修改 | 1 |
| `courtier/prompts/defaults/{zh-CN,en-US}/behavioral.yaml` | $ref 直传与摘要优先指引 | 修改 | 1 |
| `courtier/agent/runtime/es_backend.py` | 写入带 `tool`/`seq` 字段；新增同步 `load()`；序号聚合查询 | 修改 | 2 |
| `courtier/agent/artifacts/store.py` | 组合 store init 播种 ref 计数器；`load()` ES 回退 | 修改 | 2 |
| `tests/agent/test_sse_tool_result_flow.py` | 事件字段透传断言扩展 | 修改 | 0 |
| `tests/agent/test_loop_citations.py` | offset 事件与读回兜底断言 | 修改 | 0/2 |
| `tests/courtier/test_cache_store.py`（既有） | 文本适配与采样器单测 | 修改 | 1 |
| `tests/courtier/test_es_ref_collision.py` | 序号播种与 load 回退（mock ES client） | **新建** | 2 |

> 前端协议兼容：所有新字段可选（`toolCallId`/`toolCallIds`/`citationOffset`），旧事件走名字匹配与合并序兜底；SSE 事件类型与既有字段不变。

---

## Phase 0：tool_call_id 全链路透传 + 引用绝对编号（用户可见 bug 修复）

### Task 0.1: 事件链补 id（后端）

**Files:** `courtier/agent/core/loop.py`、`courtier/agent/core/loop_phases.py`

- [x] **Step 1:** `_run_think_phase` 的 `think.tool_calls` 事件载荷增 `"ids": [tc.id for tc in current_state.tool_calls]`（与 `names` 同序）。
- [x] **Step 2:** 回调签名升级（均带默认值保持兼容）：
  - `_on_tool_start(tool_name, tool_call_id=None)` → 发布 `tool.start {"name", "tool_call_id"}`；
  - `_on_tool_result(tool_name, result, summary, tool_call_id=None)` → 发布 `tool.result {..., "tool_call_id"}`。
- [x] **Step 3:** `execute_tools_phase`（`loop_phases.py:247` 循环体内）把当前 `tool_call.id` 传入两处回调调用（`on_tool_start`/`on_tool_result` 各加一个参数透传；`registry.execute` 的 `on_tool_start` 回调在 `loop_phases` 侧闭包捕获 id，registry 内部签名不动）。
- [x] **Step 4:** 单测：stub 模型响应带 2 个同名 tool_calls，捕获 EventBus 事件，断言 `tool.start`/`tool.result` 各携带正确的 `tool_call_id` 且顺序对应。

### Task 0.2: SSE 适配层透传 + 计时键修正 + ToolInfo 持久化

**Files:** `courtier/agent/api/sse_adapter.py`、`courtier/agent/api/models.py`、`courtier/agent/tools/protocol.py`、`tests/agent/test_sse_tool_result_flow.py`

- [x] **Step 1:** `on_tool_start(self, tool_name, tool_call_id=None)` / `on_tool_result(self, tool_name, result, summary, tool_call_id=None)`；SSE 载荷增 `"toolCallId"`（None 时省略）。
- [x] **Step 2:** `_handle_tool_calls`（think 事件处理，`sse_adapter.py:670`）：读取事件 `ids`，SSE `think` 载荷增 `"toolCallIds": ids`（与 `toolCalls` 同序）；`_tool_start_times` 改键为 `tool_call_id or name`（修复并行同名时长覆盖：4 张卡各自计时）。
- [x] **Step 3:** `ToolInfo`（`models.py:141` 与 `protocol.py:31` 两处定义同步）增 `tool_call_id: str | None = None`；adapter 构造 `ToolInfo` 时带上 → 会话存储的历史回放同样获得精确配对。
- [x] **Step 4:** 扩展 `test_sse_tool_result_flow.py`：两个同名工具的 start/result 交错，断言 SSE 载荷 `toolCallId` 正确、duration 各自独立、`ToolInfo.tool_call_id` 持久化。

### Task 0.3: citations 绝对编号（citationOffset）

**Files:** `courtier/agent/core/loop.py`、`tests/agent/test_loop_citations.py`

- [x] **Step 1:** `agent_loop` 内 `_on_tool_result` 闭包维护运行计数 `citation_offset_emitted`（与 `_annotate_search_citations` 的 `citation_offset` 同数学：每次成功的 `search_documents` 结果加 `min(len(hits), _CITATION_MAX_HITS)`，两者在同一执行序下恒等）。`tool.result` 事件载荷增 `"citation_offset": offset`，随 citations 一起发出。
- [x] **Step 2:** `_build_citations_payload` 不变（每调用相对列表）；绝对化由前端用 offset 完成。单测：两次搜索结果事件，断言第二次的 `citation_offset` = 第一次的命中数。

### Task 0.4: 前端按 id 配对 + 绝对编号映射

**Files:** `webui/src/types/agent.ts`、`types/chat.ts`、`handlers/think.ts`、`handlers/toolLifecycle.ts`、`handlers/utils.ts`、`utils/chatMessages.ts`、新 `webui/scripts/test-citation-mapping.mjs`

- [x] **Step 1:** 类型：`AgentEvent` 复用既有 `toolCallId?` 字段（`types/agent.ts:143`）承接 `tool_start`/`tool_result`；think 事件增 `toolCallIds?: string[]`；`ToolResult` 卡片类型增 `toolCallId?: string | null`。
- [x] **Step 2:** `handleThinkEvent`：建卡片时 `toolCallId: event.toolCallIds?.[i] ?? null`。
- [x] **Step 3:** `handleToolStartEvent` 与 `findMatchingToolIndex` 匹配规则改为：**优先 `toolCallId` 精确相等且状态 pending/running**；无 id（旧事件）回退现行为（名字 + 状态，从后往前）。
- [x] **Step 4:** `chatMessages.ts`：`citationsForTurn` 改为构建绝对编号映射——遍历本 turn 的 search 工具卡，若结果事件带 offset 则 `map[offset + i + 1] = hit`，否则按合并序顺序分配 `mergedIndex`；返回 `CitationIndex = { byNumber: Map<number, CitationHit>, list: CitationHit[] }`。`AssistantMessage.onCitation(n)` 解析顺序：`byNumber.get(n)` → `list[n-1]` → undefined（保留 console.warn）。
- [x] **Step 5:** 新建 `webui/scripts/test-citation-mapping.mjs`（仿既有脚本风格，`npm test` 挂载）：用本会话的真实事件序回放（4 个同名 search：结果按 1→4 到达、卡片序 1→4），断言 (a) 每张卡的 summary/citations 与查询一一对应；(b) `[[11]]`/`[[13]]`/`[[21]]` 解析到调用 2/2/4 的正确命中；(c) 无 id 的旧事件走合并序兜底仍一致。

**验收（Phase 0 整体）:** 后端 `uv run pytest tests/agent -q` 全绿；前端 `npm test` 全绿；手动/集成场景——同一轮并行 3 个同名 `search_documents`，UI 卡片摘要与查询一一对应，`[[n]]` 点击打开的文档/条款与编号表一致。

---

## Phase 1：$ref 直传文本参数 + 摘要采样修复

### Task 1.1: string 参数的 dict→文本字段适配

**Files:** `courtier/agent/core/cache_store.py`、`tests/courtier/test_cache_store.py`

- [x] **Step 1:** `_load_and_adapt` 的 `schema_type == "string"` 分支：数据为 dict 时，按优先序取第一个**值为字符串**的文本字段：`("markdown", "text", "content", "plain_text", "chunk_text", "data")`；取到且非空 → 返回该字符串；无命中 → 维持 `json.dumps` 兜底（对象型工具入参仍需整体 JSON）。取到 dict/list 值的字段跳过。
- [x] **Step 2:** 单测：persist `{markdown, format}` → 以 `$ref` 传入 string 参数 → 收到 markdown 原文而非 JSON 串；`{chunk_text, title}` → chunk_text；无可识别字段 → JSON 兜底不变。

### Task 1.2: 摘要采样器去重与预算

**Files:** `courtier/agent/core/cache_store.py`（`_sample_field_paths` 及摘录生成处）、`tests/courtier/test_cache_store.py`

- [x] **Step 1:** 采样规则改为两段式：先收集**紧凑标量叶**（值 ≤200 字符，含 `errors[]` 类对照字段，值完整保留）；剩余预算再填充长字符串字段（每条截 300 字符）。
- [x] **Step 2:** 近重复去重：候选摘录与已选摘录做前 80 字符前缀比对（或 `difflib.SequenceMatcher.ratio > 0.9`），重复的跳过——`results[0].source` 与 `results[0].target` 只保留一条，腾出预算给 `errors[]`。
- [x] **Step 3:** 单测用本会话 correct_text 真实结构：断言摘录包含 ≥3 条 `errors[*]` 字段（operation/context 等完整值），`source`/`target` 至多一条且截断。
- [x] **Step 4:** 回归：既有 cache_store 采样测试不回退（`uv run pytest tests/agent/test_cache_store.py tests/courtier/test_cache_store.py -q`）。

### Task 1.3: 行为指引更新

**Files:** `courtier/prompts/defaults/zh-CN/behavioral.yaml`、`en-US/behavioral.yaml`

- [x] **Step 1:** `$ref` 规则区（zh-CN `behavioral.yaml:21-25` 附近）增补两条（en-US 镜像）："文本类参数（如纠错/审阅的 text）可直接传 `$ref`，系统自动解析为其中的文本字段，无需先 get_artifact 读回再粘贴"；"查看工具结果优先读返回中的 summary/key_excerpts，仅当需要逐字引用全文时才 get_artifact"。
- [x] **Step 2:** 防漂移：`tests/courtier/test_prompt_single_source.py` 增关键词断言（"自动解析"、"key_excerpts"）。

**验收（Phase 1 整体）:** 重放本会话场景（子代理 content_audit）：convert_document → correct_text 直传 `$ref`（无中间 get_artifact 轮）；correct_text 结果的 key_excerpts 呈现纠错对照，无二次 get_artifact 读回；子代理轮数从 5 降至 3。

---

## Phase 2：ES ref 防冲突 + load() 跨进程回退

### Task 2.1: ref 序号全局播种

**Files:** `courtier/agent/runtime/es_backend.py`、`courtier/agent/core/cache_store.py`、`tests/courtier/test_es_ref_collision.py`

- [x] **Step 1:** `ElasticsearchResultBackend.store()` 写入体增 `"tool"` 与 `"seq"`（由 `_REF_PATTERN` 从 ref_id 解析，`$ref:{tool}:{seq}[:label]` 取无 label 段）。
- [x] **Step 2:** `CacheStore` 新增 `seed_ref_counters(mapping: dict[str, int])`（仅上调，不降现有计数；与 `cache_store.py:587` 的既有上调逻辑同族）。
- [x] **Step 3:** 组合 store（`artifacts/store.py` 中包裹磁盘 + ES 的复合类）init 时聚合查询播种：terms agg on `tool`（size 100）+ sub-agg `max(seq)` → `seed_ref_counters`；结果做**模块级 TTL 缓存**（60s，键 = 索引名，值取 max(缓存, 查询)）避免每请求 build_agent 都打 ES。ES 不可达时 warning 并跳过（磁盘文件本就有防冲突后缀，功能不降级）。
- [x] **Step 4:** 单测（mock ES client）：init 后首个 ref 为 `max_seq+1`；两实例先后 init 不复用序号；缓存 TTL 内二次 init 不再查询；label 后缀解析不受影响。

### Task 2.2: load() 磁盘未命中回退 ES + loop 读回兜底

**Files:** `courtier/agent/runtime/es_backend.py`、`courtier/agent/artifacts/store.py`、`courtier/agent/core/loop.py`、`tests/courtier/test_es_ref_collision.py`、`tests/agent/test_loop_citations.py`

- [x] **Step 1:** `ElasticsearchResultBackend` 增同步 `load(result_id) -> Any | None`（直接用同步 `self._client.get`——`load()` 本就是阻塞语义；返回 `_source["data"]`，404/异常返回 None）。
- [x] **Step 2:** 组合 store 的 `load()`：磁盘/ref_map 未命中且配置了 ES 主后端 → 调后端同步 `load` 返回。
- [x] **Step 3:** `loop.py` 两处引用构建器加兜底（纵深防御，覆盖任何后端形态）：`loader(...)` 返回非 dict 时改走 `await artifact_store.read(result.result_id)`，取 `loaded.get("data")`（`_get` 返回形状已确认，`es_backend.py:166-175`）。`_build_citations_payload` 已有 read 分支——改为"load 返回 None 也尝试 read"；`_annotate_search_citations` 同样补 read 兜底。
- [x] **Step 4:** 单测：(a) mock 后端 load=None、read 返回数据 → 两构建器均产出 citations/引用表；(b) 集成标记测试：写入 ES → **新进程**组合 store `load()` 直接读回（本会话三 ref 场景复现）。

**验收（Phase 2 整体）:** 重启进程后续跑会话，历史搜索的 citations 与引用表不缺失；并发两会话各自 search 持久化后，ES 中两个 ref 文档并存（`_id` 不同）。

---

## 提交切分（约定式，逐 Task 一提交）

```
fix(loop): thread tool_call_id through think/tool_start/tool_result events   # 0.1
feat(sse): toolCallId/citationOffset passthrough + per-call duration keying  # 0.2+0.3
fix(webui): match tool results by tool_call_id; absolute citation numbering  # 0.4
fix(cache): extract text field when adapting $ref objects to string params   # 1.1
fix(cache): dedupe and budget result summary excerpts                        # 1.2
docs(prompts): pass $ref directly for text params; prefer excerpts           # 1.3
fix(artifacts): globally seeded ref sequence for ES result ids               # 2.1
fix(artifacts): load() falls back to ES; citation builders retry via read()  # 2.2
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；`cd webui && npm test` 全绿（含新 citation-mapping 脚本）。
2. 场景回放 A（引用错位）：并行 3+ 同名搜索 → 卡片摘要、`[[n]]` 点击目标、citations 编号三者一致（对照 Phase 0 前的错位行为）。
3. 场景回放 B（$ref 利用率）：content_audit 子代理轮数 5→3，correct_text 入参为解析后的文本。
4. 场景回放 C（存储健壮性）：跨进程 load 回读成功；两会话并发写 ref 无覆盖。
5. Langfuse/日志核验：`tool.result` 事件带 `tool_call_id`；无 `citation out of range` 前端告警。

## 明确不做的事

- 不改 `[[n]]` 编号算法与 `citation_index` 注入（上轮计划已固化，本次只修配对与映射）。
- 不改 `findMatchingToolIndex` 之外的前端渲染结构（steps/verdict 布局不动）。
- 不引入事件 schema 版本号——新字段全部可选、旧事件走兜底，兼容由字段缺失性保证。
- 不做 correct_text 插件侧改动（入参契约不变；改善全部落在通用解析/摘要层）。
- ref id 格式保持 `$ref:{tool}:{seq}[:label]` 不变（模型上下文中已有该格式的引用）。

---

## 实施记录（2026-08-16）

全部 10 个 Task 完成并逐项提交。与计划的偏差与实施中发现的问题：

1. **Task 0.1 回调兼容**：初版给 `_on_tool_start`/`_on_tool_result` 直接追加参数，
   旧的单参显示回调会被 `_safe_call` 静默吞掉 TypeError。改为签名容量探测
   （`_positional_capacity`），有容量才传 id。
2. **Task 0.2 校准**：`tools/protocol.py` 的 `ToolInfo` 是工具注册元数据，与展示无关，
   只有 `api/models.py` 的 `ToolInfo` 需要 `tool_call_id`。think 事件的 `toolCallIds`
   只在 EventBus 路径（生产 `start_listening` 接线）透传，旧 `on_step` 字符串协议
   保持 names-only。
3. **Task 1.2 落点校准**：摘要采样器在 `runtime/summarizer.py`（非 cache_store）。
   改造策略：紧凑标量（≤500 字）优先、大 blob 截 300 字、前 160 字符近重复去重——
   correct_text 的 `errors[]` 对照字段因此进入摘录，source/target 至多占一席。
4. **Task 2.1 测试暴露的真实缝隙**：TTL 缓存内，sibling store 写入后另一 store
   仍从旧快照播种——修法为进程级共享计数底数（`_SHARED_REF_COUNTERS`，每次
   persist 上调；`max_ref_sequences` 命中 TTL 前以之为底）+ ES 写入
   `op_type=create`（冲突显式失败而非静默覆盖）。
5. **测试影响面**：共享计数器引入跨测试泄漏，6 个测试套件补了 autouse 重置
   fixture；`test_registry.py:273` 的预期按新文本适配语义更新
   （`{text: "x"*5000}` 现在解析为文本本身）。
6. **回归**：后端 1538 passed / 0 failed；前端 11 个 npm 脚本 + build（含类型检查）全绿。
