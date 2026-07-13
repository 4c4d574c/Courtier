# Artifact 合约系统清理与完善 — 设计文档

**日期:** 2026-06-09
**状态:** 待实现

## 1. 问题概述

审查发现 artifact 模块与插件工具之间的交互存在以下问题：

1. **`input_fields` 声明不完整** — `audit_writing_style` 等工具未声明 `input_fields`，artifact 自动绑定系统忽略它们
2. **`_UPSTREAM_PRODUCERS` 映射不完整** — 静态字典缺少多种 artifact type，且需手动维护
3. **`InputField` / `ContractField` 双重类层次** — 两套并行字段定义类，`registry.py` 和 `loop_hints.py` 各自转换，逻辑不一致
4. **注册时无验证** — 工具声明 `input_fields` 的 artifact type 是否有生产者，只在运行时发现

## 2. 设计目标

- 删除冗余类型，统一为 `InputField` / `OutputField` / `ToolContract`
- 补充缺失的 `input_fields` 声明
- `_UPSTREAM_PRODUCERS` 改为从 `ToolRegistry` + `ProjectorRegistry` 动态推导
- `ToolRegistry.register()` 时做静态可达性警告

## 3. 类型统一

### 3.1 目标类型定义

`InputField` 保持为工具类属性的声明方式（`input_fields: tuple[InputField, ...]`）。
`output_artifact_type` 保持为简单的 `str | None` 属性，不需要 `OutputField`。
`ToolContract` 是内部传递用容器，不是工具类属性。

```python
# src/agent/artifacts/models.py

@dataclass(frozen=True)
class InputField:
    """工具输入字段的 artifact 合约声明。作为工具类属性 input_fields 使用。"""
    name: str
    artifact_type: str
    materialize_as: str | None = None  # None 时从 artifact_type 推导
    required: bool = True
    constraints: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class RuntimePolicy:
    max_calls: int | None = None
    max_consecutive: int | None = None
```

工具输出保持现有的 `output_artifact_type: str | None` 类属性，不需要额外的 `OutputField` 包装 — 当前 `OutputArtifactContract` 的额外字段（`role`, `subject`, `persist`, `llm_visible`）在生产代码中始终使用默认值。

### 3.2 删除的类型

- `ContractField` — 由 `InputField` 替代（`InputField` 新增 `required` 和 `constraints`）
- `ToolInputContract` — 由 `tuple[InputField, ...]` 替代
- `OutputArtifactContract` — 删除，`output_artifact_type: str` 字符串已足够
- `ToolOutputContract` — 删除，无实际使用者
- `ToolRuntimePolicy` — 由 `RuntimePolicy` 替代（后者已在 `proxies.py` 中使用）

### 3.3 受影响的调用点

| 文件 | 变更 |
|------|------|
| `models.py` | 删除旧类型，`InputField` 加 `required`/`constraints` 字段，新增 `build_contract_from_input_fields()` |
| `resolver.py` | `resolve()` 参数从 `ToolInputContract` 改为 `tuple[InputField, ...]` |
| `executor.py` | `validate_materialized_value()` 参数从 `ContractField` 改为 `InputField` |
| `binder.py` | `bind_tool_inputs()` 参数从 `ToolInputContract` 改为 `tuple[InputField, ...]` |
| `registry.py` | 移除 `ContractField` 临时构建逻辑，复用 `build_contract_from_input_fields()`；`_check_runtime_policy()` 统一用 `RuntimePolicy` |
| `loop_hints.py` | 移除 `_get_effective_contract()` 的转换逻辑，复用工厂函数 |
| `proxies.py` | 不变（已在用 `InputField` / `RuntimePolicy`） |

### 3.4 工厂函数

在 `models.py` 中新增单一工厂函数，替代 `registry.py` 和 `loop_hints.py` 中重复的转换逻辑：

```python
def build_contract_from_input_fields(
    tool_name: str,
    input_fields: tuple[InputField, ...],
) -> tuple[InputField, ...]:
    """标准化 input_fields，补全 materialize_as 默认值."""
    return tuple(
        InputField(
            name=f.name,
            artifact_type=f.artifact_type,
            materialize_as=f.materialize_as or f.artifact_type,
            required=f.required,
            constraints=f.constraints,
        )
        for f in input_fields
    )
```

## 4. 补充 input_fields 声明

### 4.1 `audit_writing_style` (plugins/style_audit/tools.py)

```python
from src.agent.artifacts.models import InputField

class AuditWritingStyleTool:
    input_fields: tuple[InputField, ...] = (
        InputField(name="text", artifact_type="core.plain_text", materialize_as="string"),
    )
```

`doc_type` 和 `subtype` 由 LLM 显式提供，不通过 artifact 绑定。

### 4.2 其他工具

| 工具 | 状态 |
|------|------|
| `audit_format` | 已有 `document: parsed_document` |
| `correct_text` | 已有 `texts: paragraph_list` |
| `audit_content` | 已有 `paragraphs: paragraph_list` |
| `detect_plagiarism` | 已有 `new_doc: plain_text`, `library_docs: text_collection` |
| `parse_document` | 已有 `output_artifact_type: parsed_document` |
| `search_documents` | 已有 `output_artifact_type: search_results` |
| `detect_document_type` | 不声明 — title 很短，LLM 直接传入 |
| `load_template` | 不声明 — 不需要 artifact |
| `load_format_spec` | 不声明 — 纯查询工具 |
| `list_*` 工具 | 不声明 — 无数据依赖 |

## 5. 上游生产者自动推导

### 5.1 当前状态

```python
_UPSTREAM_PRODUCERS: dict[str, str] = {
    "docaudit.parsed_document": "parse_document",
    "docaudit.search_results": "search_documents",
    "core.plain_text": "parse_document",
    "core.text_collection": "search_documents",
    "docaudit.reference_text_list": "search_documents",
}
```

缺失：`docaudit.paragraph_list`、`docaudit.plagiarism_report`、`docaudit.document_metadata` 等。

### 5.2 目标实现

```python
# models.py

def derive_upstream_producers(
    tools: list[Any],  # list[ToolProtocol]
    projector_registry: "ProjectorRegistry",
) -> dict[str, list[str]]:
    """从工具列表和投影注册表动态推导上游生产者映射。
    
    直接生产者：工具的 output_artifact_type → tool.name
    间接生产者：通过投影图从已知生产者反向推导
    """
    producers: dict[str, list[str]] = {}
    
    # 直接生产者
    for tool in tools:
        oat = getattr(tool, "output_artifact_type", None)
        if oat:
            producers.setdefault(oat, []).append(tool.name)
    
    # 间接生产者：对于不能直接生产的类型，查找投影路径
    all_produced = set(producers.keys())
    for target_type in _collect_all_required_types(tools, projector_registry):
        if target_type in producers:
            continue
        # 查找哪些已生产类型可以投影到 target_type
        for produced_type in all_produced:
            if _has_projection_path(produced_type, target_type, projector_registry):
                producers.setdefault(target_type, []).extend(producers[produced_type])
    
    return producers

# 替代原有的 upstream_producer_for()
def upstream_producer_for(artifact_type: str) -> str | None:
    """保留兼容接口."""
    return None  # 由调用方使用 derive_upstream_producers 替代
```

## 6. 注册时静态可达性验证

### 6.1 ToolRegistry 变更

```python
class ToolRegistry:
    def __init__(
        self,
        policy: ProjectionPolicy | None = None,
        projector_registry: ProjectorRegistry | None = None,
    ) -> None:
        self._tools: dict[str, ToolProtocol] = {}
        self._policy = policy or ProjectionPolicy()
        self._projector_registry = projector_registry or create_default_projector_registry()
        self._producer_cache: dict[str, list[str]] | None = None
        # ...

    def register(self, tool: ToolProtocol, force: bool = False) -> None:
        if tool.name in self._tools and not force:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        if tool.name in self._tools:
            del self._tools[tool.name]
        
        # 静态可达性警告
        warnings = self._validate_contract_reachability(tool)
        for w in warnings:
            logger.warning("Tool '%s': %s", tool.name, w)
        
        self._tools[tool.name] = tool
        self._producer_cache = None  # 缓存失效

    def _validate_contract_reachability(self, tool: ToolProtocol) -> list[str]:
        """检查 input_fields 所需类型是否能被现有系统满足."""
        input_fields = getattr(tool, "input_fields", None)
        if not input_fields:
            return []
        
        producers = self._get_producers()
        warnings = []
        for f in input_fields:
            if f.artifact_type not in producers:
                warnings.append(
                    f"input field '{f.name}' requires artifact type "
                    f"'{f.artifact_type}' which has no known producer"
                )
        return warnings
```

### 6.2 行为

- **不阻塞注册** — 只发 WARNING 日志。原因：生产者工具可能后注册，或某些参数 LLM 直接提供
- **生产者缓存** — `_get_producers()` 延迟计算并缓存，注册/注销时失效

## 7. 错误处理

- `resolver.py`: 当 field 无法解析时，`suggested_actions` 使用动态推导的 `producers[artifact_type]` 列表（而非单一工具）
- `loop_hints.py`: `_build_blocked_tools_hints()` 复用 `derive_upstream_producers()` 的结果
- `registry.py`: `_bind_contract_arguments()` 自动绑定失败时返回的 `ToolResult.error` 包含具体缺失字段和可用生产者

## 8. 测试策略

| 测试 | 内容 |
|------|------|
| 单元测试 | `build_contract_from_input_fields()` 补全默认值 |
| 单元测试 | `derive_upstream_producers()` 正确推导直接/间接生产者 |
| 单元测试 | `InputField`/`OutputField`/`ToolContract` 创建和比较 |
| 集成测试 | `ToolRegistry.register()` 对缺失合约的工具发出 warning |
| 集成测试 | `audit_writing_style` 的 `text` 字段可从 `core.plain_text` 自动绑定 |
| 回归测试 | 所有现有 `test_plugin/*` 和 `test_agent/*` 测试通过 |

## 9. 实现顺序

1. `models.py` — 删除旧类型、新增类型、新增工厂函数、新增 `derive_upstream_producers()`
2. `executor.py` — `validate_materialized_value()` 适配 `InputField`
3. `resolver.py` — 适配新类型签名
4. `binder.py` — 适配新类型签名
5. `registry.py` — 移除 ContractField 构建、新增验证、注入 ProjectorRegistry
6. `loop_hints.py` — 移除重复转换、复用工厂函数
7. `plugins/style_audit/tools.py` — 补充 `input_fields`
8. 运行全部测试，修复回归
