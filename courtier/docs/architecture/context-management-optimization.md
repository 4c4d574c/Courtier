# 上下文管理机制：缺陷分析与优化方案

> 本文档针对 `courtier/agent/core/context_manager.py` 三层上下文预算机制（大结果落盘 → 微压缩 → 全量压缩）的现存缺陷给出分阶段改造方案。范围覆盖 `courtier/` 后端与 `webui/` 前端。
>
> **两项全局决策**（已确认）：
> 1. 方案中所有增强项（含原标记 TODO/可选的项）均为**必做**，无范围裁剪。
> 2. **不考虑向后兼容，全量迁移**：不保留旧行为开关、旧数据格式兜底与死代码；发现即替换。旧版本保存的会话不保证可恢复。
>
> 现状机制说明见文末附录 A；本文档重点是问题与方案。

---

## 实施状态（滚动更新）

| 里程碑 | 状态 | 关键改动 | 验收 |
|---|---|---|---|
| M1（P1）正确性修复 | ✅ 已落地（2026-07-30） | `_find_last_real_user`/`_align_tool_boundaries`（`core/context_manager.py`）；`snapshot_state`/`load_state` + 会话链路（`api/models.py`、`api/session_store.py`、`api/routes/sessions.py`、`api/services/stream_service.py`）；`fork()` + `runtime/runtime.py` delegate 分支与 `cache_dir` 字段；删除 `rehydrate_artifact_store`（含 `services/__init__.py` 导出与 `tests/agent/test_routes_rehydrate.py`） | `tests/agent/test_context_manager.py` 新增 6 个测试类（17 用例）；`tests/agent/artifacts/test_store_snapshot.py` 更新；全量 1093 passed；ruff clean |
| M2（P2）预算 token 化 | ✅ 已落地（2026-07-30） | 7 个新配置项（`config.py`）；`estimate_tokens` 接入预算判断，`_estimate_chars` 删除；`update_actual_usage` 真实 token 校准（`loop_phases.py`）；微压缩预算门控 + 按 token 保留（`MIN_RECENT_TOOL_RESULTS=2` 下限）；压缩后摘要截断 + `last_compact_over_budget` 告警（SSE compact 事件带"压缩后仍超预算"）；预览长度可配置（默认 1000，`cache_store`/`artifacts/store`/`agent_service`/`app` 贯通） | 新增 11 用例；全量 1104 passed；ruff clean |
| M3（P3）部分压缩与增量摘要 | ✅ 已落地（2026-07-30） | `_full_compact` 改为分段压缩（`_find_last_real_user_index` 定 boundary，当前轮原样保留，无可压缩历史时跳过）；当前轮爆炸时 force micro-compact 兜底；`_build_summary` 按角色结构化提取（`_summarize_tool_message`）；增量摘要（`last_summary` + 【既有摘要】/【新增对话段】合并 prompt）；摘要 prompt 增加 ref_id/文件路径清单要求；`force_compact` 走 `force_all=True` 压缩全部 | 新增 8 用例；全量 1115 passed；ruff clean |
| M4（P4）工程清理与能力补齐 | ✅ 已落地（2026-07-30） | 删除 `persist_large_output`/`force_persist`（plugin SDK 同名类不受影响）；主链路统一切换 `MemoryManager`（builders 接受 `session_id`）；`POST /api/sessions/{id}/compact` + ChatHeader"压缩上下文"按钮（复用 compacted 提示渲染）；压缩 prompt 外移 `domains/docaudit/config/prompts/{locale}/context.yaml`（`context.compact_prompt`/`context.compact_merge_prompt` 注册为 reserved keys，`{history}` 占位 + `---` 分隔用户指令，core 保留英文协议兜底）；微压缩 note 中文化；缓存哈希去重（`tool_name:sha256` 索引落盘 `.hash_index.json`，跨请求生效、失效自愈）+ 会话删除 GC（扫描其余会话 snapshot，仅删无引用文件，路径穿越防护）+ `scripts/clean_agent_cache.py` 手动清理；4 个 Prometheus 指标（`context_tokens` gauge、`context_compaction_total`、`context_compaction_duration_seconds`、`context_ref_recover_total`）；大用户输入治理（`context_max_user_message_chars` 超限时路由层 persist + 预览替换，用户侧展示不受影响） | 新增/更新 20+ 用例；全量 1135 passed；ruff clean；前端 `npm run build` + `npm test` 通过 |

> **M2 遗留说明**：P2.3 降级链第 2 步"PromptPipeline 段级预算裁剪 system prompt 动态段"未实施。原因：system prompt 在一次 run 内固定（`messages[0]`），运行中裁剪需要跨层重建 prompt，收益有限；当前以摘要截断 + `last_compact_over_budget` 告警覆盖该场景。如后续出现 system prompt 主导超预算的实际案例，再单独立项。

验收命令：

```bash
cd /home/lmwl/Documents/docaudit/agent/courtier
uv run pytest -m "not integration" -q
uv run ruff check courtier/ tests/ scripts/
```

---

## 0. 问题清单总览

| # | 问题 | 级别 | 证据 | 修复阶段 |
|---|------|------|------|----------|
| 1 | 全量压缩把 reminder 当作"最近用户消息"保留，真实任务仅靠摘要幸存 | 缺陷 | `context_manager.py:354-361`；reminder 每轮 append 到末尾（`loop.py:201-203`），compact 在其后执行（`loop_phases.py:60`）；`agents/echo.py:74` 已有排除 reminder 的先例 | P1 |
| 2 | 摘要失败回退路径 `messages[-8:]` 任意切片，可产生孤儿 tool 消息/悬空 tool_calls，导致 API 400 | 缺陷 | `context_manager.py:306-318` | P1 |
| 3 | `CompactState` 按请求重建不持久化：防死循环守卫跨请求失效（恢复会话每轮重复摘要），压缩编号每请求重排 | 缺陷 | `agent_service.py:184/275`；守卫在 `context_manager.py:270` | P1 |
| 4 | 轮中压缩无"当前轮保护"，本轮刚拿到的工具结果也被摘要掉 | 缺陷 | `loop_phases.py:57-69` 无边界概念 | P3 |
| 5 | 子代理共享父 `ContextManager`，`CompactState` 跨 agent 串扰；fallback 分支硬编码相对路径 `.agent_cache` | 缺陷 | `runtime/runtime.py:259-265` | P1 |
| 6 | 微压缩 `_omitted` 占位符缺文件路径；恢复依赖 marker 扫描兜底（`stream_service.py:163`），与 snapshot 恢复双轨并行 | 缺陷 | `context_manager.py:169-176`；`stream_service.py:198-200` | P1 |
| 7 | 预算用字符数不用 token，不感知模型上下文窗口；`estimate_tokens` 写好但无调用点 | 策略 | `context_manager.py:373-382/384`；`config.py:146` 仅有生成上限 | P2 |
| 8 | 微压缩无条件每轮执行，与预算脱钩；固定保留 5 条一刀切 | 策略 | `loop.py:657-658`；`context_manager.py:28` | P2 |
| 9 | Layer 1 预览仅 200 字符，模型几乎必然多花一轮 `get_artifact` | 策略 | `cache_store.py:30` | P2 |
| 10 | 压缩输入高度有损（非 system 消息截 500 字符）；每次全量重新摘要，无增量机制 | 策略 | `context_manager.py:473-491` | P3 |
| 11 | 压缩后无大小校验，`MAX_CONTEXT_CHARS_AFTER_COMPACT` 定义未使用；system prompt 过大时永远超预算运行且无告警 | 策略 | `context_manager.py:29` | P2 |
| 12 | 死代码/未接线：`persist_large_output`/`force_persist`/`force_compact`/`_estimate_chars`；主链路用裸 `ContextManager`，`MemoryManager` 仅在 runtime fallback 使用 | 卫生 | 全项目 grep 无生产调用点 | P4 |
| 13 | `.agent_cache` 无 GC：会话删除不清理；重复解析产生重复缓存（ref_id 计数器式，无去重） | 卫生 | `cache_store.py:260` | P4 |
| 14 | 压缩 prompt 硬编码中文于 core，违反"core 零硬编码 NL 文本"约定；微压缩 note 硬编码英文 | 卫生 | `context_manager.py:283-292/165-168` | P4 |
| 15 | 可观测性薄：无上下文大小/压缩频率/占位符恢复率指标 | 卫生 | 仅有 logger | P4 |
| 16 | 大用户输入无治理，只能靠 Layer 3 事后补救 | 策略 | Layer 1 仅覆盖 tool 结果 | P4 |

---

## 1. 设计目标与原则

1. **先正确，后策略**：P1 修复正确性缺陷，P2 调整预算策略，P3 做结构性改进，P4 清理。每个 Phase 独立交付、独立测试。
2. **全量迁移，不留旧路径**：不保留行为开关、旧格式兜底、兼容 shim。旧机制就地替换，旧版本会话不保证恢复。
3. **所有增强项必做**：包括按 token 保留工具结果、增量摘要、真实 token 校准、哈希去重、大用户输入治理、`force_compact` API 化。
4. **core 零硬编码 NL 文本**：新增的面向模型的自然语言一律入 PromptBundle（`domains/<domain>/config/prompts/{locale}/`）。
5. **预算 token 化且模型感知**：所有预算判断统一走 token 估算 + 可配置的模型窗口，并以 LLM 响应的真实 `prompt_tokens` 校准。
6. **最小侵入**：不改变 Think→Act→Observe 主循环结构，不改变 ArtifactStore 的 persist/resolve 契约。

---

## 2. P1 — 正确性修复

### 2.1 全量压缩跳过 reminder（问题 1）

**改动点**：`context_manager.py` `_full_compact` 查找最近用户消息处。

新增 `_find_last_real_user(messages)` helper：倒序查找 `role == "user"` 且 `content` 非空且 `source != "reminder"` 的消息。P3 的保护边界复用此 helper。

### 2.2 回退切片对齐 tool-call 边界（问题 2）

**改动点**：`context_manager.py` `_full_compact` 的 fallback 分支。

新增 `_align_tool_boundaries(messages: list[Message]) -> list[Message]`：

1. 从头部丢弃连续的 `role="tool"` 孤儿消息（其配对的 assistant 不在切片内）。
2. 遍历切片内每个带 `tool_calls` 的 assistant 消息：若其任一 `tool_call_id` 在后续消息中无对应 `role="tool"` 消息，则移除该 assistant 的 `tool_calls`（降级为纯文本 assistant，保留 reasoning 内容），避免悬空调用。
3. 对齐后再执行现有的 4000 字符截断逻辑。

### 2.3 CompactState 持久化（问题 3）

**接口**：ContextManager 新增：

```python
def snapshot_state(self) -> dict: ...
def load_state(self, state: dict | None) -> None: ...
```

序列化 `{version, has_compacted, last_summary, compact_count}`。

**接线**：
- 保存：会话持久化链路（`session_store.py` / `stream_service.py`，与 artifact snapshot 同一保存点）写入 `context_state` 字段，每次保存必写。
- 恢复：构建 ContextManager 后调用 `load_state(session.get("context_state"))`；字段缺失时按空状态处理（仅此一处宽容，属缺省初始化而非兼容层）。

**效果**：防死循环守卫跨请求生效；`[上下文压缩 #N]` 编号连续；P3 增量摘要所需的 `last_summary` 跨请求可用。

### 2.4 子代理独立 CompactState（问题 5）

**改动点**：`runtime/runtime.py` `spawn` 内。

- ContextManager 新增 `fork() -> ContextManager`：返回共享底层 `_cache`（ArtifactStore）与 `_model`、但持有全新 `CompactState` 的实例。
- `runtime.py:259-260` 改为 `cm = context_manager.fork()`。
- fallback 分支（`runtime.py:262-265`）的硬编码 `.agent_cache` 改为使用 runtime 构造时持有的 `cache_dir`（runtime 增加 `cache_dir` 字段；同时该分支随 P4.1 统一切换为 MemoryManager）。

### 2.5 恢复路径统一为 snapshot，删除 marker 扫描兜底（问题 6）

全量迁移决策下，恢复路径只保留一条：

- **删除** `stream_service.py` `rehydrate_artifact_store` 的 marker 扫描逻辑：store 恢复一律走会话 JSON 中的 artifact snapshot（`load_snapshot`）。无 snapshot 的会话 = 旧版本会话，不保证恢复（接受）。
- `micro_compact` 占位符仍补 `"file"` 字段，但仅作调试信息，不再作为恢复依据。

**测试**（P1 全部）：`tests/agent/core/test_context_manager.py` 新增/更新用例：

- reminder 排除：压缩后保留的是真实任务而非 reminder；
- 边界对齐：构造孤儿 tool / 悬空 tool_calls 切片，断言对齐后配对完整；
- CompactState snapshot/load 往返；
- fork 后父子 CompactState 互不影响；
- 恢复路径：snapshot 存在时完整恢复；rehydrate 兜底代码已删除（grep 断言或删除对应旧测试）。

---

## 3. P2 — 预算策略 token 化与门控

### 3.1 token 化预算 + 模型窗口配置 + 真实 token 校准（问题 7）

**新增配置**（`config.py`，均可被 env 覆盖）：

| 配置 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `llm_context_window_tokens` | `LLM_CONTEXT_WINDOW_TOKENS` | `32768` | 部署模型的上下文窗口 |
| `context_budget_ratio` | `CONTEXT_BUDGET_RATIO` | `0.75` | 触发全量压缩的窗口占比 |
| `context_micro_compact_ratio` | `CONTEXT_MICRO_COMPACT_RATIO` | `0.60` | 启用微压缩的窗口占比 |
| `context_compact_target_ratio` | `CONTEXT_COMPACT_TARGET_RATIO` | `0.50` | 压缩后的目标占比 |
| `context_recent_tool_results_tokens` | `CONTEXT_RECENT_TOOL_RESULTS_TOKENS` | `4000` | 微压缩保留最近工具结果的 token 预算（见 3.2） |
| `context_preview_max_chars` | `CONTEXT_PREVIEW_MAX_CHARS` | `1000` | Layer 1 预览长度（见 3.4） |
| `context_max_user_message_chars` | `CONTEXT_MAX_USER_MESSAGE_CHARS` | `10000` | 大用户输入治理阈值（见 P4.5） |

**改动点**：

- `ContextManager.__init__` 接受 `max_context_tokens` / `micro_compact_tokens` / `compact_target_tokens` / `recent_tool_results_tokens`，`agent_service.py` 从 settings 计算传入。
- 预算判断全部切换到 `estimate_tokens`（既有 CJK 感知启发式，原地启用）；**删除 `_estimate_chars`**。
- **真实 token 校准（必做）**：think 阶段 LLM 响应已统计 `prompt_tokens`（`loop.py:156` 现有变量）。`think_phase` 每轮响应后调用 `context_manager.update_actual_usage(prompt_tokens)`；`compact_if_needed` 的判断值取 `max(estimate_tokens(messages), last_actual_prompt_tokens)`——真实值修正启发式偏差，取大者保证不晚触发。

### 3.2 微压缩：预算门控 + 按 token 保留（问题 8，含原 TODO 增强）

**门控**：`micro_compact` 入口：

```python
if self.estimate_tokens(messages) < self._micro_compact_tokens:
    return messages  # 预算充裕，不做任何替换
```

**按 token 保留（替代固定 5 条）**：对超预算的消息序列，从最新 tool 消息倒序累积，保留预算内的最近结果，并设下限防止全灭：

```python
kept: list[int] = []
budget = self._recent_tool_results_tokens
for i in reversed(tool_indices):
    cost = self.estimate_tokens((messages[i],))
    if len(kept) >= 2 and cost > budget:
        break  # 已保下限且预算耗尽
    kept.append(i)
    budget -= cost
```

下限为最近 2 条（硬编码常量 `MIN_RECENT_TOOL_RESULTS = 2`），其余由 `context_recent_tool_results_tokens` 控制。原 `RECENT_TOOL_RESULTS` 常量与构造参数删除。

### 3.3 压缩后大小校验与降级链（问题 11）

**改动点**：`_full_compact` 尾部增加校验，压缩目标为 `compact_target_tokens`（替代原未使用的 `MAX_CONTEXT_CHARS_AFTER_COMPACT`，该常量删除）：

1. 压缩后 `estimate_tokens` > target：截断摘要本身（保留头部，摘要过长说明模型未遵守"紧凑"要求）。
2. 仍超预算：裁剪 system prompt 动态段（memory/courtier_md/environment 由 PromptPipeline 分段，pipeline 增加段级预算参数，下轮重建 prompt 时生效）。
3. 仍超预算：记 `logger.error`，并通过 `on_step("compact", "压缩后仍超预算")` 通知前端（复用现有 `context_compacted` SSE 通道，detail 中标注异常）。

### 3.4 Layer 1 预览长度提升（问题 9）

**改动点**：`cache_store.py` `PREVIEW_MAX_CHARS` 从 200 提升到可配置的 `context_preview_max_chars`（默认 1000）。结构化数据（dict/list）的 shape 摘要（`_sample_field_paths`）保持不变；文本类预览给足 1000 字符。

**测试**（P2 全部）：

- token 预算触发阈值（构造恰好越界/临界的消息序列）；
- 真实 token 校准：mock 响应携带 usage，断言判断值取 max；
- 微压缩门控：低预算时 10 条 tool 消息原样保留；
- 按 token 保留：大小混合的 tool 结果，断言保留集合符合预算与下限；
- 压缩后校验：mock 模型返回超长摘要，断言截断与降级事件；
- 配置注入：settings → ContextManager 参数传递正确。

---

## 4. P3 — 部分压缩与增量摘要

### 4.1 当前轮保护边界（问题 4）

**核心改动**：`_full_compact` 从"全量替换"改为"分段压缩"（唯一行为，无回归开关）：

1. 用 `_find_last_real_user()` 定位最后一条真实用户消息的索引 `boundary`。
2. `boundary` 之后的消息（当前轮次的 assistant/tool 交互）**全部原样保留**。
3. 仅压缩 `boundary` 之前的历史段：保留 `messages[0]`（system prompt），中间段送摘要。
4. 输出结构：`[system_prompt, 摘要消息, ...messages[boundary:]]`。
5. 兜底：若 `boundary` 之后的消息自身已超预算（当前轮工具结果爆炸），先依赖 P2 门控后的 Layer 1/2；仍超时才对当前轮追加 micro-compact（允许动当前轮的旧 tool 消息，但仍按 3.2 的 token 预算保留最近结果）。

**防死循环守卫同步更新**：改为"已压缩过 且 boundary 之前除 system prompt 与旧摘要外无其他消息"时跳过压缩。

### 4.2 结构化摘要输入（问题 10 前半）

**改动点**：`_build_summary` 重写为按角色差异化提取：

| 角色 | 提取策略 |
|------|----------|
| system（首条） | 前 2000 字符（身份/工具清单） |
| system（压缩摘要） | 全量保留（上次摘要必须完整进入下一轮摘要） |
| user（非 reminder） | 全量（通常较短） |
| user（reminder） | 丢弃 |
| assistant | content 前 1000 字符 + 工具调用名列表 |
| tool | 工具名 + 成功/失败 + `ref_id`（若有）+ raw_data 的前 5 个标量字段（通用策略，core 不感知业务字段）+ 文本前 300 字符 |

压缩 prompt 同步要求模型输出中**必须保留 ref_id 清单与文件路径清单**两个小节（prompt 文本入 PromptBundle，见 P4.2）。

### 4.3 增量摘要（问题 10 后半，必做）

利用 P1.3 持久化的 `CompactState.last_summary`，避免每次对全量历史重新摘要：

- 部分压缩后的消息结构固定为 `[system, 摘要消息, tail...]`，因此**无需额外标记**即可推导增量段：若 `state.has_compacted` 且 `messages[1]` 是摘要消息，则"新增段"= `messages[2:boundary]`。
- 摘要输入 = 旧摘要全文 + 新增段的结构化提取（4.2 格式）；摘要 prompt 改为"将新增对话段与既有摘要合并，输出更新后的完整摘要"（入 PromptBundle，与单次压缩模板并列）。
- 首次压缩（无旧摘要）退化为 4.2 的全量摘要输入。
- `last_summary` 同步更新为合并后的新摘要。

**测试**（P3 全部）：

- 部分压缩：boundary 后消息逐条恒等保留，boundary 前被摘要替换；
- 当前轮爆炸兜底：构造当前轮超大工具结果，断言走加强 micro-compact；
- 结构化摘要输入：各角色消息按上表格式提取；
- 增量摘要：第二次压缩时摘要输入仅含旧摘要 + 新增段，且 `last_summary` 正确轮换。

---

## 5. P4 — 工程清理与能力补齐

### 5.1 死代码处置与统一切换（问题 12，全部必做）

| 对象 | 处置 |
|------|------|
| `_estimate_chars` | P2 切换后删除 |
| `MAX_CONTEXT_CHARS_AFTER_COMPACT` | P2.3 以 `compact_target_tokens` 替代，常量删除 |
| `RECENT_TOOL_RESULTS` | P2.2 以 token 预算替代，常量删除 |
| `persist_large_output` / `force_persist` | 删除方法，docstring 用法示例同步更新（生产路径在 `tools/registry.py:374`） |
| `force_compact` | 接 API：**`POST /api/sessions/{id}/compact`** —— 同步执行：加载会话消息 → `force_compact` → 保存会话 → 返回压缩前后 token 估算。前端在会话头部菜单加"压缩上下文"入口，复用 `context_compacted` 事件提示 |
| `MemoryManager` | **主链路统一切换**：`agent_service.py` 构建 agent 时以 `MemoryManager` 替代裸 `ContextManager`（MemoryManager 完整继承三层预算行为，额外获得 session/long_term/retrieval 命名空间）；runtime fallback 分支同步复用同一构建参数。三层记忆的读写 API 由后续"用户记忆"需求启用 |

### 5.2 prompt 外移（问题 14）

- 压缩 prompt 移至 `domains/docaudit/config/prompts/{locale}/context_compact.j2`；增量合并 prompt 为 `context_compact_merge.j2`（含 4.2 要求的 ref_id/文件路径清单小节）。
- `ContextManager.__init__` 接受 `compact_prompt_template` / `compact_merge_prompt_template`；`agent_service.py` 从 PromptEngine 渲染注入。渲染失败时 catch 并回落到 core 内置的极简英文协议模板（防御性默认，非兼容层）。
- 微压缩 note 改为中文（与会话语境一致），作为协议文本保留在 core。

### 5.3 缓存 GC 与哈希去重（问题 13，全部必做）

**内容哈希去重**：

- `CacheStore.persist` 对 serialized 计算 sha256，命中已有文件则复用其 ref_id，不再重复写盘。
- 维护 hash → ref_id 索引（内存映射，随 snapshot 的 ref_map 持久化；恢复时由 ref_map 反建）。

**会话删除联动 GC**：

- `session_service` 删除会话时，对其 snapshot ref_map 指向的每个缓存文件，**扫描其余所有会话的 snapshot**；无任何其他会话引用同一文件时才删除（天然解决哈希去重后的共享引用问题，无需持久化引用计数）。
- 会话数量级为数十，删除是低频操作，全量扫描可接受；文档注明规模假设。
- 路径穿越防护与现有 rehydrate 逻辑一致。

**孤儿文件**：提供手动清理脚本 `scripts/clean_agent_cache.py`（扫描无会话引用的缓存文件并列出，`--delete` 确认删除）。不做启动时自动扫描。

### 5.4 可观测性（问题 15）

新增 Prometheus 指标（`telemetry/metrics.py`）：

- `courtier_context_tokens`（gauge，按 agent_name 标签）：每次 think 前的上下文大小（校准后判断值）；
- `courtier_context_compaction_total`（counter，`type=full|micro|fallback|failed`）；
- `courtier_context_compaction_duration_seconds`（histogram）：全量压缩耗时（含摘要 LLM 调用）；
- `courtier_context_ref_recover_total`（counter，`result=hit|miss`）：`resolve_refs` 命中/未命中。

Langfuse：compact 事件在现有 trace span 上记录 before/after token 数与摘要长度。

### 5.5 大用户输入治理（问题 16，必做）

用户消息超过 `context_max_user_message_chars`（默认 10000）时，在消息进入 state 之前走与 Layer 1 相同的 persist + 预览机制：content 替换为预览 + `$ref` 标记，原文落盘。处理点放在 API 层（`sessions` 路由/服务），保持 core 简单。LLM 需要全文时通过 `get_artifact` 获取。

---

## 6. 迁移策略（全量切换，无兼容层）

随各 Phase 落地，以下旧机制**就地删除**，不保留开关或兜底：

- `_estimate_chars` 及字符数预算常量 `MAX_CONTEXT_CHARS`；
- `RECENT_TOOL_RESULTS` 固定条数机制；
- `MAX_CONTEXT_CHARS_AFTER_COMPACT` 未使用常量；
- `persist_large_output` / `force_persist` 遗留 API；
- `rehydrate_artifact_store` 的 marker 扫描恢复路径（恢复只走 snapshot）；
- 硬编码于 core 的中文压缩 prompt；
- 裸 `ContextManager` 在主链路的使用（统一为 MemoryManager）。

会话 JSON 直接写入新格式（`context_state` + artifact snapshot）；旧版本保存的会话不保证恢复，用户新建会话即可。SSE 协议复用现有 `context_compacted` 事件；新增 `POST /api/sessions/{id}/compact` 为纯增量。

## 7. 实施顺序与里程碑

| 里程碑 | 内容 | 预估改动量 | 验收 |
|--------|------|-----------|------|
| M1（P1） | 2.1–2.5 五项正确性修复 | 中（~300 行 + 测试） | 新增单测全绿；全量 `uv run pytest -m "not integration"` 通过 |
| M2（P2） | 3.1–3.4 token 化预算、门控、真实 token 校准、压缩后校验 | 中（~450 行 + 测试） | 单测全绿；人工验证长会话压缩触发时机合理 |
| M3（P3） | 4.1–4.3 部分压缩、结构化摘要输入、增量摘要 | 大（~550 行 + 测试） | 单测全绿；端到端审核一份长文档验证摘要质量 |
| M4（P4） | 5.1–5.5 清理、统一切换、force_compact API + 前端入口、GC 与去重、指标、用户输入治理 | 中-大（~600 行 + 测试） | 全量测试 + 前端 `npm run build && npm test` 通过 |

M1 → M2 顺序强依赖（P2 的门控依赖 P1 的正确性基础）；M3 依赖 M2；M4 各项相互独立，可与 M2/M3 并行穿插，其中 5.1 的 MemoryManager 切换建议在 M1 之后尽早完成（CompactState 持久化与其交互最多）。

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| token 启发式估算误差（±20%）导致过早/过晚压缩 | 触发点设 75% 留边距；真实 `prompt_tokens` 每轮校准；配置项可在线调整 |
| 部分压缩/增量摘要改变模型可见历史结构，审核质量回归 | M3 验收含端到端长文档审核对比；压缩 prompt 模板可在 PromptBundle 中调优而无需改代码 |
| 哈希去重后多会话共享缓存文件，删除会话误删共享文件 | 删除前扫描其余会话 snapshot，仅删无引用文件；单测覆盖共享场景 |
| 会话删除联动 GC 全量扫描的性能 | 低频操作 + 会话数量级数十；文档注明规模假设，超量级时改索引 |
| 摘要 LLM 调用失败 | 现有 fallback 保留（对齐边界后的最近 N 条截断），指标计数 `type=fallback` |
| CompactState 持久化格式演进 | 带 `version` 字段，未知版本按空状态处理 |

---

## 附录 A：改造前机制速查（M1 实施前的分析基线）

> 注：本节记录的是改造前的现状。M1 落地后，`rehydrate_artifact_store` 已删除（恢复只走 snapshot），`_full_compact` 保留的最近用户消息已改为跳过 reminder，fallback 切片已对齐 tool-call 边界。

三层预算控制（`context_manager.py`）：

1. **Layer 1 大结果落盘**：工具结果 > `LARGE_OUTPUT_THRESHOLD=3000` 字符时写入 `.agent_cache/`，上下文留 `__persisted_output__` 标记 + `$ref:tool:N` 引用（执行点在 `tools/registry.py:374`）；下游参数中的 `$ref` 由 `ArtifactStore.resolve_refs()` 还原（`registry.py:322`）。
2. **Layer 2 微压缩**：每轮 observe 后（`loop.py:657-658`），tool 消息超过 `RECENT_TOOL_RESULTS=5` 条时，老的替换为 `_omitted` 占位符（未落盘的先补落盘）；同步去重 reminder（`_deduplicate_reminders`）。
3. **Layer 3 全量压缩**：每轮 think 前（`loop_phases.py:60`），`_estimate_chars` 超 `MAX_CONTEXT_CHARS=40000` 时调 LLM 摘要全部历史，输出 = 原 system prompt + 摘要 system 消息 + 最近一条 user 消息；失败回退保留最近 8 条截断；`CompactState` 跟踪压缩状态。

配套：PromptPipeline 注入"上下文规则"段（`get_ref_instructions()`）；`rehydrate_artifact_store`（`stream_service.py:163`）+ artifact snapshot 负责跨请求恢复 ref；压缩触发时经 `on_step("compact")` → SSE `context_compacted` 通知前端。

---

## 附录 B：parse_document 修复轮对上下文链路的增量变更（2026-08-04）

本轮以 parse_document 工具治理为契机，对上下文/artifact 链路做了以下增量修正（均含测试，全量 `uv run pytest -m "not integration"` 1207 通过）：

1. ** summarizer 死亡区间消除 **：`ResultSummarizer` 不再按 actor_type 分档，工具结果超过 `raw_inline_max_chars`（1500）即强制 persist 并挂 `result_id`；原先 1500–6000 字符"丢 raw_data 但无 $ref"的区间不复存在。代价：>1500 字符的工具结果都会落盘一次。`summary_inline_max_chars` 构造参数保留但已废弃。
2. **微压缩占位符携带 ref 与 success**：`_extract_ref_info` 扩展到 summarizer 形态（`result_id` → `metadata.stored.result_id` → `metadata.persisted_ref_id` 三级回退），占位符同时保留 `success`；`_has_successful_call_for` 因此不会在压缩后误判"未解析"而重复强制调用 parse_document。全量压缩摘要形态用文本启发式（同 blob 内 file_path + `$ref:<tool>:` 共现）兜底，保守方向是允许一次幂等重解析。
3. **chat 模式对齐 summarizer**：`build_chat_agent` 的私有 registry 现在与 audit 模式一样配置 `ResultSummarizer` + persist，大工具结果不再全文进上下文。
4. **ref 计数器跨请求连续**：`set_ref` 从 `$ref:<tool>:<n>` 反推并抬升 `ref_counters`，`load_snapshot` 恢复后新文档不再回绕覆盖 `$ref:parse_document:1`。
5. **哈希去重带版本盐**：去重键为 `tool_name:sha256(salt + content)`，默认盐取 courtier 发行版版本（未安装时为 `"0"`，升级需手动 bump，见 `cache_store._default_cache_salt` 注释）；`.hash_index.json` 写入时剔除目标文件已不存在的陈旧条目，不再无界增长。
6. **缓存文件名带熵**：`{tool}_{seq}_{ms}_{uuid6}.{ext}`，消除并发请求/新实例同毫秒同序号互相覆盖的隐患。
7. **超时与强制调用**：插件调用超时抛出带时长的中文错误（此前为空串）；强制首调校验 file_path 参数一致性、首个 think 失败时注入强制调用而非终止 run、强制 tool_call id 带 uuid 后缀避免跨 turn 重复。
8. **插件 system_prompt 接入提示词**：`ExtensionRegistry.get_system_prompts()` → Agent 惰性 provider → PromptPipeline"工具使用说明"段，按 agent 实际持有的插件工具过滤；插件崩溃重启后进行中的会话会热替换同名 ProxyTool，不再持死连接。
