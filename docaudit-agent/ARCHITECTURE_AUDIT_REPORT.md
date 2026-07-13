# Python 系统架构审计报告：DocAudit / SDTAgent

> **审计日期**：2026-06-23
> **分析范围**：`docaudit-agent/src/` 下所有业务包（排除 `tests/` 和 `migrations/`）
> **代码规模**：200 个 Python 文件，~33,400 行代码
> **Python 版本**：3.12+

---

## 一、模块组成与依赖关系

### 1.1 顶层包划分

| 包名 | 路径 | 职责描述 | 依赖包数 | 被依赖数 |
|------|------|----------|----------|----------|
| `agent` | `src/agent/` | Agent 运行时：核心循环、LLM 集成、工具注册、记忆、权限、遥测、API 服务、提示词管道、Hook、Artifact 管理、子代理 | 0 | 1（plugin） |
| `content_compliance` | `src/content_compliance/` | 内容合规检查：协议定义、规则引擎、检查器注册表、JSON 加载器 | 0 | 1（plugin） |
| `dbop` | `src/dbop/` | 数据库操作：异步 SQLAlchemy 会话、CRUD 仓储、文档/页面/段落/元素表模型 | 1（docmodels） | 1（validator） |
| `dedump` | `src/dedump/` | 抄袭检测（模块名"dedump"是遗留命名）：相似度计算、动态阈值、MinHash 预筛 | 0 | 0 |
| `docannot` | `src/docannot/` | DOCX 标注：关键词规则匹配、docxnote 库 Monkey-patch | 0 | 0 |
| `docbuilder` | `src/docbuilder/` | 文档构建：从 Document 模型构造 DOCX、LibreOffice PDF→DOCX | 2（docmodels, docparse） | 0 |
| `doccorrector` | `src/doccorrector/` | 中文文本纠错：6 阶段管道（词典→4B模型→重复字符→全半角→标点混用→截断） | 0 | 0 |
| `docmodels` | `src/docmodels/` | **共享数据模型（核心枢纽）**：Pydantic 模型（Document、Page、Paragraph、Font 等）和格式规范模型 | 0 | **4** |
| `docparse` | `src/docparse/` | 文档解析：PDF（文本+扫描件）、DOCX、OCR 集成、结构识别、间距分析 | 1（docmodels） | 1（docbuilder） |
| `es` | `src/es/` | Elasticsearch 客户端：单例管理、分块索引、搜索、标注追加 | 1（config） | 0 |
| `plugin` | `src/plugin/` | 插件系统：子进程隔离、JSON-RPC over stdio、Manifest 扫描、扩展注册表、代理对象 | 2（agent, content_compliance） | 0 |
| `storage` | `src/storage/` | 文件存储抽象：MinIO 后端、文件缓存、异步单例访问器 | 1（config） | 0 |
| `validator` | `src/validator/` | 文档格式校验：字体规范化、文中检测、格式检查、模板 CRUD/预览/骨架 | 3（docmodels, config, dbop） | 0 |

### 1.2 关键依赖链路

```
config ──► storage                    (MinIO 配置)
config ──► es                         (ES 主机配置)
config ──► validator                  (DB URL、LLM 配置)

docmodels ◄── dbop                    (存储已解析文档)
docmodels ◄── docparse                (解析文档为模型)
docmodels ◄── docbuilder              (从模型构建 DOCX)
docmodels ◄── validator               (按规范校验文档)

dbop ──► validator                    (content_checker 和模板 CRUD 的 DB 访问)

docparse ──► docbuilder               (converter 中复用 rule_patterns)

agent ──► plugin                      (插件代理挂载到 agent 注册表)
content_compliance ──► plugin         (插件代理挂载到 checker 注册表)
```

**最长依赖链**：
```
docmodels ◄── docparse ◄── docbuilder
                ▲
config ────────┘
dbop ◄─────────┘ ──► validator
```

### 1.3 依赖拓扑图

```
                    ┌──────────┐
                    │  config  │ (零依赖 src 包)
                    └────┬─────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    ┌─────────┐    ┌──────────┐    ┌───────────┐
    │ storage │    │    es    │    │ validator │
    └─────────┘    └──────────┘    └─────┬─────┘
                                         │
         ┌───────────────────────────────┤
         │                               │
         ▼                               ▼
    ┌───────────┐                   ┌──────┐
    │ docmodels │◄──────────────────│ dbop │
    └─────┬─────┘                   └──────┘
          │
    ┌─────┼──────────┐
    ▼     ▼          ▼
  docparse │    docbuilder
           │
    ┌──────┼──────────┐
    ▼      ▼          ▼
  dedump docannot doccorrector
  (叶子包，无下游依赖)

    ┌───────┐     ┌─────────────────────┐
    │ agent │────►│       plugin        │
    └───────┘     │ (fan-out 最高的包)  │
                  └──────────┬──────────┘
                             │
                  ┌──────────▼──────────┐
                  │ content_compliance  │
                  └─────────────────────┘
```

### 1.4 关键指标

| 指标 | 数值 |
|------|------|
| **循环依赖** | **0**（有向无环图） |
| **最高扇入** | `docmodels`（被 4 个包依赖） |
| **最高扇出** | `plugin` 和 `validator`（各依赖 3 个外部包） |
| **动态导入** | 2 处（`exporter.py`、`parser.py` 中使用 `importlib` 加载插件） |
| **延迟导入** | 1 处（`es/client.py` 中延迟导入 `config.Settings` 避免循环） |

---

## 二、冗余逻辑分析

### 2.1 重复代码片段

| 位置 1 | 位置 2 | 相似度 | 描述 | 建议 |
|--------|--------|--------|------|------|
| `agent/agents/exporter.py:12-27` | `agent/agents/parser.py:12-27` | **100%** | `_import_plugin_tool()` 函数完全相同（10 行 importlib 样板代码） | 提取至 `agent/agents/_utils.py` 或 `agent/tools/plugin_loader.py` |
| `dbop/_utils.py:26-51` | `validator/format_checker.py:26-47` | **90%** | `SECTION_FIELDS` 与 `FIELD_NAME_CN` 枚举相同的文档节类型（文号、标题、主送机关 等）。`_utils.py` 声明为"单一来源"，但 `validator` 中有独立副本 | `validator/format_checker.py` 应从 `dbop/_utils.SECTION_FIELDS` 导入 |
| `agent/core/model.py:20-44` | `agent/agents/base.py:311-316` | **80%** | JSON 字符串→dict 解析逻辑（`json.loads()` + `try/except` 模式）在 3 处独立实现 | 提取统一函数 `try_parse_json(raw: str) -> dict \| None` |
| `docparse/parsers/font_detector.py` | `docparse/parsers/scanned/font_detector.py` | **70%** | 两个模块都实现基于行的字体信息合并（`merge_font_detections` vs `merge_font_info`），仅合并策略不同（覆盖 vs 保留现有值） | 统一为一个带有策略参数的函数 |
| `agent/core/state.py:20-27` | `agent/memory/store.py:15-19` | **30%** | 两个文件都定义了 `_json_default()` 用于 JSON 序列化回退，但行为不同 | 合并为共享的序列化工具 |

### 2.2 功能重叠函数

| 函数 A | 函数 B | 重叠功能 | 建议 |
|--------|--------|----------|------|
| `content_compliance/core.py` 中的 `Violation`/`ComplianceResult`/`ContentChecker` 协议 | `validator/content_checker.py:34-57` 中的 `ContentViolation`/`ContentViolationList`/`ContentChecker` 类 | 内容合规检查（协议 vs Pydantic 实现，数据结构不兼容） | 合并为单一系统。`content_compliance/` 中的协议设计更清晰 |
| `docparse/parsers/spacing.py`（803 行） | `docparse/parsers/scanned/spacing.py`（264 行） | 间距分析（段落边界、边距、缩进、对齐），两个模块互相导入 | 将扫描特有功能（跨页延续、直方图聚类）移入共享 `spacing.py`，或记录有意分离的原因 |
| `docparse/parsers/scanned/structure.py`（11 行，单函数） | `docparse/parsers/structure_recognizer.py`（740+ 行） | `scanned/structure.py` 中的 `collect_all_paragraphs` 是主管道的一个微小包装器 | 将 `scanned/structure.py` 合并到 `scanned/__init__.py` 以减少文件碎片 |

### 2.3 冗余分支 / 死代码

| 位置 | 类型 | 描述 | 建议 |
|------|------|------|------|
| `agent/core/loop_hints.py:16-22` | **假 ImportError 守卫** | 使用 `try/except ImportError` 导入同项目包（`src.agent.artifacts.*`）内的 3 个模块——运行时始终可用，导入守卫无意义 | 替换为直接导入 |
| `agent/core/loop_hints.py:16-22` | **未使用导入** | 导入的 `ProjectionPolicy`、`create_default_projector_registry`、`ProjectionResolver` 从未使用，仅使用布尔标志 `_PROJECTION_AVAILABLE` | 删除未使用的导入，只保留 `_PROJECTION_AVAILABLE = True` |
| `validator/format_checker.py:487-494` | **开发脚手架残留** | `if __name__ == "__main__":` 块引用硬编码测试文件路径，随生产代码一起发布 | 删除或移入 `tests/` 夹具 |
| `agent/core/loop_streaming.py:10-11` | **无意义守卫** | `if __name__ == "__main__": raise RuntimeError(...)` — 此模块从不应被直接执行 | 删除守卫 |
| `docbuilder/builder.py:400` | **禁用的管道步骤** | `# TODO(ticket): Re-enable ruling line reconstruction when template spec is finalized` — 主要管道步骤（ruling line reconstruction）被禁用 | 实现 ruling line reconstruction 或删除所有相关代码 |
| `docparse/parsers/ocr/ppstructure.py:100-122, 425-447` | **遗留 OCR API 格式** | 在 `_get_ocr_arrays()` 和 `_parse_blocks()` 中分支处理 `overall_ocr_res`（旧格式）——如所有 OCR 服务器已迁移至当前格式，则为死代码 | 待评估（参见 四、兼容层分析） |

### 2.4 空/吞没的异常处理器

| 位置 | 行号 | 模式 | 描述 | 严重度 |
|------|------|------|------|--------|
| `plugin/client.py` | 89-90 | `except Exception: pass` | 连接循环内吞没异常 | **高** |
| `plugin/client.py` | 298-299 | `except Exception: pass` | 发送通知时吞没异常 | **高** |
| `plugin/client.py` | 353-354 | `except Exception: pass` | 读循环中吞没异常 | **高** |
| `plugin/manager.py` | 370-371 | `except Exception: pass` | `cancel_pending` 中吞没异常 | **中** |
| `plugin/manager.py` | 412-413 | `except Exception: pass` | `kill` 中吞没异常 | **中** |
| `agent/api/services/stream_service.py` | 311-312 | `except Exception: pass` | 流式传输中吞没异常 | **中** |
| `plugin/proxies.py` | 288-289 | `except Exception: pass` | 注释"不掩盖原始错误" | **低** |
| `doccorrector/corrector.py` | 157-159 | `except ImportError: pass` | pycorrector/torch 不可用时静默跳过 | **低**（可接受：可选增强） |
| `dedump/dedump.py` | 134-135 | `except ImportError: return []` | datasketch 不可用时回退到穷举搜索 | **低**（可接受：性能降级，不损失正确性） |

> **注意**：未发现 bare `except:`（无类型）处理器。

### 2.5 冗余类型导入（Python 3.12+ 项目中的 Python 3.8 风格）

以下文件使用 `typing.List`、`typing.Optional`、`typing.Dict`、`typing.Tuple`、`typing.Union`，在目标为 Python 3.12+ 的项目中完全多余：

| 包 | 受影响文件数 |
|----|-------------|
| `dbop/` | **7** 个文件（tables/document, page, paragraph, element, format_template, library, resource + db_manager, load_doc, save_doc） |
| `docmodels/` | **2** 个文件（document.py, spec.py） |
| `dedump/` | **1** 个文件（dedump.py） |
| `validator/` | **4** 个文件（format_checker.py, templates/crud.py, templates/preview.py, templates/skeleton.py） |
| `agent/` | **0**（已现代化） |

> 注意：`agent/` 包已全部使用现代语法（`list[X]`、`dict[K, V]`、`X | None`），仅旧版领域包需要更新。

---

## 三、备份/降级逻辑分支

### 3.1 备份/冗余写入

| 位置 | 逻辑类型 | 当前状态 | 评估建议 |
|------|----------|----------|----------|
| `agent/api/session_store.py:58-61, 104-121` | 双写（内存 dict + JSON 文件） | 活跃 | **保留**。有意的崩溃恢复模式（注释："Persist on each update for crash recovery"） |
| `agent/core/cache_store.py:99-138` | 双写（磁盘文件 + ref_map 内存索引） | 活跃 | **保留**。ref_map 是解决 `$ref` 引用解析的必要组件 |
| `agent/core/loop_phases.py:251-263` | 跨组件写路径（cache_store 同时传递给 ContextManager 和 ToolRegistry） | 活跃 | **保留**。预期的合约连线 |

> **结论**：未发现真正的"双写多存储"模式（DB + Redis + MQ）。所有"双写"均限定在单进程内的内存+磁盘，设计意图清晰。

### 3.2 降级/特性开关

| 位置 | 逻辑类型 | 当前状态 | 评估建议 |
|------|----------|----------|----------|
| `agent/artifacts/executor.py:129, 191` | `ProjectionFeatureFlags` — projector schema 校验开关 | 活跃 | **保留**。干净的 Pydantic Feature Flag 模式 |
| `agent/telemetry/tracer.py:62-66` | `OTEL_LOG_LEVEL=DEBUG` 时启用 ConsoleSpanExporter | 活跃 | **保留**。仅调试用途，范围明确 |
| `config.py:175-189` | `deployment_env` 校验（staging/production 强制要求 JWT/admin 密码） | 活跃 | **保留**。安全校验器，非行为开关 |
| `agent/agents/assistant.py:142-143` | Gateway 搜索仅在 `gateway_agent_base_url` 已配置时使用 | 活跃 | **保留**。清晰的配置驱动行为 |

> **未发现**：waffle / unleash / 特性开关库；Kill Switch 模式；持续启用且从未恢复的降级逻辑。

### 3.3 重试与兜底逻辑

| 位置 | 逻辑类型 | 当前状态 | 评估建议 |
|------|----------|----------|----------|
| `agent/agents/subagent/runner.py:294-377` | 自定义重试循环，指数退避 `await asyncio.sleep(2 ** attempt)`，基于 `FailureStrategy` 枚举（STRICT/RETRY/TOLERANT） | 活跃 | **保留**。设计良好的可配置重试策略。但注意：`TOLERANT` 模式下非超时异常直接返回 `ToolResult(success=False)` 而不重试——可能掩盖瞬时故障 |
| `plugin/manager.py:303-357` | 插件崩溃恢复：指数退避 `delay = min(1 * (2 ** (n-1)), 30)`（上限 30s），最多重启 3 次；5s 内崩溃标记为 FATAL 的电路断路器 | 活跃 | **保留**。**示范级设计**——电路断路器 + 重试组合 |
| `agent/core/model.py:482-496` | 模型流式→非流式降级：vLLM/Qwen 已知 bug（`finish_reason="tool_calls"` 但无实际工具调用数据）时回退到非流式 `generate()` | 活跃 | **保留**。针对已知模型供应商问题的定向降级 |
| `docparse/parsers/llm_client.py:309, 388, 551, 634` | **每个 LLM API 调用被 `try/except Exception` 包裹并转为 `RuntimeError`——零重试** | 活跃 | **⚠️ 高优先级**。单个瞬时网络错误将导致整个文档解析失败。4 个调用点均无重试且用 `raise RuntimeError(...)` 丢失异常链（应使用 `raise ... from exc`） |
| `agent/agents/assistant.py:87-92` | 查询改写超时（180s）或异常时降级返回原始查询 | 活跃 | **保留**。干净的降级——用户至少能得到原始查询结果 |
| `agent/agents/assistant.py:101-116, 152-164` | ES 搜索（5s 超时）和 Gateway 搜索（10s 超时）失败时返回空 `("", [])` | 活跃 | **保留**。外部服务隔离良好 |

### 3.4 优雅降级

| 位置 | 逻辑类型 | 当前状态 | 评估建议 |
|------|----------|----------|----------|
| `agent/core/context_manager.py:277-282` | LLM 摘要失败时降级为纯文本截断（`summary = summary_text[:800]`） | 活跃 | **保留**。**示范级优雅降级**——压缩继续执行但内容降级 |
| `agent/agents/assistant.py:274-277` | LLM 流失败时向用户发送中文错误提示 `"AI 服务暂时不可用，请稍后重试。"` | 活跃 | **保留**。良好的用户沟通 |
| `validator/templates/preview.py:214-215` | 印章图片缺失时回退为空字符串 `""` | 活跃 | **保留**。精确的特定异常降级 |
| `doccorrector/corrector.py:136-162` | pycorrector/torch 不可用时静默跳过纠错阶段 | 活跃 | **保留**。可选增强的合理降级 |

### 3.5 过度防御

| 位置 | 逻辑类型 | 风险 | 评估建议 |
|------|----------|------|----------|
| `agent/core/model.py:20-92` | 工具参数解析有 **4 层**回退：(1) `$ref` 正则预引用, (2) JSON 解析, (3) 未转义引用修复+重试, (4) `ast.literal_eval` | 中（`ast.literal_eval` 已知安全问题） | **保留**。大小（>10K 字符）和嵌套深度（>100）限制缓解了 DoS 风险。为兼容多种模型输出（OpenAI、Qwen、DeepSeek、vLLM）所必需的复杂性 |
| `agent/core/loop_streaming.py:43-53` | Token 回调异常被双重 `try/except Exception: logger.debug(...)` 静默吞没 | 低 | **简化**。两个 catch 块做相同的事。将日志级别从 DEBUG 提升至 WARNING 以便生产环境可见 |
| `dbop/db_manager.py:131-132` | CRUD 硬上限 `_MAX_LIMIT = 10_000`，默认限制 `_DEFAULT_LIMIT = 1000` | 低 | **保留**。合理的防御性上限 |
| `agent/core/loop_guards.py:174-175` | `_count_business_artifacts` 捕获 Exception 返回 0 | 低 | **保留**。守卫函数中 0 是安全的默认值 |

---

## 四、兼容旧格式/旧行为代码

### 4.1 格式适配器

| 位置 | 兼容目标 | 代码模式 | 上游调用方 | 移除成本 | 风险等级 |
|------|----------|----------|------------|----------|----------|
| `validator/templates/crud.py:94-242, 296-511` | **模板格式三向转换**（"新格式" v1.0 ↔ "旧格式" flat ↔ "Canvas 格式" 前端） | 手写双向转换器，含 `CATEGORY_KEY_MAP` 字段映射表和语义转换（如将标题元素折叠至 `main_text`） | `templates/preview.py:63`，标注引擎 | **高** | **高** |
| `docparse/parsers/ocr/ppstructure.py:100-122, 425-447` | **PPStructureV3 旧版 OCR API 响应格式**（`overall_ocr_res` 嵌套结构） | `_get_ocr_arrays()` 中检测 `overall_ocr_res` 分支；`_parse_blocks()` 仅从旧 `parsing_res_list` 提取 | 仅内部（`PPStructureAdapter.parse_response()`） | **中** | **低-中** |
| `agent/artifacts/models.py:720-724` | **旧版 `$ref:tool:n` artifact 引用格式** | `parse_artifact_ref()` 兼容新旧两种格式；`model.py:30` 中的 `_REF_ARG_PATTERN` 也匹配 `$ref:` | 持久化的 artifact 存储、进行中的 Agent 会话 | **中** | **中** |
| `plugin/protocol.py:72-80` | **旧版扁平流式事件格式**（无 `kind` 字段） | `JSONRPCStreamChunk` 上有 6 个可选字段默认 `None`；`proxies.py:234-264` 中的代理流循环分支于 `chunk.kind is not None` | 使用旧流式格式的插件 | **高** | **中** |

### 4.2 废弃/弃用路径

| 位置 | 兼容目标 | 代码模式 | 上游调用方 | 移除成本 | 风险等级 |
|------|----------|----------|------------|----------|----------|
| `plugin/proxies.py:70-72` | 旧 `input_contract`/`output_contract` 属性 | 设为 `None` 以防止 AttributeError（注释："Legacy compat — kept as None so dual-path code doesn't double-fire"） | 任何访问 `tool.input_contract` 的代码 | **低** | **低** |
| `agent/agents/subagent/tool.py:244` | 旧文本拼接任务路径 | 注释标记为 deprecated（"旧路径：文本拼接 task"）；仍为活跃实现 | 子代理分发逻辑 | **中** | **中** |
| `dedump/dedump.py:1-6` | `dedump` 模块名是误称（代码实现抄袭检测而非数据去重） | 模块文档字符串确认命名问题 | 任何 `import src.dedump` 的代码 | **低** | **低-中** |
| `agent/artifacts/projectors.py:410-451` | 标记为 `"deprecated"` 稳定级别的 Projector | `ProjectorStability` 类型包括 `"deprecated"`；`resolver.py:169` 自动跳过，除非 `policy.allow_deprecated=True` | 无（需要显式选择启用） | **中** | **低** |

### 4.3 向后兼容导出

| 位置 | 兼容目标 | 代码模式 | 上游调用方 | 移除成本 | 风险等级 |
|------|----------|----------|------------|----------|----------|
| `docparse/parsers/calibration.py:55-59` | 已移至 `docmodels.constants` 的常量 | Re-export `STANDARD_MARGINS`、`STANDARD_FIRST_INDENT` | 从 `calibration` 导入的旧导入者 | **低** | **低** |
| `docparse/parsers/scanned/__init__.py:54-66` | 子模块中函数的私有别名测试访问 | 10 个私有别名（如 `_prepare_images`、`_resize_images_for_ocr`） | 测试夹具和外部消费者 | **低** | **低** |
| `plugin/protocol.py:11-20` | 错误码常量已移至 `plugin/sdk/protocol.py` | Re-export `PARSE_ERROR`、`METHOD_NOT_FOUND` 等 | 从 `plugin.protocol` 导入的旧导入者 | **低** | **低** |

### 4.4 可选依赖守卫

| 位置 | 兼容目标 | 代码模式 | 移除成本 | 风险等级 |
|------|----------|----------|----------|----------|
| `doccorrector/corrector.py:139-159` | 无 torch 时 pycorrector 不可用 | `try/except ImportError: pass`，返回空列表 | **低** | **低** |
| `dedump/dedump.py:132-135` | 无 datasketch 时 MinHash LSH 不可用 | `try/except ImportError: return []`，回退到 O(N) 穷举搜索 | **低** | **低** |
| `agent/core/loop_hints.py:16-22` | Projection 系统——始终存在的同项目包 | `try/except ImportError` 守卫导入但当模块缺失时静默跳过 hints | **低** | **低** |

### 4.5 前向兼容机制

| 位置 | 目的 | 状态 |
|------|------|------|
| `plugin/scanner.py:43-52, 151-158` | 插件 API 版本兼容性检查（semver：同主版本，插件次要版本 ≤ 宿主次要版本） | 当前 `HOST_API_VERSION = "1.0"`——无版本漂移 |
| `agent/artifacts/models.py:733-775` | Artifact schema 版本兼容性检查（semver 范围如 `>=1.0,<2.0`） | 无实际 schema 版本漂移 |

### 4.6 Pydantic v1/v2

| 状态 | 详情 |
|------|------|
| **无 Pydantic v1 残留** | 代码库统一使用 Pydantic v2 API：`model_dump()`、`model_validate()`、`model_config = {...}`、`field_validator`、`model_copy()`。未发现 `__get_validators__`、`.dict()`、`Config` 内部类或 `@validator` 装饰器 |

### 4.7 版本判断分支

| 状态 | 详情 |
|------|------|
| **无** | 未发现 `sys.version_info` 比较、`PY310`/`PY311`/`PY312` 常量、`pydantic.VERSION` 检查或 `__version__` 对比 |

---

## 五、综合建议

### 高优先级（立即处理）

1. **`docparse` LLM API 调用增加重试机制**
   - 位置：`docparse/parsers/llm_client.py:309, 388, 551, 634`
   - 4 个调用点均无重试，瞬时网络错误导致整个文档解析失败
   - 建议：添加指数退避重试（如 max_retries=3），使用 `raise ... from exc` 保留异常链

2. **`plugin/client.py` 关键路径增加异常日志**
   - 位置：`plugin/client.py:80`（`_read_loop` 中 `except Exception: break`）
   - 读取循环中任何 `readline()` 异常均被当作 EOF 处理，无法区分真正的 I/O 故障
   - 建议：至少对非预期的异常类型（非 `ValueError`/`EOFError`/连接关闭）记录 WARNING

3. **解决 `content_compliance/` 与 `validator/content_checker.py` 的功能重叠**
   - 两个独立但目的相同的实现（协议 vs Pydantic）
   - 建议：统一为 `content_compliance/` 的协议设计，废弃 `validator/content_checker.py` 中的重复实现

### 中优先级（技术债务清理）

4. **合并 100% 重复的 `_import_plugin_tool()`**
   - 位置：`agent/agents/exporter.py` 和 `agent/agents/parser.py`
   - 建议：提取至 `agent/tools/plugin_loader.py`

5. **消除 `SECTION_FIELDS` 的双重定义**
   - 位置：`dbop/_utils.py`（声称是"单一来源"）和 `validator/format_checker.py`（独立副本）
   - 建议：`validator/format_checker.py` 从 `dbop/_utils.SECTION_FIELDS` 导入

6. **确定 PPStructureV3 旧版 OCR 格式的支持范围**
   - 如果所有 OCR 服务器已迁移至当前格式：删除 `_get_ocr_arrays()` 中的 `overall_ocr_res` 分支、`_parse_blocks()` 中的 `parsing_res_list` 提取以及 4 个遗留测试夹具
   - 如果仍有旧版服务器：为迁移添加截止时间

7. **清理 `$ref:tool:n` 旧版 artifact 引用格式**
   - 确定是否有持久化的 artifact 或进行中的 Agent 会话仍使用旧格式
   - 安排迁移截止时间，移除 `parse_artifact_ref()` 中的旧分支

8. **将 `agent/core/loop_streaming.py` 中的 token 回调日志级别提升至 WARNING**
   - DEBUG 级别的日志在生产环境中不可见，导致流失败被完全忽略

### 低优先级（日常清理）

9. **移除假 `ImportError` 守卫**
   - `agent/core/loop_hints.py:16-22` — 同项目包始终可用，守卫无意义。移除守卫，保留直接导入

10. **清理旧式 typing 导入**
    - 将 `dbop/`、`docmodels/`、`dedump/`、`validator/` 中的 `List`/`Optional`/`Dict`/`Tuple`/`Union` 替换为 `list`/`X | None`/`dict`/`tuple` 原生语法

11. **移除开发脚手架残留**
    - `validator/format_checker.py:487-494` — 将 `if __name__ == "__main__":` 块移至测试夹具
    - `agent/core/loop_streaming.py:10-11` — 移除无意义的 `__main__` 守卫

12. **清理向后兼容导出**
    - `calibration.py:55-59` — 更新导入者使用 `docmodels.constants`
    - `scanned/__init__.py:54-66` — 更新测试导入使用规范子模块路径
    - `plugin/protocol.py:11-20` — 更新导入者使用 `plugin.sdk.protocol`

13. **移除 `plugin/proxies.py:70-72` 中的 `ProxyTool` 旧字段桩**
    - 确认无代码仍访问 `tool.input_contract` 或 `tool.output_contract` 后移除

14. ~~**考虑将 `dedump/` 重命名为 `plagiarism/`**~~ ✅ **已完成 (2026-06-23)**
    - 模块已从 `src/dedump/dedump.py` 迁移至 `src/plagiarism/core.py`
    - `pyproject.toml`、`plugins/plagiarism/tools.py`、README、架构文档均已同步更新

---

## 六、审计元数据

| 字段 | 内容 |
|------|------|
| 框架 | FastAPI + Pydantic v2 + SQLAlchemy async |
| 分析方法 | 静态分析（AST 遍历、grep 模式匹配、导入图构建） |
| 置信度 | **高** — 对于静态可判定项（循环依赖、死代码、重复代码、导入守卫）。**中** — 对于需要运行时行为确认的动态代码路径和可选依赖分支 |
| 需人工确认 | PPStructureV3 OCR 服务器版本分布；`$ref:tool:n` 旧格式的持久化 artifact 数量；模板旧格式和 Canvas 格式的前端消费者现状 |
