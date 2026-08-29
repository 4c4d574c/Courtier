# 上下文管理模块测试大纲与实施计划

## 背景与目标

上下文管理模块 = `ContextManager`（`courtier/agent/core/context_manager.py`，三层上下文预算控制）+ `MemoryManager`（`memory_manager.py`，四层记忆扩展，生产路径实际构造的类）+ 集成点（loop 压缩事件与用量校准、API compact 路由、run_manager 状态持久化、预算比例推导）。

现状：`tests/agent/test_context_manager.py` 约 60 个用例 + `test_memory_manager.py` 9 个 + compact 事件与 API 路由零散覆盖。基础功能路径大体有覆盖，但边界与极端条件缺口大，且测试组织无统一结构。

目标：
1. **推倒重写**测试套件，统一结构，覆盖全部功能、边界条件、极端条件；
2. 一并实施四个已对齐的行为修复（F1–F4，见 §2）。

## 对齐结论（已与用户确认，2026-08-29）

1. **范围**：核心 + MemoryManager + 集成点，全含。
2. **现有用例**：推倒重写、统一结构；以"旧用例映射附录"（§6）保证不丢覆盖。
3. **疑似问题处置**（逐条对齐）：
   - F1 二次微压缩丢 ref_id → **修**（占位符消息跳过，幂等 no-op）；
   - F2 压缩后校准值残留 → **修，方案 A**（实质压缩后清空校准值；否决了"pin 现状"与"按比例折算"两个备选）；
   - F3 `MemoryManager.fork()` 返回普通 ContextManager → **修，方案乙**（子代理隔离命名空间，fork 携带子代理标识；否决了"父子共享命名空间"的方案甲）；
   - F4 中文 retrieve 切词失效 → **修**（CJK bigram 切分；否决了 pin 现状与 jieba 引依赖）。
4. 处置流程：每个行为修复先写"探针"用例跑出实际行为，复现后修复，再以期望行为断言固化。

## 行为修复设计

### F1 微压缩幂等：占位符消息跳过

- **现状**：占位符顶层键是 `ref_id`，但 `_extract_ref_info`（context_manager.py:331-379）只识别 `raw_data.__persisted_output__`、顶层 `result_id`、`metadata.stored.result_id` 三种形态。旧占位符再次被压缩时提不到 ref，`_persist_tool_message` 因 `raw_data` 为 None 返回 None，新占位符丢掉 `ref_id`/`file`——占位符 note 中"将 $ref 作为工具参数即可恢复完整数据"的承诺失效（数据仍在 store 的 ref_map，但消息不再指向它）。
- **修复**：`micro_compact` 主循环中，内容解析出 `_omitted: true` 的 tool 消息直接原样保留——压缩已压缩消息 = no-op。
- **位置**：`context_manager.py` `micro_compact`。
- **验证用例**：3.3。

### F2 压缩后清空校准值（方案 A）

- **现状**：`budget_usage = max(启发式, 上次 actual)`。全量压缩改写消息元组后，stale actual 仍高于门控直至下一次模型响应刷新——压缩后若有新用户轮次，会白白发起一次摘要合并 LLM 调用（含摘要质量漂移风险）；微压缩门控保持常开，最近 tool 结果被提前转占位符。
- **修复**：`_full_compact` 的**成功路径与 fallback 路径**（两处实际改写元组的地方，增量合并属成功路径）将 `_last_actual_prompt_tokens` 置回 `None`。其余路径（无新历史跳过 / 无模型 / 无可压历史）返回原元组，不动。
- **语义**：校准值只对其测量时的消息元组有效；压缩后回到启发式，下一次模型响应恢复校准（窗口仍为一轮模型调用，方向从"多干活"变为中性）。
- **明确不碰**：`micro_compact` 路径不清空（紧邻下一次模型调用，刷新快，保留保护）。
- **验证用例**：1.8、4.10。

### F3 `MemoryManager.fork()` 子代理隔离命名空间（方案乙）

- **现状**：`ContextManager.fork()` 硬编码返回普通 `ContextManager`（docstring 明示故意）。生产上主代理是 MemoryManager（agent_service.py:231），子代理启动 `fork()`（runtime.py:272）→ **所有子代理都没有四层记忆能力**。四层记忆 API 目前只有 benchmark 调用，产品未接线。
- **修复**：
  - `MemoryManager` 覆写 `fork(sub_name: str | None = None)`：`sub_name` 非空时子代理 `session_id = f"{父.session_id}:sub:{sub_name}"`（嵌套派生自然成链 `sess_x:sub:h1:sub:h2`）；共享 memory store 与 long_term（本来全局）；`_working_summary` 清空；预算/模板/artifact store 复制语义同现有 fork；CompactState 天然独立（现有行为）。
  - `sub_name=None` → 继承父 session_id（共享命名空间，供无标识场景与直接调用）。
  - `ContextManager.fork()` 签名与行为不变（仍返回普通 ContextManager）。
  - runtime.py:272 调用点按类型分派：`isinstance(context_manager, MemoryManager)` → `fork(sub_name=handle.handle_id)`；否则原 `fork()`。取 `handle_id`（实例唯一标识）避免同名技能并行互踩。
- **已知取舍（记录不做）**：孤立子代理命名空间不做自动清理——store 为文件制、记忆未接线，等记忆层迁移（Pi 迁移记忆层级一节）一并设计。
- **验证用例**：5.4、5.6、6.6。

### F4 CJK bigram 查询切词

- **现状**：`retrieve` 用 `query.text.split()` 切词（memory_manager.py:155）。中文无空格 → 整个查询成一词 → 要求记忆 JSON 中出现完整连续子串 → 部分命中得分 0，中文召回塌方。
- **修复**：`memory_manager.py` 新增模块级 `_extract_query_terms(text) -> set[str]`：按空白切块；块内连续 CJK 串滑窗成重叠 bigram（长度 1 的 CJK 串保留单字）；块内连续非 CJK 串按原语义整串收录（保留 `len>1` 过滤）；统一 lowercase。`retrieve` 换用该函数；`_score_memory` 得分语义不变（命中比例、子串匹配）。
- **已知代价（记录不做）**：无意义 bigram（如"度报"）可能误命中——比例计分 + top_k 排序下可控。将来换 ES/向量后端时本套语义测试作回归基线（模块 docstring 的 "intentionally replaceable" 契约不变）。
- **验证用例**：6.2。

## 新测试套件结构与统一约定

```
tests/agent/core/
├── conftest.py                  # 扩展：mock_model、make_context_manager 预算工厂、消息构造助手
├── test_context_manager.py      # 重写：第 1–5 节（估算 / L1 / L2 / L3 / 状态与 fork）
├── test_memory_manager.py       # 重写：第 6 节
└── test_context_integration.py  # 新增：第 7 节（吸收 test_compact_event.py 后删除该文件）
```

- 删除旧 `tests/agent/test_context_manager.py`（迁移完成后，见任务 7）。
- store 级行为用例（persist/dedup/ref_map）迁入 `tests/agent/artifacts/test_store.py`，与现有 persist 测试合并去重。
- 若 `tests/agent/core/` 缺 `__init__.py` 则补上——新旧 `test_context_manager.py` 并存期避免 pytest 同名模块冲突。

统一约定：
- 每个用例 docstring 首行标注大纲编号（如 `4.10 压缩后…`）；
- 命名 `test_<行为>_<条件>`；
- 每条用例标注处置：**修复验证**（F1–F4）/ **pin**（固化现状 + 注释说明理由）/ **极端** / **回归**（旧用例迁移）；
- 全部 mock 模型，不依赖真实 LLM 与网络；磁盘用 `tmp_path` 隔离；不新增 `integration` 标记。

## 用例清单

### 第 1 节 Token 估算与预算校准（8）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 1.1 | 空消息元组、content=None → 估算为 0 | 边界 | 新增 |
| 1.2 | CJK 各区段（基本区、扩展 A/B、兼容区）均按 CJK 计权 | 功能 | 新增 |
| 1.3 | 中英混排 0.65/0.25 双权重比例 | 功能 | 新增 |
| 1.4 | content=None + tool_calls：参数 JSON 计入估算 | 边界 | 新增 |
| 1.5 | estimate 与 actual 恰好相等 → max 语义稳定 | 边界 | 新增 |
| 1.6 | `update_actual_usage` 覆盖语义：新报告覆盖旧值（非取历史最大） | 边界 | 新增 |
| 1.7 | 1MB 级文本估算耗时上限（护栏断言放宽） | 极端 | 新增 |
| 1.8 | 压缩后校准值已清空：budget_usage 回落为启发式 | 边界 | **修复验证 F2** |

### 第 2 节 Layer 1 $ref（ContextManager 侧，6）

store 级行为（persist 小/大、dedup、ref_map、盘上文件一致性）迁 artifacts 套件，见 §6 附录。

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 2.1 | `preview_max_chars` 生效：预览截断带省略标记 | 边界 | 新增 |
| 2.2 | `large_output_threshold=0` 全落盘 / 极大值全直通 | 边界 | 新增 |
| 2.3 | 磁盘写失败（OSError/只读目录）→ 不抛出、返回未持久化标记 | 极端 | 新增 |
| 2.4 | >10MB JSON 落盘 + resolve 往返一致 | 极端 | 新增 |
| 2.5 | 伪装串不误解析（`$$ref:`、`$ref:` 空名、前缀相似串） | 边界 | 新增 |
| 2.6 | 同 ref 重复 resolve 幂等 | 边界 | 新增 |

### 第 3 节 Layer 2 微压缩（9）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 3.1 | 零 tool 消息 / 空历史 → 原样返回 | 边界 | 新增 |
| 3.2 | `recent_tool_results_tokens=0`、`micro_compact_tokens=0`/负值 | 边界 | 新增 |
| 3.3 | 占位符重复压缩 no-op：ref_id/file 保留、不再落盘 | 边界 | **修复验证 F1** |
| 3.4 | 非法 JSON / 纯文本 tool 结果 → 无 ref 占位符（数据不可恢复，确认接受） | 边界 | pin |
| 3.5 | 空 content tool 消息；`name=None` 回退 `tool_call_{id}` 命名 | 边界 | 新增 |
| 3.6 | 同一 tool_call_id 多条结果 | 边界 | 新增 |
| 3.7 | persist 中途失败（磁盘满）→ 占位符降级不崩溃 | 极端 | 新增 |
| 3.8 | 数千条 tool 消息微压缩耗时上限 | 极端 | 新增 |
| 3.9 | 提醒去重组合：periodic/pre-turn 交错、同文不同 source | 边界 | 新增 |

### 第 4 节 Layer 3 全量压缩（12）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 4.1 | 空元组 / 仅 system / 仅 assistant / 仅 reminder → boundary 回退路径不崩 | 边界 | 新增 |
| 4.2 | `model=None` → 原样返回 + failed 指标 | 边界 | 新增 |
| 4.3 | 模型返回空 content → `system_body[:500]` 兜底 | 边界 | 新增 |
| 4.4 | `compact_target_tokens` > `max_context_tokens`（配置倒挂） | 边界 | 新增 |
| 4.5 | `max_context_tokens=1` → 每轮必压、无死循环 | 极端 | 新增 |
| 4.6 | 连续 3 次压缩：编号 #1→#3 递增，增量合并链摘要不降级 | 功能 | 新增 |
| 4.7 | merge 段仅含 reminder → `_build_summary` 空串仍走通 | 边界 | 新增 |
| 4.8 | `on_compact_start` 抛异常向上传播（预期 run 失败） | 极端 | pin |
| 4.9 | 第二次压缩时模型才失败 → merge 路径 fallback | 边界 | 新增 |
| 4.10 | 压缩后新用户轮 + 小 estimate → 不再触发合并；`update_actual_usage(高)` 后恢复触发 | 边界 | **修复验证 F2** |
| 4.11 | 伪造"[上下文压缩 #"前缀的 system 文本被 `_is_summary_message` 误判 | 边界 | pin+记录 |
| 4.12 | 压缩后当前轮仍超预算 → `micro_compact(force=True)` 兜底分支 | 回归 | 新增 |

摘要构建与助手函数（`_build_summary` 角色差异化、`_align_tool_boundaries` 四种修复、`_split_compact_template`、`_find_last_real_user`、`_fit_text_to_token_budget`、`_summarize_tool_message`）作为第 4 节内子组保留，旧用例迁移（附录）。

### 第 5 节 状态持久化与 fork（6）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 5.1 | `load_state` 异常类型（compact_count 字符串/负数、has_compacted 非布尔） | 边界 | 新增 |
| 5.2 | payload 部分字段缺失 | 边界 | 新增 |
| 5.3 | `snapshot_state` 不含 `last_compact_over_budget` → 重启后 flag 重置 | 边界 | pin |
| 5.4 | `MemoryManager.fork(sub_name)`：返回 MemoryManager、命名空间隔离、共享 store/long_term、working 清空、预算保留、CompactState 独立 | 功能 | **修复验证 F3** |
| 5.5 | `ContextManager.fork()` 行为不变（返回普通 ContextManager、共享缓存隔离状态） | 回归 | 迁移 |
| 5.6 | 嵌套 fork：命名空间成链 `sess:sub:h1:sub:h2` | 功能 | **修复验证 F3** |

### 第 6 节 MemoryManager 四层记忆（7）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 6.1 | session/long_term 的 delete/keys：删不存在 key、空 namespace | 边界 | 新增 |
| 6.2 | bigram 切词：整串命中 1.0、部分命中比例得分、中英混合、ASCII 行为不变、单字 CJK 回退、空 query 无结果 | 功能 | **修复验证 F4** |
| 6.3 | `top_k=0`/负值 | 边界 | 新增 |
| 6.4 | 压缩后 `_working_summary` 更新、working 层可命中 | 功能 | 迁移 |
| 6.5 | memory store 文件损坏 → 读取降级不崩 | 极端 | 新增 |
| 6.6 | `fork(sub_name)` 后 session 记忆与父隔离、long_term 双向共享 | 功能 | **修复验证 F3** |
| 6.7 | session/long_term 基本 CRUD 与隔离（旧用例统一结构迁移） | 回归 | 迁移 |

### 第 7 节 集成点（6）

| 编号 | 场景 | 类型 | 处置 |
|---|---|---|---|
| 7.1 | think_phase 后 `update_actual_usage` 以 provider prompt_tokens 被调用（loop_phases.py:158） | 功能 | 新增 |
| 7.2 | run_manager context_state 落盘 → 重载 → `has_compacted` 守卫跨请求生效 | 功能 | 新增 |
| 7.3 | `_context_budget_kwargs` 比例推导：settings 覆盖默认值；窗口 0/负值不崩 | 边界 | 新增 |
| 7.4 | API compact 空会话 400 分支（"会话还没有可压缩的上下文"） | 边界 | 新增 |
| 7.5 | `_persist_oversized_task` 超长任务落盘改写、短任务原样（sessions.py:82） | 功能 | 新增 |
| 7.6 | think_phase 的 compacting/compact 事件与先后顺序（吸收 test_compact_event.py） | 回归 | 迁移 |

合计 54 个大纲用例；旧套件 60+9+3 个用例经附录映射迁移并入。

## 旧用例映射附录

| 旧位置（类/文件） | 用例数 | 去向 |
|---|---|---|
| TestLayer1PersistLargeOutput | 9 | artifacts 套件（store 级）+ 第 2 节（manager 侧） |
| TestLayer2MicroCompact | 3 | 第 3 节 |
| TestMicroCompactPlaceholderFile / Gate / TokenBasedKeep / PlaceholderRefAndSuccess | 11 | 第 3 节 |
| TestLayer3FullCompact | 6 | 第 4 节 |
| TestBuildSummary / TestFindLastRealUser / TestAlignToolBoundaries / TestFallbackAlignsToolBoundaries | 15 | 第 4 节助手子组 |
| TestFullCompactSkipsReminders / TestPartialCompaction / TestIncrementalSummary / TestPostCompactValidation | 9 | 第 4 节 |
| TestFitTextToTokenBudget / TestSummarizeToolMessage / TestSplitCompactTemplate / TestCompactPromptTemplateInjection | 11 | 第 4 节 |
| TestEstimate / TestActualUsageCalibration | 3 | 第 1 节 |
| TestResolveRefs / TestResolveMarkerDict / TestGetRefInstructions / TestRefResolutionIntegration | 19 | 第 2 节 |
| TestPersistWithSchema | 1 | artifacts 套件 |
| TestCompactState / TestCompactStatePersistence / TestFork | 8 | 第 5 节 |
| TestDeduplicateReminders | 4 | 第 3 节（3.9 组） |
| test_memory_manager.py（全） | 9 | 第 6 节 |
| test_compact_event.py（全） | 3 | 7.6 |

## 实施顺序（逐任务提交）

每个任务一个 conventional commit；修复类任务先探针复现再修，提交前跑 `uv run pytest -m "not integration"` 确认旧套件不红。

1. `docs` 本计划文档。
2. `test` 新套件骨架：`tests/agent/core/conftest.py`（fixtures/工厂/消息助手，补 `__init__.py`）。
3. `fix(agent)` F1 占位符幂等 + 3.3。
4. `fix(agent)` F2 清空校准值 + 1.8、4.10。
5. `fix(agent)` F4 bigram + 6.2。
6. `fix(agent)` F3 fork 乙 + runtime 分派 + 5.4、5.6、6.6。
7. `test(agent)` 重写第 1、2、4 节 → `tests/agent/core/test_context_manager.py` 初版。
8. `test(agent)` 第 3、5 节并入同文件。
9. `test(agent)` 第 6 节重写 `test_memory_manager.py`；第 7 节新建 `test_context_integration.py`（吸收并删除 `test_compact_event.py`）；删除旧 `tests/agent/test_context_manager.py`。
10. `test(artifacts)` store 级迁移用例并入 `tests/agent/artifacts/test_store.py`。
11. 收尾：全量验证（pytest 非 integration 全绿、ruff/mypy 干净）、附录逐类核对、回填本文件"实施状态"。

## 验收标准

- `uv run pytest -m "not integration"` 全绿（旧文件删除后）；
- 新套件全离线：无网络、无真实 LLM、`tmp_path` 隔离磁盘；
- ruff / mypy 对改动文件干净；
- 四个修复各有"修复验证"用例，F1/F2/F4 探针复现记录在"实施状态"；
- 旧用例映射附录逐类核对，无覆盖静默丢失。

## 已知局限（记录不做）

- 孤立子代理记忆命名空间无自动清理（F3 乙取舍）；
- 非法 JSON / 纯文本 tool 结果压缩后不可恢复（3.4 pin）；
- "[上下文压缩 #"前缀可被 system 文本伪造（4.11 pin+记录）；
- `last_compact_over_budget` 不跨重启持久（5.3 pin）；
- bigram 误命中噪声（F4 已知代价）；
- retrieve 仍为占位实现，升级路线为 ES/向量后端。

## 实施状态

**已完成（2026-08-29）。** 计划文档提交 2fd907f 之后的提交链：

| 提交 | 内容 |
|---|---|
| d5e869c | T2 conftest 骨架（fixtures / 预算工厂 / 消息构造助手） |
| 518a644 | F1 微压缩占位符幂等（探针复现 → 修）+ 3.3 |
| 6ffc8c8 | F2 压缩后清空校准值（方案 A）+ 1.8 / 4.10 |
| c8e6c5a | F4 CJK bigram 切词 + 6.2 |
| 1cab59a | F3 fork 乙（MemoryManager.fork(sub_name) + runtime 分派）+ 5.4 / 5.6 / 6.6 |
| 72bb1cd | （探针新发现的计划外修复）persist 磁盘 IO 失败优雅降级 + 2.3 / 3.7 |
| 8028213 | §1–§5 重写（tests/agent/core/test_context_manager.py，104 用例） |
| cbaf58b | T10 store 级用例迁入 artifacts 套件（9 用例） |
| bc24cbe | §6 重写（18 用例）+ §7 集成套件（7.1/7.3/7.6）+ 删除旧 test_context_manager.py 与 test_compact_event.py；7.2 落 test_run_manager.py、7.4 落 test_routes.py |
| 6a06fed / b36c9bf | lint 修复 / 逐名核对审计补迁 3 例（on_compact_start 时机、periodic 提醒变体、占位符 file 存在性） |

验证：`uv run pytest -m "not integration"` **1872 passed / 6 skipped / 0 failed**；警告 8 → 3（旧文件的 asyncio 标记 PytestWarning 随删除消失，余 3 条为 test_cache_store.py 既有）；ruff 对全部触碰文件干净；mypy 因 `telemetry/metrics.py` 既有语法报错无法完成（非本次改动，遗留问题）。旧套件 98 个用例名与新月套 150 个用例名逐名核对，补齐 3 处缺口后无覆盖丢失。

### 偏离计划记录

1. T7+T8 合并为一个提交（同一文件分两次提交无增量价值）。
2. T10 实际先于 T9 执行：旧文件删除前必须先迁出 store 级用例。
3. 7.2 落在 test_run_manager.py、7.4 落在 test_routes.py（复用各自 harness），未集中进 test_context_integration.py。
4. 7.5（_persist_oversized_task）侦察有误：test_routes.py 已有 4 个用例完整覆盖，无需新增。
5. 计划外第 5 个修复（探针发现）：`cache_store.persist` 的 `_write_file` 让 OSError 逃逸，registry Layer-1 与微压缩在磁盘满 / 只读时整个崩掉，与 `_persist_tool_message` 注释意图相悖。按处置框架"确认是 bug → 修"：persist 降级返回未持久化结果（调用方已有处理分支），微压缩在自身 persist 失败时保留原消息而非生成无指针占位符。
6. 事实纠正：`update_actual_usage` 是覆盖语义（讨论中曾误述为取历史最大），1.6 按覆盖语义固化。
7. 编写期新固化的行为：微压缩 keep 选择是"从新到旧前缀、预算耗尽即 break"（3.5/3.6），不是背包式挑选——最旧的小结果不会被保留；retrieve top_k 负值走 Python 负切片（6.3 记录怪癖）。

### 结果分析（不足与待优化）

**性能观察（2026-08-29 复审：两项已处理）**
- ~~微压缩串行落盘~~ → **已修**（perf 2e3e356）：`persist_many` 批量落盘，一次锁 + 一次写盘 pass + 批内去重；真正瓶颈是 `_dedup_record` 逐条全量重写哈希索引（二次方），批内改为内存累积、末尾落盘一次。千条结果压缩 5.3s → 0.05s，护栏收紧到 1s。
- ~~`estimate_tokens` 每次调用 O(总字符数)~~ → **已修**（perf d5fb2d8）：Message 冻结、单条成本不变，按消息实例记忆化（id + 弱引用校验，复用 id 不回旧值），每轮只重算新增消息。
- ~~微压缩 keep 前缀语义过度压缩~~ → **已定（2026-08-29）**：保持现状（方案 B）——默认配置下 Layer 1 已把大结果转为小 marker，停止点之后丢小结果的场景基本是理论性的，连续前缀对模型更可预测；现状由 3.5/3.6 用例固化。

- ~~微压缩 keep 前缀语义在"最新一条巨大 + 更旧若干小结果"场景会过度压缩（只保 MIN=2）~~ → **已定（2026-08-29）**：保持现状（方案 B）——默认配置下 Layer 1 已把大结果转为小 marker，问题基本是理论性的；现状由 3.5/3.6 用例固化。

**行为层发现（已固化 / 已记录）**
- F2 修复后的取舍：压缩后一轮为纯启发式窗口（有 4.10 防重压测试背书）。
- persist IO 失败降级后，持续失败时上下文无法收缩（flag 与指标可见），无数据损失——可接受的保守行为。

**覆盖缺口（2026-08-29 复审：已补测，test 1e07553）**
- ~~ES 主后端持久化路径~~ → 真 ES integration 测试（跨 store 实例经 ES 回读）。
- ~~CompactState 未来版本升级迁移~~ → 前向兼容测试（v1 payload 带未知附加字段）。
- ~~父子代理并发共享 store~~ → 批量 + 单条并发落盘无 ref 碰撞、全部可读；fork 子代理并发写命名空间隔离、long_term last-writer-wins。
- ~~记忆层仍未接线产品路径~~ → **第一期已接线**（feat 5d5d70a，2026-08-29）：4 个 memory_* builtin 工具（registry 注入 run 作用域 context_manager，子代理 fork 命名空间自动生效）+ think_phase 每真实用户轮自动检索注入（source=hint、settings 可关、PromptBundle 模板 context.memory_recall_hint）；边界修复：_find_last_real_user_index 排除 hint（fix 9cc3a82）。每轮注入仅一次、无命中零成本；自动检索注入目前仅在离线测试覆盖，真实端到端会话验证待接入后观察。
- ~~既有清理候选：test_cache_store.py 的 3 条 PytestWarning~~ → 已清理（test c5dd8d7）；mypy 因 metrics.py 注释事故被阻塞的问题已修复（fix 7cba49f），触碰文件 mypy 全绿。
