# Artifact 合约系统清理与完善 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 ContractField/ToolInputContract 等冗余类型，统一使用 InputField；补充 audit_writing_style 的 input_fields；改为动态推导上游生产者；添加注册时验证。

**Architecture:** InputField（dataclass）替代 ContractField（pydantic BaseModel）承担全部字段合约声明职责。`tuple[InputField, ...]` 替代 ToolInputContract 作为内部传递类型。derive_upstream_producers() 从 ToolRegistry + ProjectorRegistry 动态推导生产者映射。ToolRegistry.register() 增加静态可达性警告。

**Tech Stack:** Python 3.12+, pytest, dataclasses

---

### Task 1: models.py — 删除旧类型，增强 InputField

**Files:**
- Modify: `src/agent/artifacts/models.py`
- Modify: `src/agent/artifacts/__init__.py`

- [ ] **Step 1: 给 InputField 添加 `required` 和 `constraints` 字段**

在 `models.py` 找到 `InputField` dataclass（约第 592 行），替换为：

```python
@dataclass(frozen=True)
class InputField:
    """A single input field requirement for a tool.

    Declares that the tool's parameter ``name`` should be auto-bound
    from an artifact of ``artifact_type``.
    """

    name: str
    artifact_type: str
    materialize_as: str | None = None  # derived from artifact_type if None
    required: bool = True
    constraints: dict[str, Any] = field(default_factory=dict)
```

需要导入 `field`：检查文件顶部是否有 `from dataclasses import dataclass`，改为 `from dataclasses import dataclass, field`。

- [ ] **Step 2: 删除 ContractField、ToolInputContract、ToolRuntimePolicy**

删除以下类定义（保留其他所有内容不变）：
- `ContractField`（约第 377 行）
- `ToolInputContract`（约第 390 行）
- `OutputArtifactContract`（约第 402 行）
- `ToolOutputContract`（约第 415 行）
- `ToolRuntimePolicy`（约第 422 行）

- [ ] **Step 3: 添加 `build_contract_from_input_fields()` 工厂函数**

在 `RuntimePolicy` dataclass 之后、`stable_content_hash()` 之前插入：

```python
def build_contract_from_input_fields(
    tool_name: str,
    input_fields: tuple[InputField, ...],
) -> tuple[InputField, ...]:
    """标准化 input_fields，补全 materialize_as 默认值。

    tool_name 参数保留以兼容调用方签名，当前仅用于未来扩展。
    """
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

- [ ] **Step 4: 添加 `derive_upstream_producers()` 和辅助函数**

在 `build_contract_from_input_fields()` 之后插入：

```python
def derive_upstream_producers(
    tools: list[Any],
    projector_registry: Any,  # ProjectorRegistry
) -> dict[str, list[str]]:
    """从工具列表和投影注册表动态推导上游生产者映射。

    直接生产者：工具的 output_artifact_type → [tool.name]
    间接生产者：通过投影图从已生产类型反向推导可达路径。
    """
    producers: dict[str, list[str]] = {}

    # 直接生产者
    for tool in tools:
        oat = getattr(tool, "output_artifact_type", None)
        if oat and isinstance(oat, str):
            producers.setdefault(oat, []).append(tool.name)

    # 间接生产者：对未直接生产的类型，查找投影路径
    all_produced = set(producers.keys())
    # 收集所有被工具 input_fields 需要的类型
    required_types: set[str] = set()
    for tool in tools:
        input_fields = getattr(tool, "input_fields", None)
        if input_fields:
            for f in input_fields:
                required_types.add(f.artifact_type)

    for target_type in required_types:
        if target_type in producers:
            continue
        for produced_type in all_produced:
            if _has_projection_path(
                produced_type, target_type, projector_registry
            ):
                producers.setdefault(target_type, []).extend(
                    producers[produced_type]
                )
                break  # 找到一条路径即可

    return producers


def _has_projection_path(
    source_type: str,
    target_type: str,
    projector_registry: Any,
    max_depth: int = 3,
) -> bool:
    """BFS 检查是否存在从 source_type 到 target_type 的投影路径."""
    from collections import deque

    if source_type == target_type:
        return True
    visited: set[str] = {source_type}
    queue: deque[tuple[str, int]] = deque([(source_type, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for projector in projector_registry.outgoing(current):
            if projector.spec.target_type == target_type:
                return True
            if projector.spec.target_type not in visited:
                visited.add(projector.spec.target_type)
                queue.append((projector.spec.target_type, depth + 1))
    return False
```

- [ ] **Step 5: 更新 `__init__.py` 导出列表**

在 `src/agent/artifacts/__init__.py` 中：
- 移除导出：`ContractField`, `OutputArtifactContract`, `ToolInputContract`, `ToolOutputContract`, `ToolRuntimePolicy`
- 添加导出：`build_contract_from_input_fields`, `derive_upstream_producers`
- 保留所有现有导出（`InputField`, `RuntimePolicy` 等已在列表中）

- [ ] **Step 6: 运行现有测试确认不破坏导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.artifacts.models import InputField, RuntimePolicy, build_contract_from_input_fields, derive_upstream_producers; print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add src/agent/artifacts/models.py src/agent/artifacts/__init__.py
git commit -m "refactor: delete ContractField/ToolInputContract, enhance InputField, add derive_upstream_producers"
```

---

### Task 2: executor.py — validate_materialized_value 适配 InputField

**Files:**
- Modify: `src/agent/artifacts/executor.py`

- [ ] **Step 1: 修改 import 和函数签名**

将文件顶部的 import：
```python
from .models import (
    Artifact,
    ContractField,
    ...
)
```
改为：
```python
from .models import (
    Artifact,
    InputField,
    ...
)
```

将 `validate_materialized_value` 函数签名从：
```python
def validate_materialized_value(
    value: Any,
    field: ContractField,
) -> str | None:
```
改为：
```python
def validate_materialized_value(
    value: Any,
    field: InputField,
) -> str | None:
```

函数体不变 — `field.constraints` 和 `field.name` 现在都来自 `InputField`。

- [ ] **Step 2: 运行 executor 相关测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.artifacts.executor import validate_materialized_value, MaterializerRegistry, ProjectionExecutor; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/agent/artifacts/executor.py
git commit -m "refactor: update validate_materialized_value to use InputField instead of ContractField"
```

---

### Task 3: resolver.py — resolve() 适配 tuple[InputField, ...]

**Files:**
- Modify: `src/agent/artifacts/resolver.py`

- [ ] **Step 1: 修改 import**

将：
```python
from .models import (
    Artifact,
    ContractField,
    MaterializerSpec,
    ...
    ToolInputContract,
    check_schema_version_compatible,
    upstream_producer_for,
)
```
改为：
```python
from .models import (
    Artifact,
    InputField,
    MaterializerSpec,
    ...
    check_schema_version_compatible,
    derive_upstream_producers,
)
```

- [ ] **Step 2: 修改 resolve() 方法签名和内部引用**

将 `resolve()` 方法签名从：
```python
def resolve(
    self,
    contract: ToolInputContract,
    artifacts: list[Artifact],
    policy: ProjectionPolicy,
) -> ProjectionResolution:
```
改为：
```python
def resolve(
    self,
    fields: tuple[InputField, ...],
    tool_name: str,
    artifacts: list[Artifact],
    policy: ProjectionPolicy,
    producers: dict[str, list[str]] | None = None,
) -> ProjectionResolution:
```

方法体内：
- `contract.fields` → `fields`
- `contract.tool_name` → `tool_name`
- 第 69 行 `producer = upstream_producer_for(field.artifact_type)` 替换为：
```python
# Suggest upstream tools if producers mapping is available
upstream = producers.get(field.artifact_type, []) if producers else []
for producer in upstream:
    suggested_actions.append(
        {
            "action": "call_tool",
            "tool": producer,
            "reason": (
                f"{producer} outputs {field.artifact_type}, "
                f"which can satisfy the {field.name} field"
            ),
        }
    )
```
- 诊断消息中的 `role={field.role}, subject={field.subject}` 改为仅 `type={field.artifact_type}`

- [ ] **Step 3: 更新 `_resolve_field_multi` 和 `_artifact_allowed`**

`_artifact_allowed` 静态方法中移除 `field: ContractField` 参数中对 `field.role`/`field.subject`/`field.allow_explicit_override` 的引用（当前已通过简化逻辑不再使用这些字段，但签名是 `ContractField`）。改为 `field: InputField`。

`_find_shortest_path` 中 `field: ContractField` 参数改为 `field: InputField`。

- [ ] **Step 4: 验证导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.artifacts.resolver import ProjectionResolver, emit_event; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/artifacts/resolver.py
git commit -m "refactor: update ProjectionResolver.resolve to use tuple[InputField, ...] and dynamic producers"
```

---

### Task 4: binder.py — bind_tool_inputs 适配 tuple[InputField, ...]

**Files:**
- Modify: `src/agent/artifacts/binder.py`

- [ ] **Step 1: 修改 import**

将：
```python
from .models import ProjectionPolicy, ToolInputContract
```
改为：
```python
from .models import ProjectionPolicy, InputField
```

- [ ] **Step 2: 重写 bind_tool_inputs() 方法**

完整替换 `bind_tool_inputs` 方法：

```python
def bind_tool_inputs(
    self,
    fields: tuple[InputField, ...],
    tool_name: str,
    explicit_kwargs: dict[str, Any],
    producers: dict[str, list[str]] | None = None,
) -> ToolResult:
    """Auto-bind missing tool input fields from artifacts."""
    missing_fields = tuple(
        f for f in fields
        if f.required and f.name not in explicit_kwargs
    )
    if not missing_fields:
        emit_event("tool_arguments_bound", {
            "tool": tool_name,
            "fields": [],
            "source": "explicit",
        })
        return ToolResult(
            success=True,
            data={"arguments": {}, "artifact_bindings": {}},
        )

    resolver = ProjectionResolver(create_default_projector_registry())
    candidates = self._artifact_store.list_projection_candidates()
    resolution = resolver.resolve(
        missing_fields,
        tool_name,
        candidates,
        self._policy,
        producers=producers,
    )
    if resolution.status != "resolved":
        messages = [d.message for d in resolution.diagnostics]
        emit_event("tool_binding_failed", {
            "tool": tool_name,
            "missing_fields": [f.name for f in missing_fields],
            "suggestions": list(resolution.suggested_actions),
        })
        return ToolResult(success=False, error="; ".join(messages))

    executor = ProjectionExecutor(
        projector_registry=create_default_projector_registry(),
        materializer_registry=MaterializerRegistry.default(),
        artifact_store=self._artifact_store,
        features=self._policy.features,
    )
    arguments: dict[str, Any] = {}
    artifact_bindings: dict[str, str] = {}
    validation_errors: list[str] = []
    for name, plan in resolution.plans.items():
        binding = executor.execute(plan)
        field = next((f for f in fields if f.name == name), None)
        if field is not None:
            err = validate_materialized_value(binding.value, field)
            if err is not None:
                validation_errors.append(err)
        arguments[name] = binding.value
        artifact_bindings[name] = binding.artifact_id
    if validation_errors:
        emit_event("tool_binding_failed", {
            "tool": tool_name,
            "reason": "constraint_validation",
            "errors": validation_errors,
        })
        return ToolResult(
            success=False,
            error="; ".join(validation_errors),
        )
    emit_event("tool_arguments_bound", {
        "tool": tool_name,
        "fields": list(artifact_bindings.keys()),
        "source": "auto_binding",
    })
    return ToolResult(
        success=True,
        data={"arguments": arguments, "artifact_bindings": artifact_bindings},
    )
```

- [ ] **Step 3: 删除 register_tool_output() 方法**

`register_tool_output` 接收 `output_contract: Any` 并依赖 `OutputArtifactContract`（已删除）。该方法无实际调用者（所有工具都使用 `output_artifact_type` 字符串路径）。直接删除整个方法（约第 116-157 行）。

- [ ] **Step 4: 验证导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.artifacts.binder import ContractBinder; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/artifacts/binder.py
git commit -m "refactor: update ContractBinder to use tuple[InputField, ...], remove register_tool_output"
```

---

### Task 5: registry.py — 移除 ContractField 构建，添加验证

**Files:**
- Modify: `src/agent/tools/registry.py`

- [ ] **Step 1: 修改 import**

将：
```python
from src.agent.artifacts.models import (
    ProjectionPolicy, ToolRuntimePolicy, InputField, RuntimePolicy,
)
```
改为：
```python
from src.agent.artifacts.models import (
    ProjectionPolicy, InputField, RuntimePolicy,
    build_contract_from_input_fields, derive_upstream_producers,
)
from src.agent.artifacts.projectors import ProjectorRegistry, create_default_projector_registry
```

- [ ] **Step 2: 修改 `__init__` — 注入 ProjectorRegistry，添加 producer_cache**

```python
def __init__(
    self,
    policy: ProjectionPolicy | None = None,
    projector_registry: ProjectorRegistry | None = None,
) -> None:
    self._tools: dict[str, ToolProtocol] = {}
    self._tool_call_counts: dict[str, int] = {}
    self._tool_consecutive_counts: dict[str, int] = {}
    self._last_tool_called: str | None = None
    self._policy = policy or ProjectionPolicy()
    self._projector_registry = projector_registry or create_default_projector_registry()
    self._producer_cache: dict[str, list[str]] | None = None
```

- [ ] **Step 3: 在 register() 和 unregister() 中添加缓存失效和验证**

在 `register()` 方法末尾（`self._tools[tool.name] = tool` 之后、方法结束之前）添加：

```python
self._producer_cache = None
warnings = self._validate_contract_reachability(tool)
for w in warnings:
    logger.warning("Tool '%s': %s", tool.name, w)
```

在 `unregister()` 方法末尾添加：

```python
self._producer_cache = None
```

- [ ] **Step 4: 改写 execute() 中的合约绑定部分**

将 execute() 中约第 134-177 行的合约绑定逻辑替换为：

```python
# Auto-bind contract arguments from typed artifacts
artifact_bindings: dict[str, str] = {}
input_fields = getattr(tool, "input_fields", None)
if (
    artifact_store is not None
    and input_fields is not None
    and len(input_fields) > 0
    and self._policy.features.tool_auto_binding_enabled
):
    effective_fields = build_contract_from_input_fields(name, input_fields)
    if self._policy.features.resolver_dry_run:
        logger.info(
            "Dry-run: would auto-bind %s fields from artifacts", name
        )
    else:
        producers = self._get_producers()
        binding_result = self._bind_contract_arguments(
            fields=effective_fields,
            tool_name=name,
            artifact_store=artifact_store,
            explicit_kwargs=kwargs,
            producers=producers,
        )
        if not binding_result.success:
            return binding_result
        kwargs = {**kwargs, **binding_result.data["arguments"]}
        artifact_bindings = binding_result.data["artifact_bindings"]
```

- [ ] **Step 5: 改写 `_bind_contract_arguments()` 方法**

```python
def _bind_contract_arguments(
    self,
    *,
    fields: tuple[InputField, ...],
    tool_name: str,
    artifact_store: ArtifactStore,
    explicit_kwargs: dict[str, Any],
    producers: dict[str, list[str]] | None = None,
) -> ToolResult:
    from src.agent.artifacts.binder import ContractBinder
    binder = ContractBinder(artifact_store, self._policy)
    return binder.bind_tool_inputs(
        fields, tool_name, explicit_kwargs, producers=producers,
    )
```

- [ ] **Step 6: 删除 `_register_output_artifact()` 方法**

该方法（约第 328-338 行）依赖已删除的 `output_contract`/`ContractBinder.register_tool_output`。直接删除。

同时从 execute() 中删除对应的 `output_contract` 分支（约第 239-248 行），变为：

```python
# Register tool output as typed artifact
output_artifact_type = getattr(tool, "output_artifact_type", None)
if artifact_store is not None and result.success and output_artifact_type is not None:
    try:
        self._register_output_artifact_simple(
            tool_name=name, artifact_type=output_artifact_type,
            result=result, artifact_store=artifact_store,
        )
    except Exception:
        logger.debug("Failed to register output artifact for %s", name, exc_info=True)
```

- [ ] **Step 7: 统一 `_check_runtime_policy()` 仅处理 RuntimePolicy**

当前 `_check_runtime_policy` 同时处理 `RuntimePolicy` 和 `ToolRuntimePolicy`。删除 `ToolRuntimePolicy` 分支：

```python
def _check_runtime_policy(
    self, name: str, policy: RuntimePolicy, kwargs: dict[str, Any] | None = None
) -> ToolResult | None:
    """Enforce runtime policy. Returns ToolResult if blocked, None if allowed."""
    self._tool_call_counts[name] = self._tool_call_counts.get(name, 0) + 1
    if self._last_tool_called == name:
        self._tool_consecutive_counts[name] = (
            self._tool_consecutive_counts.get(name, 0) + 1
        )
    else:
        self._tool_consecutive_counts[name] = 1
    self._last_tool_called = name

    max_calls = policy.max_calls
    max_consecutive = policy.max_consecutive

    if max_calls is not None and self._tool_call_counts[name] > max_calls:
        emit_event("repeated_tool_call_blocked", {
            "tool": name, "reason": "max_calls",
            "count": self._tool_call_counts[name], "limit": max_calls,
        })
        return ToolResult(
            success=False,
            error=(
                f"Tool '{name}' has been called {self._tool_call_counts[name]} times, "
                f"exceeding the limit of {max_calls}."
            ),
            metadata={"blocked_reason": "max_calls_exceeded"},
        )
    if max_consecutive is not None and self._tool_consecutive_counts.get(name, 0) > max_consecutive:
        emit_event("repeated_tool_call_blocked", {
            "tool": name, "reason": "max_consecutive_calls",
            "count": self._tool_consecutive_counts[name], "limit": max_consecutive,
        })
        return ToolResult(
            success=False,
            error=(
                f"Tool '{name}' has been called {self._tool_consecutive_counts[name]} "
                f"consecutive times, exceeding the limit of {max_consecutive}."
            ),
            metadata={"blocked_reason": "max_consecutive_exceeded"},
        )
    return None
```

- [ ] **Step 8: 添加 `_get_producers()` 和 `_validate_contract_reachability()`**

```python
def _get_producers(self) -> dict[str, list[str]]:
    """返回缓存的 producers 映射，延迟计算."""
    if self._producer_cache is None:
        self._producer_cache = derive_upstream_producers(
            list(self._tools.values()), self._projector_registry,
        )
    return self._producer_cache


def _validate_contract_reachability(self, tool: ToolProtocol) -> list[str]:
    """检查 input_fields 所需类型是否能被现有系统满足."""
    input_fields = getattr(tool, "input_fields", None)
    if not input_fields:
        return []
    producers = self._get_producers()
    warnings: list[str] = []
    for f in input_fields:
        if f.artifact_type not in producers:
            warnings.append(
                f"input field '{f.name}' requires artifact type "
                f"'{f.artifact_type}' which has no known producer"
            )
    return warnings
```

- [ ] **Step 9: 验证导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.tools.registry import ToolRegistry; print('OK')"
```

- [ ] **Step 10: Commit**

```bash
git add src/agent/tools/registry.py
git commit -m "refactor: remove ContractField building from ToolRegistry, add registration validation, use RuntimePolicy"
```

---

### Task 6: loop_hints.py — 移除重复转换逻辑

**Files:**
- Modify: `src/agent/core/loop_hints.py`

- [ ] **Step 1: 修改 import**

将：
```python
from ..artifacts.resolver import emit_event
```
改为：
```python
from ..artifacts.models import InputField, build_contract_from_input_fields
from ..artifacts.resolver import emit_event
```

- [ ] **Step 2: 重写 `_get_effective_contract()`**

完整替换为：

```python
def _get_effective_fields(tool: Any) -> tuple[InputField, ...] | None:
    """返回工具的有效 input_fields，补全 materialize_as 默认值。"""
    input_fields = getattr(tool, "input_fields", None)
    if input_fields is not None and input_fields:
        return build_contract_from_input_fields(tool.name, input_fields)
    return None
```

- [ ] **Step 3: 更新所有调用点**

将 `_build_blocked_tools_hints`、`_is_terminal_tool_ready`、`_get_ready_terminal_tools`、`_build_terminal_ready_hints` 中的：
- `effective = _get_effective_contract(tool, artifact_store)` → `effective = _get_effective_fields(tool)`
- `effective.tool_name` → `tool.name`（因为现在返回的是 tuple，不再包含 tool_name）
- `effective.fields` → 不再需要，直接用 `effective`（已是 fields tuple）

具体改动：

在 `_build_blocked_tools_hints`（约第 44 行）：
```python
for tool in tool_registry.list_tools():
    fields = _get_effective_fields(tool)
    if fields is None:
        continue
    candidates = artifact_store.list_projection_candidates()
    resolver = ProjectionResolver(create_default_projector_registry())
    resolution = resolver.resolve(fields, tool.name, candidates, ProjectionPolicy())
    if resolution.status == "failed":
        lines.append(f"- {tool.name} 无法调用（缺少必要输入）：")
        ...
```

类似地更新其他三个函数中的相同模式。

- [ ] **Step 4: 验证导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.core.loop_hints import check_and_inject_hints; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/loop_hints.py
git commit -m "refactor: simplify loop_hints to use build_contract_from_input_fields, remove ContractField conversion"
```

---

### Task 7: plugins/style_audit/tools.py — 补充 input_fields

**Files:**
- Modify: `plugins/style_audit/tools.py`

- [ ] **Step 1: 添加 import 和 input_fields**

在文件顶部已有 import 区域添加：
```python
from src.agent.artifacts.models import InputField
```

在 `AuditWritingStyleTool` 类中，`parameters` 之后添加：
```python
input_fields: tuple[InputField, ...] = (
    InputField(name="text", artifact_type="core.plain_text", materialize_as="string"),
)
```

完整类定义变为：

```python
class AuditWritingStyleTool:
    """Audit document writing style against content compliance rules."""

    name: str = "audit_writing_style"
    description: str = (
        "Audit document writing style against content compliance rules. "
        "Checks for proper opening/closing phrases, tone, and structural "
        "conventions for Chinese government documents. Returns is_valid and "
        "a list of violations."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Full document text to check.",
            },
            "doc_type": {
                "type": "string",
                "description": "Document type (e.g. 通知, 请示, 报告).",
                "default": "通知",
            },
            "subtype": {
                "type": "string",
                "description": "Document subtype (e.g. 指示性通知, 任免性通知).",
            },
        },
        "required": ["text", "doc_type", "subtype"],
    }

    input_fields: tuple[InputField, ...] = (
        InputField(name="text", artifact_type="core.plain_text", materialize_as="string"),
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        ...
```

- [ ] **Step 2: 验证**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from plugins.style_audit.tools import AuditWritingStyleTool; t = AuditWritingStyleTool(); print(t.input_fields)"
```

- [ ] **Step 3: Commit**

```bash
git add plugins/style_audit/tools.py
git commit -m "feat: add input_fields to audit_writing_style for artifact auto-binding"
```

---

### Task 8: 运行全部测试，修复回归

**Files:** 无新建/修改（修复阶段）

- [ ] **Step 1: 运行所有 agent 测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/agent/ -x -v 2>&1 | tail -30
```

预期：全部通过或仅有预先存在的失败。

- [ ] **Step 2: 运行所有 plugin 测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/plugin/ -x -v 2>&1 | tail -30
```

预期：全部通过或仅有预先存在的失败。

- [ ] **Step 3: 运行 app 集成测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/test_app_plugin_integration.py tests/test_orch_dynamic_agents.py -x -v 2>&1 | tail -30
```

- [ ] **Step 4: 如有失败，逐个修复并 commit**

对于每个测试失败，分析原因（通常是 import 路径或类型签名不匹配），修复后 commit。

- [ ] **Step 5: 最终验证 — 确认无残留引用**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && grep -rn "ContractField\|ToolInputContract\|OutputArtifactContract\|ToolOutputContract\|ToolRuntimePolicy" src/ plugins/ --include="*.py" | grep -v ".pyc" | grep -v "__pycache__"
```

预期：无输出（所有旧类型引用已清理）。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: verify all tests pass after artifact contract cleanup"
```
