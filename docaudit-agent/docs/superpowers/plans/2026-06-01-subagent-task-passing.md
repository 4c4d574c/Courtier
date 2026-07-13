# 子代理任务传递优化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将子代理任务传递从纯文本拼接重构为 Pydantic 类型化参数传递，实现混合 ref 解析（参数级自动解析 + 显式传递），并为未来声明式管道预留输出契约。

**架构：** 新增 `input_models.py` 定义所有代理的 Input/Output Pydantic 模型。`SubAgentConfig` 新增 `input_model`/`output_model` 字段。`_SubAgentTool` 从 `input_model` 自动生成 JSON Schema 并实现 Pydantic 校验 + ref 解析。`SubAgentRunner.dispatch` 接收结构化 `SubAgentInput` 替代文本 `task`。`Agent.run()` 新增可选的 `input` 参数。

**技术栈：** Python, Pydantic, pytest, pytest-asyncio

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `src/agent/agents/input_models.py` (新建) | 所有代理的 Input/Output Pydantic 模型定义 |
| `src/agent/agents/subagent.py` (修改) | SubAgentConfig 新增字段、_SubAgentTool 重构、dispatch 签名变更 |
| `src/agent/agents/base.py` (修改) | Agent.run() 新增 input 参数 |
| `src/agent/agents/orch.py` (修改) | SubAgentConfig 定义更新、system prompt 更新、移除 legacy 路径 |
| `tests/agent/test_subagent.py` (修改) | 新增测试覆盖结构化参数路径 |

**不变的文件：** `core/loop.py`, `core/state.py`, `tools/registry.py`, `tools/protocol.py`, `core/cache_store.py`, `core/context_manager.py`

---

### 任务 1：创建 Input/Output Pydantic 模型

**文件：**
- 创建：`src/agent/agents/input_models.py`
- 测试：`tests/agent/test_subagent.py`

- [ ] **步骤 1：编写测试 — SubAgentInput 基类**

在 `tests/agent/test_subagent.py` 末尾添加：

```python
class TestSubAgentInput:
    """Tests for the SubAgentInput base model and derived input models."""

    def test_subagent_input_base_has_task_field(self):
        from src.agent.agents.input_models import SubAgentInput

        inp = SubAgentInput(task="审核格式")
        assert inp.task == "审核格式"
        assert inp.explicit_inputs is None

    def test_subagent_input_explicit_inputs(self):
        from src.agent.agents.input_models import SubAgentInput

        inp = SubAgentInput(
            task="审核格式",
            explicit_inputs={"document": "$ref:parse_document:1"},
        )
        assert inp.explicit_inputs == {"document": "$ref:parse_document:1"}

    def test_format_auditor_input_fields(self):
        from src.agent.agents.input_models import FormatAuditorInput

        inp = FormatAuditorInput(
            task="检查格式",
            document="$ref:parse_document:1",
        )
        assert inp.task == "检查格式"
        assert inp.document == "$ref:parse_document:1"
        assert inp.doc_type == "通知"  # default

    def test_transform_input_fields(self):
        from src.agent.agents.input_models import TransformInput

        inp = TransformInput(
            task="转换格式",
            sources=["$ref:fmt:1", "$ref:cnt:1"],
        )
        assert len(inp.sources) == 2
        assert inp.target_schema is None

    def test_schema_includes_anyof_for_union_fields(self):
        """Fields typed as str | dict should generate anyOf in JSON Schema."""
        from src.agent.agents.input_models import FormatAuditorInput

        schema = FormatAuditorInput.model_json_schema()
        doc_prop = schema["properties"]["document"]
        assert "anyOf" in doc_prop

    def test_all_auditor_inputs_inherit_subagent_input(self):
        from src.agent.agents.input_models import (
            SubAgentInput,
            FormatAuditorInput,
            ContentAuditorInput,
            CorrectionAuditorInput,
            PlagiarismAuditorInput,
            StyleAuditorInput,
            TransformInput,
        )

        for cls in [
            FormatAuditorInput, ContentAuditorInput, CorrectionAuditorInput,
            PlagiarismAuditorInput, StyleAuditorInput, TransformInput,
        ]:
            assert issubclass(cls, SubAgentInput)
```

- [ ] **步骤 2：运行测试确认失败**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentInput -v
```
预期：FAIL，`ModuleNotFoundError: No module named 'src.agent.agents.input_models'`

- [ ] **步骤 3：创建 input_models.py**

```python
"""Input/Output Pydantic models for sub-agent typed task passing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SubAgentInput(BaseModel):
    """所有子代理输入的基础模型。"""

    task: str = Field(description="子代理需要完成的任务描述（自然语言）")
    explicit_inputs: dict[str, str] | None = Field(
        default=None,
        description=(
            "显式的 ref_id → 参数名映射。"
            "key 为参数名，value 为 $ref:xxx 引用字符串。"
            "系统在 dispatch 前自动将 ref 解析后的数据注入到对应字段。"
        ),
    )


class SubAgentOutput(BaseModel):
    """所有子代理输出的基础模型。"""

    summary: str = Field(description="执行结果摘要")


# -- Auditor Inputs ------------------------------------------------------------


class FormatAuditorInput(SubAgentInput):
    """格式审计器的结构化输入。"""

    document: str | dict = Field(
        description=(
            "待审计的文档数据。可传入 $ref:parse_document:1 引用已解析的文档，"
            "系统自动从缓存加载完整数据"
        ),
    )
    doc_type: str = Field(
        default="通知",
        description="要验证的文档类型（如：通知、函、请示）",
    )


class ContentAuditorInput(SubAgentInput):
    """内容合规审计器的结构化输入。"""

    document: str | dict = Field(
        description="待审计的文档数据。可传入 $ref:parse_document:1 引用"
    )
    rules: list[str] | None = Field(
        default=None,
        description="内容合规规则列表",
    )


class CorrectionAuditorInput(SubAgentInput):
    """文字纠错审计器的结构化输入。"""

    document: str | dict = Field(
        description="待纠错的文档数据。可传入 $ref:parse_document:1 引用"
    )
    rules: list[str] | None = Field(
        default=None,
        description="纠错规则列表",
    )


class PlagiarismAuditorInput(SubAgentInput):
    """抄袭检测审计器的结构化输入。"""

    document: str | dict = Field(
        description="待检测的文档数据。可传入 $ref:parse_document:1 引用"
    )
    library_docs: list[str] | None = Field(
        default=None,
        description="参考库文档（文本列表），为空时子代理自行检索索引库",
    )
    top_k: int = Field(default=5, description="检索参考文档数量")


class StyleAuditorInput(SubAgentInput):
    """行文风格审计器的结构化输入。"""

    document: str | dict = Field(
        description="待审计的文档数据。可传入 $ref:parse_document:1 引用"
    )
    doc_type: str = Field(
        default="通知",
        description="文档类型，影响风格规则的选择",
    )
    rules: list[str] | None = Field(
        default=None,
        description="风格规则列表",
    )


# -- Transform Input -----------------------------------------------------------


class TransformInput(SubAgentInput):
    """数据转换代理的结构化输入。"""

    sources: list[str] = Field(
        description="源数据引用列表，每个元素为 $ref:xxx 字符串"
    )
    target_schema: dict[str, Any] | None = Field(
        default=None,
        description="目标输出格式的 JSON Schema",
    )
    output_for: str | None = Field(
        default=None,
        description="目标工具名称，系统自动查找其 input schema 作为 target_schema",
    )


# -- Output Models -------------------------------------------------------------


class FormatAuditOutput(SubAgentOutput):
    """格式审计结果。"""

    doc_type: str | None = Field(default=None, description="检测到的文档类型")
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="格式错误列表"
    )


class ContentAuditOutput(SubAgentOutput):
    """内容合规审计结果。"""

    domain: str = Field(default="通用", description="合规域")
    violations: list[dict[str, Any]] = Field(
        default_factory=list, description="违规项列表"
    )


class CorrectionAuditOutput(SubAgentOutput):
    """文字纠错审计结果。"""

    corrections: list[dict[str, Any]] = Field(
        default_factory=list, description="纠错项列表"
    )


class PlagiarismAuditOutput(SubAgentOutput):
    """抄袭检测审计结果。"""

    is_plagiarism: bool = Field(default=False, description="是否检测到抄袭")
    matches: list[dict[str, Any]] = Field(
        default_factory=list, description="匹配片段列表"
    )


class StyleAuditOutput(SubAgentOutput):
    """行文风格审计结果。"""

    is_valid: bool = Field(default=True, description="风格是否合规")
    violations: list[dict[str, Any]] = Field(
        default_factory=list, description="违规项列表"
    )


class TransformOutput(SubAgentOutput):
    """数据转换代理的输出。"""

    ref_id: str = Field(description="转换结果的缓存引用 ID")
```

- [ ] **步骤 4：运行测试确认通过**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentInput -v
```
预期：7 tests PASS

- [ ] **步骤 5：Commit**

```bash
git add src/agent/agents/input_models.py tests/agent/test_subagent.py
git commit -m "feat: add SubAgentInput/Output Pydantic models for typed task passing"
```

---

### 任务 2：SubAgentConfig 新增 input_model/output_model 字段

**文件：**
- 修改：`src/agent/agents/subagent.py`

- [ ] **步骤 1：修改 SubAgentConfig dataclass**

在 `subagent.py` 中，修改 `SubAgentConfig`（第 25-31 行）：

```python
@dataclass(frozen=True)
class SubAgentConfig:
    agent: Agent
    failure_strategy: FailureStrategy
    max_retries: int = 0
    description: str = ""
    extra_params: dict[str, Any] | None = None
    input_model: type | None = None      # 新增：Pydantic BaseModel subclass
    output_model: type | None = None     # 新增：Pydantic BaseModel subclass
```

- [ ] **步骤 2：运行现有测试确认兼容**

```bash
uv run pytest tests/agent/test_subagent.py -v
```
预期：所有已存在的测试 PASS（新字段默认 None，不影响现有行为）

- [ ] **步骤 3：Commit**

```bash
git add src/agent/agents/subagent.py
git commit -m "feat: add input_model/output_model fields to SubAgentConfig"
```

---

### 任务 3：重构 _SubAgentTool.parameters 生成

**文件：**
- 修改：`src/agent/agents/subagent.py`
- 测试：`tests/agent/test_subagent.py`

- [ ] **步骤 1：编写测试 — 从 Pydantic 模型生成 parameters**

在 `tests/agent/test_subagent.py` 的 `TestSubAgentExtraParams` 类之后添加：

```python
class TestSubAgentToolParametersFromModel:
    """Tests for _SubAgentTool parameter generation from Pydantic input_model."""

    def test_parameters_generated_from_input_model(self):
        from src.agent.agents.subagent import SubAgentRunner, SubAgentConfig, _SubAgentTool
        from src.agent.agents.input_models import FormatAuditorInput
        from src.agent.agents.echo import create_echo_agent

        agent = create_echo_agent()
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
            input_model=FormatAuditorInput,
        )
        runner = SubAgentRunner()
        tool = _SubAgentTool(name="fmt", config=config, runner=runner)

        props = tool.parameters["properties"]
        assert "task" in props
        assert "document" in props
        assert "doc_type" in props
        assert "explicit_inputs" in props
        assert props["task"]["type"] == "string"
        assert "anyOf" in props["document"]

    def test_parameters_fallback_when_no_input_model(self):
        from src.agent.agents.subagent import SubAgentRunner, SubAgentConfig, _SubAgentTool
        from src.agent.agents.echo import create_echo_agent

        agent = create_echo_agent()
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
            input_model=None,
        )
        runner = SubAgentRunner()
        tool = _SubAgentTool(name="echo", config=config, runner=runner)

        # Default: only task and output_for
        assert "task" in tool.parameters["properties"]
        assert "output_for" in tool.parameters["properties"]
        assert "document" not in tool.parameters["properties"]

    def test_required_fields_from_input_model(self):
        from src.agent.agents.subagent import SubAgentRunner, SubAgentConfig, _SubAgentTool
        from src.agent.agents.input_models import TransformInput
        from src.agent.agents.echo import create_echo_agent

        agent = create_echo_agent()
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
            input_model=TransformInput,
        )
        runner = SubAgentRunner()
        tool = _SubAgentTool(name="transform", config=config, runner=runner)

        required = tool.parameters["required"]
        assert "task" in required
        assert "sources" in required  # TransformInput 的必填字段
```

- [ ] **步骤 2：运行测试确认失败**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolParametersFromModel -v
```
预期：FAIL — 现有 _SubAgentTool.__init__ 不支持从 input_model 生成 parameters

- [ ] **步骤 3：重构 _SubAgentTool.__init__**

在 `subagent.py` 中，替换 `_SubAgentTool.__init__`（第 242-276 行）：

```python
class _SubAgentTool:
    """Adapter: makes a SubAgentConfig callable as a ToolProtocol."""

    _REF_HINT_PATTERN = re.compile(r"^\$ref:[a-zA-Z_][a-zA-Z0-9_]*:\d+")

    def __init__(
        self,
        name: str,
        config: SubAgentConfig,
        runner: SubAgentRunner,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.name = f"run_{name}"
        self.description = config.description or config.agent.role
        self._name = name
        self._runner = runner

        if config.input_model is not None:
            self.parameters = self._build_parameters_from_model(config)
        else:
            self.parameters = self._build_parameters_legacy(config, extra_params)

    def _build_parameters_from_model(self, config: SubAgentConfig) -> dict[str, Any]:
        """从 Pydantic input_model 生成 OpenAI function-calling JSON Schema。"""
        schema = config.input_model.model_json_schema()
        properties = {}
        required: list[str] = []

        for field_name, field_info in schema.get("properties", {}).items():
            prop = dict(field_info)
            # 为 str | dict 类型的字段注入 anyOf ref 提示
            if self._is_union_with_dict(prop):
                prop = self._inject_ref_hint(prop)
            properties[field_name] = prop

        if "required" in schema:
            required = list(schema["required"])

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @staticmethod
    def _is_union_with_dict(prop: dict[str, Any]) -> bool:
        """检测 prop 是否为 string | object 联合类型。"""
        any_of = prop.get("anyOf")
        if not any_of or len(any_of) < 2:
            return False
        types = {o.get("type") for o in any_of if isinstance(o, dict)}
        return "string" in types and "object" in types

    @staticmethod
    def _inject_ref_hint(prop: dict[str, Any]) -> dict[str, Any]:
        """在 anyOf 中注入 $ref 字符串选项，引导 LLM 传递引用。"""
        new_prop = dict(prop)
        new_any_of = []
        for option in prop.get("anyOf", []):
            if isinstance(option, dict) and option.get("type") == "string":
                new_any_of.append({
                    "type": "string",
                    "pattern": r"^\$ref:[a-z_]+:\d+",
                    "description": (
                        "缓存引用字符串。系统自动从磁盘加载完整数据，"
                        "避免上下文膨胀。推荐使用此方式"
                    ),
                })
            else:
                new_any_of.append(option)
        new_prop["anyOf"] = new_any_of
        return new_prop

    def _build_parameters_legacy(
        self,
        config: SubAgentConfig,
        extra_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """保留旧参数生成逻辑（无 input_model 时的回退路径）。"""
        params: dict[str, Any] = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description to delegate to the sub-agent.",
                },
                "output_for": {
                    "type": "string",
                    "description": (
                        "Optional. Name of the downstream tool that will consume "
                        "this subagent's output. When set, the subagent formats its "
                        "result to match that tool's input schema."
                    ),
                },
            },
            "required": ["task"],
        }
        if extra_params:
            props = extra_params.get("properties", {})
            params["properties"].update(props)
            extra_required = extra_params.get("required", [])
            if extra_required:
                params["required"].extend(extra_required)
        return params
```

还需要在文件顶部添加 `import re`：

```python
import re
```

- [ ] **步骤 4：运行测试确认通过**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolParametersFromModel -v
uv run pytest tests/agent/test_subagent.py -v
```
预期：新测试 3 PASS，所有旧测试继续 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: generate _SubAgentTool parameters from Pydantic input_model"
```

---

### 任务 4：重构 _SubAgentTool.execute() — Pydantic 校验 + ref 解析

**文件：**
- 修改：`src/agent/agents/subagent.py`
- 测试：`tests/agent/test_subagent.py`

- [ ] **步骤 1：编写测试 — execute() 对结构化输入的校验和 ref 解析**

在 `tests/agent/test_subagent.py` 末尾添加：

```python
class TestSubAgentToolExecuteStructured:
    """Tests for _SubAgentTool.execute() with structured (Pydantic) inputs."""

    @pytest.mark.asyncio
    async def test_execute_with_input_model_validates_and_dispatches(self):
        """当 config 有 input_model 时，execute() 做 Pydantic 校验后 dispatch。"""
        from src.agent.agents.input_models import FormatAuditorInput
        from src.agent.agents.echo import create_echo_agent

        runner = SubAgentRunner()
        agent = create_echo_agent()
        runner.define(
            "fmt",
            SubAgentConfig(
                agent=agent,
                failure_strategy=FailureStrategy.STRICT,
                input_model=FormatAuditorInput,
            ),
        )
        tools = runner.build_tools()
        result = await tools[0].execute(
            task="检查格式",
            document={"title": "测试通知"},
            doc_type="通知",
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_resolves_ref_in_document_field(self, tmp_path):
        """当 document 字段值为 $ref 字符串时，自动解析。"""
        from src.agent.agents.input_models import FormatAuditorInput
        from src.agent.agents.base import Agent, AgentResult

        cache_dir = str(tmp_path / "cache")
        store = CacheStore(cache_dir=cache_dir)

        # 预存一份文档数据
        doc_data = {"title": "关于xxx的通知", "pages": []}
        persist_result = store.persist(doc_data, "parse_document")
        ref_id = persist_result.ref_id

        captured_input = {}

        class CapturingAgent(Agent):
            async def run(self, input=None, task=None, context=None, **kwargs):
                if input is not None:
                    captured_input["document"] = input.document
                return AgentResult(status="completed", content="ok")

        runner = SubAgentRunner()
        runner.define(
            "fmt",
            SubAgentConfig(
                agent=CapturingAgent(name="Cap", role="x"),
                failure_strategy=FailureStrategy.STRICT,
                input_model=FormatAuditorInput,
            ),
        )
        runner.set_context({"file_path": "/tmp/test.docx"})
        tools = runner.build_tools()

        # 关键：cache_store 需要通过某种方式注入到 execute 中
        # 测试先验证 ref 未解析时传入 ref 字符串
        result = await tools[0].execute(
            task="检查格式",
            document=ref_id,
            doc_type="通知",
            cache_store=store,
        )
        assert result.success is True
        # 经过 ref 解析后，document 应该是实际的字典数据
        assert captured_input["document"] == doc_data

    @pytest.mark.asyncio
    async def test_execute_explicit_inputs_resolved(self, tmp_path):
        """explicit_inputs 中的 ref 被解析到对应字段。"""
        from src.agent.agents.input_models import FormatAuditorInput

        cache_dir = str(tmp_path / "cache")
        store = CacheStore(cache_dir=cache_dir)

        doc_data = {"title": "测试"}
        persist_result = store.persist(doc_data, "parse_document")
        ref_id = persist_result.ref_id

        captured_input = {}

        class CapturingAgent(Agent):
            async def run(self, input=None, task=None, context=None, **kwargs):
                if input is not None:
                    captured_input["document"] = input.document
                return AgentResult(status="completed", content="ok")

        runner = SubAgentRunner()
        runner.define(
            "fmt",
            SubAgentConfig(
                agent=CapturingAgent(name="Cap", role="x"),
                failure_strategy=FailureStrategy.STRICT,
                input_model=FormatAuditorInput,
            ),
        )
        tools = runner.build_tools()
        await tools[0].execute(
            task="检查格式",
            explicit_inputs={"document": ref_id},
            cache_store=store,
        )
        assert captured_input["document"] == doc_data

    @pytest.mark.asyncio
    async def test_execute_raises_on_missing_required_field(self):
        """缺少必填字段时 Pydantic 校验失败。"""
        from src.agent.agents.input_models import TransformInput
        from src.agent.agents.echo import create_echo_agent

        runner = SubAgentRunner()
        agent = create_echo_agent()
        runner.define(
            "transform",
            SubAgentConfig(
                agent=agent,
                failure_strategy=FailureStrategy.TOLERANT,
                input_model=TransformInput,
            ),
        )
        tools = runner.build_tools()
        result = await tools[0].execute(task="转换")
        # TransformInput 需要 sources
        assert result.success is False
        assert "sources" in result.error or "validation" in result.error.lower()
```

- [ ] **步骤 2：运行测试确认失败**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolExecuteStructured -v
```
预期：FAIL — execute() 尚未实现 Pydantic 校验和 ref 解析

- [ ] **步骤 3：实现新的 execute() 方法**

在 `subagent.py` 中，替换 `_SubAgentTool.execute()`（第 278-345 行）：

```python
    async def execute(self, **kwargs: Any) -> ToolResult:
        config = self._runner._configs[self._name]
        cache_store = kwargs.pop("cache_store", None)
        context_manager = kwargs.pop("context_manager", None)

        if config.input_model is not None:
            return await self._execute_structured(config, kwargs, cache_store, context_manager)
        else:
            return await self._execute_legacy(config, kwargs, context_manager)

    async def _execute_structured(
        self,
        config: SubAgentConfig,
        kwargs: dict[str, Any],
        cache_store: Any,
        context_manager: Any,
    ) -> ToolResult:
        """新路径：Pydantic 校验 → ref 解析 → dispatch。"""
        try:
            input_obj = config.input_model(**kwargs)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"参数校验失败: {exc}",
            )

        # 解析字段中的 $ref 引用
        input_obj = self._resolve_input_refs(input_obj, cache_store)

        # 解析 explicit_inputs
        if input_obj.explicit_inputs:
            input_obj = self._resolve_explicit_inputs(input_obj, cache_store)

        # Auto-create audit logger
        sub_audit_logger = self._create_sub_audit_logger()

        try:
            return await self._runner.dispatch_structured(
                self._name,
                input=input_obj,
                context_manager=context_manager,
                audit_logger=sub_audit_logger,
            )
        except Exception as exc:
            logger.exception("Subagent %s crashed", self._name)
            return ToolResult(
                success=False,
                error=f"Subagent {self._name} crashed: {exc}",
            )

    def _resolve_input_refs(self, input_obj: Any, cache_store: Any) -> Any:
        """解析 input_obj 字段中的 $ref:xxx 字符串。"""
        if cache_store is None:
            return input_obj

        for field_name, field_value in input_obj.__dict__.items():
            if field_name == "explicit_inputs":
                continue
            if isinstance(field_value, str) and _REF_PATTERN.match(field_value):
                data = cache_store.load(field_value)
                if data is not None:
                    # 根据字段类型标注做类型适配
                    field_type = input_obj.model_fields[field_name].annotation
                    adapted = self._adapt_type(data, field_type)
                    object.__setattr__(input_obj, field_name, adapted)

            elif isinstance(field_value, list):
                resolved = []
                for item in field_value:
                    if isinstance(item, str) and _REF_PATTERN.match(item):
                        data = cache_store.load(item)
                        resolved.append(data if data is not None else item)
                    else:
                        resolved.append(item)
                object.__setattr__(input_obj, field_name, resolved)

        return input_obj

    def _resolve_explicit_inputs(self, input_obj: Any, cache_store: Any) -> Any:
        """解析 explicit_inputs 中的 ref 映射。"""
        if cache_store is None:
            return input_obj

        for param_name, ref_id in input_obj.explicit_inputs.items():
            data = cache_store.load(ref_id)
            if data is not None and param_name in input_obj.model_fields:
                object.__setattr__(input_obj, param_name, data)

        return input_obj

    @staticmethod
    def _adapt_type(data: Any, field_type: Any) -> Any:
        """根据 Pydantic 字段类型标注适配数据。"""
        import typing

        origin = typing.get_origin(field_type)
        if origin is dict or field_type is dict:
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                try:
                    import json as _json
                    parsed = _json.loads(data)
                    if isinstance(parsed, dict):
                        return parsed
                except (_json.JSONDecodeError, ValueError):
                    pass
        elif origin is list or field_type is list:
            if isinstance(data, list):
                return data
        elif origin in (str | dict, dict | str):
            # Union[str, dict] — 数据已经是 dict（从缓存加载的）
            return data if isinstance(data, dict) else data
        return data

    async def _execute_legacy(
        self,
        config: SubAgentConfig,
        kwargs: dict[str, Any],
        context_manager: Any,
    ) -> ToolResult:
        """旧路径：文本拼接 task（同当前逻辑，标记为 deprecated）。"""
        task: str | None = kwargs.get("task")
        if task is None:
            return ToolResult(
                success=False,
                error=f"Missing required 'task' argument for subagent '{self._name}'. "
                f"Received keys: {list(kwargs.keys())}",
            )
        output_for: str | None = kwargs.get("output_for")
        ref_ids: list[str] | None = kwargs.get("ref_ids")

        if ref_ids:
            ref_list = "\n".join(f"  - {rid}" for rid in ref_ids)
            task = (
                f"以下缓存数据的引用 ID 可用（无需调用 read_cached_output 加载）：\n"
                f"{ref_list}\n\n"
                f"如需将这些缓存数据作为下游工具的参数传入，直接使用 ref_id 字符串"
                f"（如 \"$ref:parse_document:1\"）作为参数值即可，"
                f"系统会自动加载完整数据替换引用。\n\n"
                f"然后完成以下任务：{task}"
            )

        if output_for:
            schema: dict[str, Any] | None = self._runner.get_parent_schema(output_for)
            if schema:
                import json as _json
                task = (
                    f"{task}\n\n"
                    f"【输出格式要求】\n"
                    f"你的结果将作为工具「{output_for}」的输入参数。"
                    f"请在完成任务的最终回复中，输出一个 JSON 对象，"
                    f"字段与类型需匹配以下 schema：\n"
                    f"```json\n{_json.dumps(schema, ensure_ascii=False, indent=2)}\n```\n"
                    f"只输出 JSON，不要包含其他说明文字。"
                )
            else:
                logger.warning(
                    "output_for=%r not found in parent schemas; task unchanged",
                    output_for,
                )

        sub_audit_logger = self._create_sub_audit_logger()

        try:
            return await self._runner.dispatch(
                self._name,
                task=task,
                context=runner_context,
                context_manager=context_manager,
                audit_logger=sub_audit_logger,
            )
        except Exception as exc:
            logger.exception("Subagent %s crashed", self._name)
            return ToolResult(
                success=False,
                error=f"Subagent {self._name} crashed: {exc}",
            )

    def _create_sub_audit_logger(self) -> Any:
        """Auto-create audit logger for subagent if audit_base_dir is set in context."""
        runner_context = self._runner.get_context()
        if runner_context:
            audit_base_dir = runner_context.get("audit_base_dir")
            if audit_base_dir:
                from ..core.audit_logger import AuditLogger
                return AuditLogger.for_run(
                    agent_name=f"subagent_{self._name}",
                    base_dir=audit_base_dir,
                )
        return None
```

并在文件顶部 `_REF_PATTERN` 常量引入（复用在 `_resolve_input_refs` 中）：

在文件 `import re` 之后添加：

```python
_REF_PATTERN = re.compile(r"^\$ref:([a-zA-Z_][a-zA-Z0-9_]*):(\d+)(?::([a-zA-Z_][a-zA-Z0-9_]*))?$")
```

- [ ] **步骤 4：运行测试确认通过**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolExecuteStructured -v
```
预期：3 PASS（cache_store 注入的测试可能需要调整—本步先验证基本校验逻辑）

- [ ] **步骤 5：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: refactor _SubAgentTool.execute() for Pydantic validation and ref resolution"
```

---

### 任务 5：SubAgentRunner.dispatch_structured + Agent.run() 签名

**文件：**
- 修改：`src/agent/agents/subagent.py`
- 修改：`src/agent/agents/base.py`

- [ ] **步骤 1：在 SubAgentRunner 中新增 dispatch_structured 方法**

在 `subagent.py` 的 `SubAgentRunner` 类中，`dispatch` 方法之后添加：

```python
    async def dispatch_structured(
        self,
        name: str,
        input: Any,  # SubAgentInput
        context_manager: Any = None,
        callbacks: _CallbackHolder | None = None,
        audit_logger: Any = None,
    ) -> ToolResult:
        """Dispatch a task to a subagent with structured (Pydantic) input.

        Unlike :meth:`dispatch`, this path passes a typed ``SubAgentInput``
        object directly to the subagent instead of a text ``task`` string.
        Failure strategy behavior is identical to :meth:`dispatch`.
        """
        config = self._configs.get(name)
        if config is None:
            return ToolResult(success=False, error=f"Unknown subagent: {name}")
        agent = config.agent
        cb = callbacks or self._callback_holder

        if context_manager is not None:
            context_manager.disable_compaction()
        try:
            attempts = 1 + config.max_retries
            for attempt in range(attempts):
                try:
                    result = await agent.run(
                        input=input,
                        context_manager=context_manager,
                        on_step=cb.make_on_step(name) if cb else None,
                        on_token=cb.make_on_token(name) if cb else None,
                        on_content_token=cb.make_on_content_token(name) if cb else None,
                        on_tool_result=cb.make_on_tool_result(name) if cb else None,
                        audit_logger=audit_logger,
                    )

                    if result.status == "completed":
                        return ToolResult(
                            success=True,
                            data=_extract_result_data(result),
                            metadata={"is_subagent_result": True},
                        )

                    if config.failure_strategy == FailureStrategy.STRICT:
                        return ToolResult(success=False, error=_describe_failure(result))
                    if (
                        config.failure_strategy == FailureStrategy.RETRY
                        and attempt < attempts - 1
                    ):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return ToolResult(success=False, error=_describe_failure(result))

                except Exception as exc:
                    if config.failure_strategy == FailureStrategy.STRICT:
                        raise
                    if (
                        config.failure_strategy == FailureStrategy.RETRY
                        and attempt < attempts - 1
                    ):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return ToolResult(success=False, error=str(exc))

            return ToolResult(success=False, error="All retries exhausted")
        finally:
            if context_manager is not None:
                context_manager.enable_compaction()
```

- [ ] **步骤 2：修改 Agent.run() 签名**

在 `base.py` 中，修改 `Agent.run()` 方法签名（第 157-168 行）：

```python
    async def run(
        self,
        task: str | None = None,
        input: Any | None = None,           # 新增：SubAgentInput 结构化输入
        context: dict[str, str] | None = None,
        on_step: Callable[[str, str], Awaitable[None]] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None = None,
        context_manager: Any | None = None,
        state: AgentState | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> AgentResult:
        """Entry point: receive task, run agent loop, return result.

        Can be called with either:
        - ``input`` (SubAgentInput): structured, typed input from sub-agent dispatch
        - ``task`` (str): plain text task (legacy / standalone use)
        When both are provided, ``input`` takes precedence.
        """
        # 从 SubAgentInput 中提取 task + 构建 context
        if input is not None:
            task = input.task
            # 将 Input 模型中的非 None 字段注入到 context
            input_context: dict[str, str] = {}
            for field_name, field_value in input.__dict__.items():
                if field_value is not None and field_name not in ("task", "explicit_inputs"):
                    if isinstance(field_value, str):
                        input_context[field_name] = field_value
                    else:
                        import json as _json
                        try:
                            input_context[field_name] = _json.dumps(
                                field_value, ensure_ascii=False, default=str
                            )
                        except Exception:
                            input_context[field_name] = str(field_value)[:500]
            if context:
                merged = dict(context)
                merged.update(input_context)
                context = merged
            else:
                context = input_context

        if task is None:
            raise ValueError("Either 'task' or 'input' must be provided")

        system_prompt = self.build_system_prompt(context)
        # ... 其余方法体不变
```

> **注意**：`run()` 方法体从 `system_prompt = self.build_system_prompt(context)` 开始保持不变。只修改签名和 task/context 提取逻辑。

- [ ] **步骤 3：运行测试确认兼容**

```bash
uv run pytest tests/agent/test_subagent.py tests/agent/test_orchestrator.py tests/agent/test_agent_base.py -v
```
预期：所有测试 PASS

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/subagent.py src/agent/agents/base.py
git commit -m "feat: add dispatch_structured and Agent.run() input parameter"
```

---

### 任务 6：更新 orch.py — SubAgentConfig 定义 + system prompt + 清理

**文件：**
- 修改：`src/agent/agents/orch.py`

- [ ] **步骤 1：导入 input models**

在 `orch.py` 顶部添加导入：

```python
from .subagent import SubAgentRunner, SubAgentConfig, FailureStrategy
from .input_models import (
    FormatAuditorInput,
    ContentAuditorInput,
    CorrectionAuditorInput,
    PlagiarismAuditorInput,
    StyleAuditorInput,
    TransformInput,
)
```

- [ ] **步骤 2：更新各代理的 SubAgentConfig 定义**

替换 `oro.py` 中 `__init__` 方法内第 135-143 行的子代理注册代码：

```python
        for name, agent_cls, strategy in subagents:
            agent = agent_cls(model=_model)
            input_model = _INPUT_MODEL_MAP.get(name)
            self._subagent_runner.define(
                name,
                SubAgentConfig(
                    agent=agent,
                    failure_strategy=strategy,
                    input_model=input_model,
                ),
            )
```

在文件顶部 `_AUDITOR_NAME_MAP` 之后添加映射：

```python
_INPUT_MODEL_MAP: dict[str, type] = {
    "format_auditor": FormatAuditorInput,
    "content_auditor": ContentAuditorInput,
    "correction_auditor": CorrectionAuditorInput,
    "plagiarism_auditor": PlagiarismAuditorInput,
    "style_auditor": StyleAuditorInput,
}
```

- [ ] **步骤 3：更新 TransformAgent 的 SubAgentConfig**

替换 TransformAgent 的注册代码（第 146-173 行），移除 `extra_params` 和 `description`：

```python
        transform_agent = TransformAgent(model=_model)
        self._subagent_runner.define(
            "transform",
            SubAgentConfig(
                agent=transform_agent,
                failure_strategy=FailureStrategy.TOLERANT,
                input_model=TransformInput,
            ),
        )
```

- [ ] **步骤 4：移除 register_parent_schema 调用**

移除 `__init__` 中不再需要的父级 schema 注册（第 93-120 行）：

```python
# 删除以下代码块：
# self._subagent_runner.register_parent_schema("read_cached_output", ...)
# self._subagent_runner.register_parent_schema("persist_output", ...)
```

> 保留 `register_parent_schema` 方法本身在 SubAgentRunner 中（可能其他代码使用），但移除此处的调用，因为现在通过 input_model 传递 schema。

- [ ] **步骤 5：更新 system prompt**

在 `oro.py` 的 `__init__` 中，更新 `role` 字符串（第 186-202 行），修改 ref 相关的指令：

将：
```python
"如需将完整数据作为后续工具的参数传入，有以下两种方式（系统会自动加载）：\n"
'1. 直接传递 ref_id 字符串（如 "$ref:parse_document:1"）作为参数值，'
```

替换为：
```python
"# 结构化参数传递规则\n"
"每个审计工具的 document 参数接受两种传值方式：\n"
"1. 传入 $ref 缓存引用字符串（如 \"$ref:parse_document:1\"），系统自动加载完整数据 — 推荐\n"
"2. 传入完整的 JSON 对象（仅当数据较小时使用）\n"
"始终优先使用方式 1 避免上下文膨胀。\n"
```

同时调整 orchestrator 的 role 中移除不再需要的 `data_shape._field_paths` 等旧指令。

- [ ] **步骤 6：运行测试确认**

```bash
uv run pytest tests/agent/test_orchestrator.py -v
```
预期：所有测试调整后 PASS（test_subagent_tools_registered 和 test_all_expected_tools_present 仍然通过）

- [ ] **步骤 7：Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "feat: update orch.py SubAgentConfig definitions with input_model and new system prompt"
```

---

### 任务 7：集成验证 — 端到端测试 + 清理

**文件：**
- 修改：`tests/agent/test_subagent.py`
- 修改：`tests/agent/test_orchestrator.py`

- [ ] **步骤 1：添加端到端集成测试**

在 `tests/agent/test_subagent.py` 末尾添加：

```python
class TestStructuredEndToEnd:
    """End-to-end tests for the structured sub-agent pipeline."""

    @pytest.mark.asyncio
    async def test_format_auditor_receives_structured_input(self):
        """Orchestrator dispatches to format_auditor with structured input."""
        from src.agent.agents.input_models import FormatAuditorInput
        from src.agent.agents.echo import create_echo_agent

        captured_input = {}

        class TrackingEchoAgent(Agent):
            async def run(self, input=None, task=None, **kwargs):
                if input is not None:
                    captured_input["received_input"] = input
                    captured_input["task"] = input.task
                    captured_input["document"] = input.document
                return AgentResult(status="completed", content="echo'd")

        runner = SubAgentRunner()
        runner.define(
            "format_auditor",
            SubAgentConfig(
                agent=TrackingEchoAgent(name="Fmt", role="x"),
                failure_strategy=FailureStrategy.STRICT,
                input_model=FormatAuditorInput,
            ),
        )
        runner.set_context({"file_path": "/tmp/test.docx"})
        tools = runner.build_tools()

        result = await tools[0].execute(
            task="检查格式",
            document="$ref:parse_document:1",
            doc_type="通知",
        )
        assert result.success is True
        assert captured_input["task"] == "检查格式"
        assert captured_input["doc_type"] == "通知"
        # ref 字符串未被解析（无 cache_store 注入）时保留原值
        assert captured_input["document"] == "$ref:parse_document:1"

    @pytest.mark.asyncio
    async def test_backward_compat_legacy_path(self):
        """无 input_model 时，旧的文本拼接路径仍然工作。"""
        captured_task = {}

        class TaskCapturingAgent(Agent):
            async def run(self, task=None, input=None, **kwargs):
                captured_task["task"] = task
                return AgentResult(status="completed", content="ok")

        runner = SubAgentRunner()
        runner.define(
            "old_agent",
            SubAgentConfig(
                agent=TaskCapturingAgent(name="Old", role="x"),
                failure_strategy=FailureStrategy.TOLERANT,
                input_model=None,
            ),
        )
        tools = runner.build_tools()
        result = await tools[0].execute(task="旧方式任务", ref_ids=["$ref:x:1"])
        assert result.success is True
        assert "旧方式任务" in captured_task["task"]
        assert "$ref:x:1" in captured_task["task"]

    @pytest.mark.asyncio
    async def test_orchestrator_integration_with_input_models(self):
        """完整的 orchestrator → subagent 流程使用结构化输入。"""
        from src.agent.agents.orch import OrchestratorAgent
        from unittest.mock import AsyncMock, MagicMock
        from src.agent.core.model import ToolCall

        parser = MagicMock()
        parser.name = "ParserAgent"
        parser.parse = AsyncMock(
            return_value={
                "title": "测试通知",
                "doc_type": "通知",
                "pages": [{"text": "段落1"}],
            }
        )

        model = MockModelClient(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="run_format_auditor",
                    arguments={
                        "task": "审核格式",
                        "document": "$ref:parse_document:1",
                        "doc_type": "通知",
                    },
                ),
            ]
        )

        orch = OrchestratorAgent(parser=parser, model=model)
        result = await orch.run(
            task="Audit /path/to/doc.pdf",
            context={"file_path": "/path/to/doc.pdf"},
        )
        assert result.status == "completed"
```

- [ ] **步骤 2：运行所有测试**

```bash
uv run pytest tests/agent/test_subagent.py tests/agent/test_orchestrator.py -v
```
预期：所有测试 PASS，包括新旧路径

- [ ] **步骤 3：运行完整测试套件**

```bash
uv run pytest tests/ -v
```
预期：无回归

- [ ] **步骤 4：Commit**

```bash
git add tests/agent/test_subagent.py tests/agent/test_orchestrator.py
git commit -m "test: add end-to-end integration tests for structured subagent dispatch"
```

---

## 实现顺序依赖

```
任务 1 (input_models.py) ──┐
                           ├──> 任务 3 (parameters 生成) ──> 任务 4 (execute 重构)
任务 2 (SubAgentConfig) ───┘                                        │
                                                                     ▼
                                                              任务 5 (dispatch + Agent.run)
                                                                     │
                                                                     ▼
                                                              任务 6 (orch.py 更新)
                                                                     │
                                                                     ▼
                                                              任务 7 (集成验证)
```

## 验证清单

- [ ] `uv run pytest tests/agent/test_subagent.py -v` — 所有子代理测试通过
- [ ] `uv run pytest tests/agent/test_orchestrator.py -v` — orchestrator 测试通过
- [ ] `uv run pytest tests/agent/ -v` — 所有 agent 测试通过
- [ ] `uv run pytest tests/ -v` — 全量测试通过，无回归
- [ ] 旧路径（无 input_model）仍然工作
- [ ] 新路径（有 input_model）正确校验、解析 ref、dispatch
- [ ] $ref 字符串在 anyOf 中正确提示 LLM
