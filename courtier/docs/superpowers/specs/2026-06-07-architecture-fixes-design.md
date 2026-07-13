# Architecture & Module Design Fixes — Design Spec

**Date**: 2026-06-07
**Status**: Approved
**Scope**: 10 issues (4 HIGH, 6 MEDIUM) from code review

## Overview

修复 DocAudit 项目中代码审查识别的全部 10 个架构与模块设计问题：

- 4 HIGH: 双轨架构统一、服务层缺失、loop.py 违反 SRP、超大文件
- 6 MEDIUM: Agent/领域模块重叠、插件/技能重叠、内联导入、content_compliance 结构不一致、模块级单例、缺少 DI

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| 架构统一方向 | Agent 优先 | 所有功能统一走 ToolProtocol，调用方无需关心实现来源 |
| 服务层模式 | 简单 async 函数 | 轻量，依赖关系在函数签名中显式声明 |
| loop.py 拆分组织 | 全部在 core/ 下 | 与已有的 loop_streaming.py / loop_audit.py 风格一致 |
| DI 方式 | FastAPI app.state | 不引入新框架，利用现有基础设施 |

## Section 1: 双轨架构统一

### 1.1 为每个领域模块创建 ToolProtocol 适配器

```
src/agent/tools/domain/          # 新目录
├── __init__.py
├── parse_tool.py                # 包装 docparse
├── format_check_tool.py         # 包装 validator
├── content_compliance_tool.py   # 包装 content_compliance
├── text_correction_tool.py      # 包装 doccorrector
├── plagiarism_tool.py           # 包装 dedump
└── annotate_tool.py             # 包装 docannot
```

每个适配器实现 `ToolProtocol`：

```python
class FormatCheckTool:
    name = "format_check"
    description = "直接校验文档格式"
    parameters = {...}  # JSON Schema

    async def execute(self, file_path: str, **kwargs) -> ToolResult:
        from src.validator import compare_document_to_spec
        result = compare_document_to_spec(file_path)
        return ToolResult(success=True, data=result)
```

### 1.2 清理 Agent 子代理中的重复逻辑

确认 `FormatAuditorAgent`、`ContentAuditorAgent` 等子代理使用的 Skill 工具是否与 domain tools 重复：
- 重复 → 子代理直接使用 domain tool
- 不重复（含 LLM 提示逻辑）→ 保留

### 1.3 统一注册

所有 domain tools 在应用启动时注册到 `ToolRegistry`，Agent 编排路径和直接调用路径共享同一注册表。

**Files changed**:
- New: `src/agent/tools/domain/` (6 files)
- Modified: `src/agent/agents/base.py` (Agent.__init__ 注册逻辑)
- Modified: `src/agent/agents/orch.py` (OrchestratorAgent 工具列表)
- Modified: `src/agent/api/app.py` (启动时注册 domain tools)

---

## Section 2: 服务层提取

### 文件结构

```
src/agent/api/
├── routes/                       # 拆分路由文件
│   ├── __init__.py
│   ├── sessions.py               # GET/DELETE /sessions
│   ├── files.py                  # POST /files
│   └── control.py                # POST /pause, /resume, /stop
├── services/                     # 新目录
│   ├── __init__.py
│   ├── session_service.py        # 会话 CRUD
│   ├── agent_service.py          # Agent 构建
│   ├── file_service.py           # 文件上传/校验
│   └── stream_service.py         # SSE 事件生成 + 制品回填
├── app.py                        # FastAPI 工厂
├── sse_adapter.py                # SSE 适配器
├── session_store.py              # 会话存储
├── file_store.py                 # 文件存储
└── models.py                     # Pydantic 模型
```

### 服务函数签名

```python
# services/agent_service.py
async def build_audit_agent(
    settings: Settings,
    tool_registry: ToolRegistry,
) -> tuple[Agent, ContextManager, str]: ...

async def build_chat_agent(
    settings: Settings,
) -> tuple[Agent, ContextManager, str]: ...

# services/stream_service.py
async def generate_sse_stream(
    agent: Agent,
    session_id: str,
    task: str,
    file_path: str | None,
    prior_state: AgentState | None,
    context_manager: ContextManager,
    session_store: SessionStore,
    artifact_store: ArtifactStore,
    pause_event: asyncio.Event,
    active_tasks: dict,
    audit_logger: AuditLogger | None,
) -> AsyncGenerator[str, None]: ...

# services/session_service.py
def list_sessions(store: SessionStore): ...
def get_session(store: SessionStore, session_id: str): ...
def create_session(store: SessionStore, **kwargs): ...
def delete_session(store: SessionStore, session_id: str): ...

# services/file_service.py
async def upload_file(
    file: UploadFile,
    settings: Settings,
    file_store: FileStore,
) -> dict: ...
```

### routes 变为薄层

```python
# routes/sessions.py
router = APIRouter(prefix="/api")

@router.get("/sessions")
async def handle_sessions(request: Request, ...):
    store = request.app.state.session_store
    if task is None:
        return list_sessions(store)
    return StreamingResponse(
        generate_sse_stream(...),
        media_type="text/event-stream",
    )
```

**Files changed**:
- New: `src/agent/api/services/` (4 files)
- New: `src/agent/api/routes/` (3 files)
- Deleted: `src/agent/api/routes.py` (595 lines → replaced by routes/)

---

## Section 3: loop.py 拆分

### 拆分后结构

```
src/agent/core/
├── loop.py              # 核心循环 agent_loop()，~200行
├── loop_guards.py       # 循环护栏函数，~200行
├── loop_hints.py        # 制品就绪/阻塞提示，~200行
├── loop_utils.py        # 工具结果摘要/格式化，~100行
├── loop_streaming.py    # 已有
├── loop_audit.py        # 已有
└── ...
```

### 各文件职责

**loop.py** — 仅 `agent_loop()`:
```
while not terminal:
  1. context budget compaction
  2. pre_think hook
  3. THINK: model.generate()
  4. 推理循环检测 → loop_guards.detect_reasoning_loop()
  5. GATE: permission check
  6. ACT: tool_registry.execute()
  7. OBSERVE: add results to state
  8. 探索循环检测 → loop_guards.check_explore_loop()
  9. 制品进度检测 → loop_guards.check_business_artifact_progress()
  10. 终端就绪提示 → loop_hints.check_and_inject_hints()
  11. post_observe hook
  12. micro-compact
```

**loop_guards.py**:
- `check_explore_loop()` — 空结果/重复调用/过度探索 三重检测
- `detect_reasoning_loop()` — 推理循环检测
- `check_business_artifact_progress()` — 业务制品无进展检测
- `update_null_tracking()` / `update_tool_call_history()` / `update_exploratory_tracking()` — 追踪

**loop_hints.py**（合并当前 4 个重复函数）:
- `check_and_inject_hints()` — 统一入口
- 内部共享 resolver 实例，消除重复 import 和 resolver 创建

**loop_utils.py** — 纯工具:
- `tool_result_summary()` / `fmt_size()` / `similarity()` / `normalize_args_for_dedup()`

**Files changed**:
- Modified: `src/agent/core/loop.py` (858 → ~200 lines)
- New: `src/agent/core/loop_guards.py`
- New: `src/agent/core/loop_hints.py`
- New: `src/agent/core/loop_utils.py`

---

## Section 4: 超大文件拆分

### 4.1 scanned_parser.py（1003行 → scanned/ 包）

```
src/docparse/parsers/
├── scanned/
│   ├── __init__.py              # ScannedParser 公开 API
│   ├── preprocessor.py          # 图像预处理
│   ├── ocr_engine.py            # OCR 调用
│   ├── spacing.py               # 间距计算（合并现有 spacing.py 逻辑）
│   ├── font_detector.py         # 字体检测（合并现有 font_detector.py 逻辑）
│   ├── normalizer.py            # 坐标归一化
│   ├── llm_completer.py         # LLM 结构补全
│   └── structure.py             # 版面识别 + Document 组装
└── scanned_parser.py            # 删除 → subpackage
```

### 4.2 template_service.py（880行 → templates/ 包）

```
src/validator/
├── templates/
│   ├── __init__.py
│   ├── crud.py                  # 模板 CRUD
│   ├── skeleton.py              # 骨架生成、变体管理
│   └── preview.py               # 预览渲染
└── template_service.py          # 删除 → subpackage
```

### 4.3 subagent.py（781行 → subagent/ 包）

```
src/agent/agents/
├── subagent/
│   ├── __init__.py
│   ├── runner.py                # SubAgentRunner
│   ├── tool.py                  # _SubAgentTool
│   └── config.py                # SubAgentConfig, FailureStrategy, CallbackHolder
└── subagent.py                  # 删除 → subpackage
```

**Files changed**:
- New: ~10 files across 3 subpackages
- Deleted: 3 oversized files
- Backward compatible via `__init__.py` re-exports

---

## Section 5: MEDIUM 问题

### 5.1 插件与技能系统分工

明确边界：
- `skills/` — 内置能力，Python 模块直接加载，职责：LLM 提示注入 + 工具定义
- `plugin/` — 第三方扩展，子进程 JSON-RPC 隔离，职责：外部进程管理
- 两者统一注册到 `ToolRegistry`，调用方无需关心来源

### 5.2 消除内联导入

loop.py 拆分后自然消除；subagent.py 拆分后自然消除；routes.py 提取服务层后自然消除。剩余个别内联导入逐文件处理。

### 5.3 content_compliance 结构对齐

- 测试: `src/content_compliance/tests/` → `tests/content_compliance/`
- `_auto_register()` → 显式 `init_checkers()`，应用启动时调用
- `pyproject.toml` 移除 `exclude = ["src/content_compliance/tests"]`

### 5.4 移除模块级单例

```python
# app.py — 删除模块级 app 实例
# 改为 uvicorn 工厂模式:
# uvicorn src.agent.api.app:create_app --factory
```

### 5.5 统一依赖注入

利用 FastAPI `app.state` 作为 DI 容器：

```python
# app.py create_app()
app.state.settings = Settings()
app.state.session_store = SessionStore(...)
app.state.file_store = FileStore(...)
app.state.tool_registry = ToolRegistry()
# 启动时注册所有 domain tools
```

### 5.6 Agent/领域模块重叠

随 Section 1 的双轨统一自然解决——领域模块功能暴露为 ToolProtocol 后，Agent 子代理直接使用这些工具，不再通过 Skill 系统重复包装。

---

## Implementation Order

按依赖关系排列：

| Phase | Changes | Depends On |
|-------|---------|------------|
| Phase 1 | Section 5.4 单例移除 + Section 5.5 DI 容器 | 无 |
| Phase 2 | Section 3 loop.py 拆分 | 无 |
| Phase 3 | Section 4 超大文件拆分 | 无 |
| Phase 4 | Section 5.3 content_compliance 对齐 | 无 |
| Phase 5 | Section 1 双轨统一（domain tools + 清理子代理） | Phase 1 (DI) |
| Phase 6 | Section 2 服务层提取 | Phase 1 (DI), Phase 5 (domain tools) |
| Phase 7 | Section 5.1 插件/技能分工 + Section 5.2 内联导入 | 前 6 个 Phase |

Phase 1-4 可并行执行（互不依赖）。
Phase 5-7 需顺序执行。

---

## Testing Strategy

每个 Phase 完成后：
1. 运行现有测试套件确认无回归
2. 新增模块的单元测试（目标 80%+ 覆盖率）
3. 类型检查通过

## Risks

- **向后兼容**: 通过 `__init__.py` 重导出保持公开 API 不变
- **循环导入**: 拆分文件时注意 import 顺序，必要时使用 TYPE_CHECKING
- **Agent 行为变化**: domain tools 的行为需与原有 Skill 工具行为一致
