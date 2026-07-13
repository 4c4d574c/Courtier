# DocAudit Agent 代码审查修复设计

**日期**: 2026-06-07
**范围**: 修复代码审查发现的 18 个问题（CRITICAL/HIGH/MEDIUM/LOW）
**策略**: 按批次顺序修复（A → B → C → D），每批独立测试验证

---

## 概述

本次修复针对代码审查中发现的 18 个问题，按影响范围和依赖关系分为四个批次：

- **批次 A（安全 + 配置 + 不可变性紧急修复）**: 问题 1-3
- **批次 B（架构债务）**: 问题 4, 7, 13
- **批次 C（代码质量）**: 问题 5, 8, 10, 14
- **批次 D（可选优化）**: 问题 6, 11-12, 15-18

每批次完成后运行完整测试套件验证，通过后再进入下一批次。

---

## 批次 A：安全 + 配置 + 不可变性紧急修复

### A1. CORS 配置收紧（问题 1）

**目标**: 消除生产环境 CORS 宽松配置的安全风险。

**变更文件**:
- `src/config.py`
- `src/agent/api/app.py`
- `.env.example`

**设计细节**:

`Settings` 新增字段:
```python
cors_origins: list[str] = Field(
    default=["http://localhost:5173"],
    description="允许的 CORS 来源列表"
)
cors_allow_credentials: bool = Field(
    default=False,
    description="是否允许携带凭证的跨域请求"
)
```

`app.py` 中改为:
```python
allow_origins=settings.cors_origins,
allow_credentials=settings.cors_allow_credentials and settings.cors_origins != ["*"],
```

兼容性: 现有 `.env` 未配置 `CORS_ORIGINS` 时，默认使用 localhost，不会破坏开发环境。

### A2. 统一配置管理（问题 2）

**目标**: 消除分散的 `load_dotenv()` 调用，统一使用 `Settings` 类。

**变更文件**:
- `src/config.py` — 新增 CEC/DOCPARSE 相关字段
- `src/doccorrector/corrector.py` — 移除模块级 `load_dotenv()` 和全局常量
- `src/docparse/parsers/base.py` — 移除 `load_dotenv()`
- `src/docparse/__init__.py` — 移除 `load_dotenv()`

**设计细节**:

`Settings` 新增字段:
```python
cec_api_base: str = ""
cec_api_key: str = ""
cec_model_name: str = "ChineseErrorCorrector3-4B"
cec_max_length: int = 16383
cec_user_dict: str = ""
cec_allowed_patterns: str = "看一看,想一想,试一试,人人,一一"
```

`doccorrector/corrector.py`:
- 移除顶部 `load_dotenv()` 和 `API_BASE`, `API_KEY` 等模块级常量
- `OpenAITextCorrectInfer.__init__()` 接收 `settings: Settings | None = None` 参数
- `ErrorCorrect.__init__()` 同样接收 `settings` 参数
- 默认值通过 `Settings()` 获取

`docparse/parsers/base.py`:
- 移除 `load_dotenv()`
- `ParserConfig.from_env()` 改为 `ParserConfig.from_settings(settings: Settings)`

### A3. 修复 Message 不可变性（问题 3）

**目标**: 修复 `Message.tool_calls` 使用可变 `list` 破坏 `frozen=True` 语义的问题。

**变更文件**:
- `src/agent/core/state.py`
- `src/agent/core/model.py`
- `src/agent/core/loop.py`

**设计细节**:

```python
# 变更前
@dataclass(frozen=True)
class Message:
    tool_calls: list[ToolCall] | None = None

# 变更后
@dataclass(frozen=True)
class Message:
    tool_calls: tuple[ToolCall, ...] | None = None
```

- `AgentState.add_thought()` 中: `response.tool_calls` 需 `tuple(response.tool_calls)` 转换
- 检查所有 `Message(..., tool_calls=...)` 构造处，确保传入 tuple

---

## 批次 B：架构债务修复

### B1. 简化三重缓存系统（问题 4）

**目标**: 让 `ArtifactStore` 成为唯一的引用管理入口，`CacheStore` 退化为磁盘持久化后端，`ContextManager` 只负责上下文预算管理。

**现状问题**:
- `ContextManager` 管理 Layer 1/2/3 上下文压缩，内含 `CacheStore`（`_cache`）
- `ToolRegistry.execute()` 接收 `cache_store` 参数，用于 ref 解析和结果持久化
- `ArtifactStore` 管理类型化工件，与 `ContextManager` 通过 `ref_map` 手动同步
- `routes.py` 需要 `_rehydrate_artifact_store()` 来桥接多轮会话

**变更文件**:
- `src/agent/artifacts/store.py`
- `src/agent/core/cache_store.py`
- `src/agent/core/context_manager.py`
- `src/agent/tools/registry.py`
- `src/agent/core/loop.py`
- `src/agent/api/routes.py`

**设计细节**:

`ArtifactStore` 新增方法:
```python
def resolve_ref(self, ref_id: str) -> Any:
    """从缓存文件加载 $ref 数据"""
    artifact = self.get(ref_id)
    if artifact:
        return artifact.data
    # fallback: 从 ref_map 加载
    ...

def persist_large_output(self, tool_name: str, data: Any) -> str:
    """替代 ContextManager.persist_large_output()"""
    ...
```

兼容性: `ContextManager` 保留 `resolve_refs()` 和 `persist_large_output()` 作为代理方法，但内部委托给 `ArtifactStore`。

### B2. 解除 ToolRegistry 与 artifacts 的循环依赖（问题 7）

**目标**: 将合同绑定逻辑从 `ToolRegistry` 中提取，消除与 `artifacts` 模块的双向耦合。

**变更文件**:
- `src/agent/tools/registry.py`
- 新增 `src/agent/artifacts/binder.py`

**设计细节**:

新增 `ContractBinder`:
```python
class ContractBinder:
    """协调 ToolRegistry 和 ArtifactStore 的合同绑定"""
    def __init__(self, registry: ToolRegistry, artifact_store: ArtifactStore):
        ...

    def bind_tool_inputs(self, tool_name: str, kwargs: dict) -> ToolResult:
        ...

    def register_tool_output(self, tool_name: str, result: ToolResult) -> None:
        ...
```

`ToolRegistry.execute()` 简化为只负责工具发现和执行，合同绑定通过 `ContractBinder` 在外层处理。

### B3. 数据库迁移改用 Alembic（问题 13）

**目标**: 用 Alembic 替代内嵌的 `_migrate_missing_columns()`。

**变更文件**:
- `src/dbop/db_manager.py`
- 新增 `alembic/` 目录和迁移文件
- `pyproject.toml`

**设计细节**:
- 初始化 Alembic: `alembic init alembic`
- 将现有 `_migrate_missing_columns` 中的列添加操作转换为 Alembic 迁移脚本
- `AsyncDatabase.create_all()` 改为调用 `alembic upgrade head`

---

## 批次 C：代码质量修复

### C1. 消除 SubAgentRunner 代码重复（问题 5）

**目标**: 提取公共重试逻辑，消除 `dispatch()` 和 `dispatch_structured()` 的重复代码。

**变更文件**: `src/agent/agents/subagent.py`

**设计细节**:

提取 `_execute_with_retry()` 私有方法:
```python
async def _execute_with_retry(
    self,
    config: SubAgentConfig,
    run_fn: Callable[[], Awaitable[AgentResult]],
) -> ToolResult:
    attempts = 1 + config.max_retries
    for attempt in range(attempts):
        try:
            result = await run_fn()
            if result.status == "completed":
                return ToolResult(success=True, data=_extract_result_data(result), ...)
            # 失败处理...
        except asyncio.TimeoutError:
            ...
        except Exception:
            ...
```

### C2. 提取路由层 Agent 构建公共函数（问题 8）

**目标**: 消除 `_build_api_agent()` 和 `_build_chat_agent()` 中的重复代码。

**变更文件**: `src/agent/api/routes.py`

**设计细节**:

提取公共函数:
```python
def _build_model_client(settings: Settings) -> OpenAIModelClient:
    return OpenAIModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens if settings.llm_max_tokens > 0 else None,
        extra_body=settings.llm_extra_body,
    )
```

### C3. 拆分超大文件（问题 10）

**目标**: 将超过 800 行的文件拆分为职责单一的小文件。

**拆分策略**:

| 原文件 | 拆分后 | 职责 | 预估行数 |
|--------|--------|------|---------|
| `loop.py` (940行) | `loop.py` | 主循环骨架 | ~250行 |
| | `loop_guards.py` | 探索循环守卫、推理循环检测、终端工具检测 | ~350行 |
| | `loop_streaming.py` | 流式生成回退、token 分割 | ~120行 |
| | `loop_audit.py` | 审计日志写入辅助 | ~80行 |
| `subagent.py` (813行) | `subagent_runner.py` | `SubAgentRunner` 类 | ~300行 |
| | `subagent_tool.py` | `_SubAgentTool` 适配器 | ~250行 |
| | `subagent_input.py` | `input_models` 相关解析 | ~150行 |

依赖方向: `loop.py` → `loop_guards.py`, `loop_streaming.py`, `loop_audit.py`

### C4. 标记外部依赖测试（问题 14）

**目标**: 将需要外部服务的测试标记为 integration，避免默认测试失败。

**变更文件**:
- `tests/scripts/test_document_es.py`
- `tests/scripts/test_resources_*.py`

**设计细节**:
- 在文件顶部添加: `pytestmark = pytest.mark.integration`
- CI/CD 中默认运行 `pytest -m "not integration"`
- 集成测试单独阶段运行

---

## 批次 D：可选优化

### D1. 查重算法 MinHash 预筛选（问题 6）

**目标**: 当文档库规模较大时，使用 MinHash 降低查重计算复杂度。

**变更文件**: `src/dedump/dedump.py`, `pyproject.toml`

**设计细节**:
- 当 `len(library_docs) > 100` 时启用 MinHash 预筛选
- 使用 `datasketch` 库（需添加到依赖）
- 预筛选后仅对 Top-K（20 篇）最相似文档进行精确 `SequenceMatcher` 比对
- `<= 100` 时保持现有精确算法

复杂度对比:
- 当前: O(N² × M²)，N=500 时约 25 万次完整比对
- 优化后: O(N × M) 构建签名 + O(N log N) 排序 + O(K × M²) 精确比对

### D2. 封装 Agent.run() 参数（问题 11）

**目标**: 减少 `Agent.run()` 的参数数量，提高类型安全性。

**变更文件**: `src/agent/agents/base.py`

**设计细节**:

```python
@dataclass(frozen=True)
class RunOptions:
    task: str | None = None
    input: Any | None = None
    context: dict[str, str] | None = None
    on_step: Callable[[str, str], Awaitable[None]] | None = None
    on_token: Callable[[str], Awaitable[None]] | None = None
    on_content_token: Callable[[str], Awaitable[None]] | None = None
    on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None = None
    context_manager: ContextManager | None = None
    state: AgentState | None = None
    audit_logger: AuditLogger | None = None
    artifact_store: ArtifactStore | None = None
```

保持向后兼容: `Agent.run(self, task=None, ..., options=None)`。

### D3. 拆分 ToolProtocol（问题 12）

**目标**: 将庞大的 `ToolProtocol` 拆分为基础协议和扩展 mixin。

**变更文件**: `src/agent/tools/protocol.py`

**设计细节**:

```python
@runtime_checkable
class ToolProtocol(Protocol):
    name: str
    description: str
    parameters: dict
    async def execute(self, **kwargs: Any) -> ToolResult: ...

@runtime_checkable
class ToolWithContracts(Protocol):
    input_contract: Any | None
    output_contract: Any | None

@runtime_checkable
class ToolWithRuntimePolicy(Protocol):
    runtime_policy: Any | None
    skip_persist: bool
```

### D4. 修复 orch.py 的 copy.deepcopy（问题 15）

**目标**: 用浅拷贝替代 `copy.deepcopy`，遵循不可变性原则。

**变更文件**: `src/agent/agents/orch.py`

**设计细节**:
```python
# 变更前
return copy.deepcopy(self._audit_results)

# 变更后
return {k: v for k, v in self._audit_results.items()}
```

`audit_results` 的值是基本 JSON 可序列化类型，浅拷贝足够。

### D5-D7. 其他问题

- **D5（问题 18）**: 提取 loop.py 守卫逻辑 — 已在批次 C3 中覆盖
- **D6（问题 16）**: `content_compliance` 模块评估 — 暂不改动，需与产品确认
- **D7（问题 17）**: `dedump` 模块重命名 — 批次 D 中仅添加 `DeprecationWarning`

---

## 实施顺序与验证策略

```
批次 A → 运行测试 → 提交 → 批次 B → 运行测试 → 提交 → 批次 C → 运行测试 → 提交 → 批次 D → 运行测试 → 提交
```

每批次完成后的验证:
1. `uv run pytest -m "not integration"` — 确保单元测试通过
2. `uv run pytest tests/agent/` — 确保 Agent 核心测试通过
3. `git diff --stat` — 确认改动范围符合预期

---

## 风险与回滚策略

| 批次 | 主要风险 | 回滚策略 |
|------|---------|---------|
| A | 配置字段变更影响现有 `.env` | `.env.example` 提供完整默认值，缺失字段有 fallback |
| B | ArtifactStore 重构影响数据一致性 | 保留 `ContextManager` 代理方法作为兼容层 |
| C | 文件拆分导致导入错误 | 拆分后在原文件位置保留兼容性导入（如 `from .loop_guards import *`） |
| D | MinHash 引入新依赖 | `datasketch` 为纯 Python 库，无系统依赖 |

---

## 附录：问题清单映射

| 编号 | 问题 | 批次 | 优先级 |
|------|------|------|--------|
| 1 | CORS 配置过于宽松 | A | CRITICAL |
| 2 | 分散的 load_dotenv() | A | CRITICAL |
| 3 | Message 不可变性破坏 | A | CRITICAL |
| 4 | 三重缓存系统耦合 | B | HIGH |
| 5 | SubAgentRunner 代码重复 | C | HIGH |
| 6 | 查重算法无优化 | D | HIGH |
| 7 | ToolRegistry 循环依赖 | B | HIGH |
| 8 | 路由层 Agent 构建重复 | C | HIGH |
| 10 | 超大文件 | C | MEDIUM |
| 11 | Agent.run() 参数过多 | D | MEDIUM |
| 12 | ToolProtocol 过于庞大 | D | MEDIUM |
| 13 | 数据库迁移内嵌代码 | B | MEDIUM |
| 14 | 测试失败未标记 | C | MEDIUM |
| 15 | copy.deepcopy 使用 | D | MEDIUM |
| 16 | content_compliance 集成度低 | D | LOW |
| 17 | dedump 模块名 legacy | D | LOW |
| 18 | loop.py 守卫逻辑混杂 | C/D | LOW |
