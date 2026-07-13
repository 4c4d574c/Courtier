# SubAgent output_for 参数 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 `_SubAgentTool` 增加 `output_for` 参数，父代理调用子代理时可指定下游工具的 input_schema，子代理将结果格式化为下游工具可直接使用的参数。

**架构：** `SubAgentRunner` 持有父代理工具 schema 注册表，`_SubAgentTool.execute()` 检测 `output_for` 参数后查找 schema 并拼入 task。子代理按 schema 构造输出，大数据转换在子代理内部完成。

**技术栈：** Python 3.12+, pytest, pytest-asyncio

---

### 任务 1：SubAgentRunner 新增 register_parent_schema

**文件：**
- 修改：`src/agent/agents/subagent.py:92-125`

- [ ] **步骤 1：在 SubAgentRunner.__init__ 中初始化 _parent_tool_schemas**

```python
def __init__(self) -> None:
    self._configs: dict[str, SubAgentConfig] = {}
    self._callback_holder: _CallbackHolder | None = None
    self._context: dict[str, str] | None = None
    self._parent_tool_schemas: dict[str, dict[str, Any]] = {}
```

- [ ] **步骤 2：新增 register_parent_schema 方法**

在 `set_context` 方法之后添加：

```python
def register_parent_schema(self, name: str, schema: dict[str, Any]) -> None:
    """Register a parent-level tool input schema for output_for resolution."""
    self._parent_tool_schemas[name] = schema
```

- [ ] **步骤 3：编写单元测试**

修改 `tests/agent/test_subagent.py`，在 `TestSubAgentRunnerDefine` 后新增：

```python
class TestSubAgentRunnerRegisterParentSchema:
    def test_register_parent_schema_stores_schema(self):
        runner = SubAgentRunner()
        runner.register_parent_schema("export_doc", {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        })
        assert "export_doc" in runner._parent_tool_schemas
        assert runner._parent_tool_schemas["export_doc"]["required"] == ["title"]

    def test_register_multiple_schemas(self):
        runner = SubAgentRunner()
        runner.register_parent_schema("tool_a", {"type": "object", "properties": {}})
        runner.register_parent_schema("tool_b", {"type": "object", "properties": {}})
        assert len(runner._parent_tool_schemas) == 2
        assert "tool_a" in runner._parent_tool_schemas
        assert "tool_b" in runner._parent_tool_schemas
```

- [ ] **步骤 4：运行测试验证通过**

```bash
python -m pytest tests/agent/test_subagent.py::TestSubAgentRunnerRegisterParentSchema -v
```

预期：2 passed

- [ ] **步骤 5：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: add register_parent_schema to SubAgentRunner"
```

---

### 任务 2：_SubAgentTool 新增 output_for 参数

**文件：**
- 修改：`src/agent/agents/subagent.py:180-212`

- [ ] **步骤 1：修改 _SubAgentTool.parameters 增加 output_for 字段**

```python
self.parameters: dict[str, Any] = {
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
```

- [ ] **步骤 2：修改 _SubAgentTool.execute() 增加 task 增强逻辑**

```python
async def execute(self, **kwargs: Any) -> ToolResult:
    task: str = kwargs["task"]
    output_for: str | None = kwargs.get("output_for")
    context_manager = kwargs.get("context_manager")

    if output_for:
        schema = self._runner._parent_tool_schemas.get(output_for)
        if schema:
            task = (
                f"{task}\n\n"
                f"【输出格式要求】\n"
                f"你的结果将作为工具「{output_for}」的输入参数。"
                f"请在完成任务的最终回复中，输出一个 JSON 对象，"
                f"字段与类型需匹配以下 schema：\n"
                f"```json\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n```\n"
                f"只输出 JSON，不要包含其他说明文字。"
            )

    return await self._runner.dispatch(
        self._name,
        task=task,
        context=self._runner._context,
        context_manager=context_manager,
    )
```

- [ ] **步骤 3：编写单元测试 — output_for 在 parameters 中存在**

```python
class TestSubAgentToolOutputFor:
    def test_parameters_includes_output_for_field(self):
        runner = SubAgentRunner()
        agent = _make_agent(name="FormatAuditor")
        runner.define("format_auditor", SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.STRICT,
        ))
        tools = runner.build_tools()
        tool = tools[0]
        assert "output_for" in tool.parameters["properties"]
        assert "output_for" not in tool.parameters["required"]

    def test_parameters_requires_task_still(self):
        runner = SubAgentRunner()
        agent = _make_agent(name="FormatAuditor")
        runner.define("format_auditor", SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.STRICT,
        ))
        tools = runner.build_tools()
        tool = tools[0]
        assert tool.parameters["required"] == ["task"]
```

- [ ] **步骤 4：运行测试验证通过**

```bash
python -m pytest tests/agent/test_subagent.py::TestSubAgentToolOutputFor -v
```

预期：2 passed

- [ ] **步骤 5：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: add output_for parameter to _SubAgentTool"
```

---

### 任务 3：_SubAgentTool.execute() 集成测试

**文件：**
- 修改：`tests/agent/test_subagent.py`

- [ ] **步骤 1：编写集成测试 — output_for 增强 task**

```python
    @pytest.mark.asyncio
    async def test_output_for_enhances_task_when_schema_found(self):
        """When output_for matches a registered schema, task is enhanced."""
        runner = SubAgentRunner()
        captured_tasks: list[str] = []

        class CapturingAgent(Agent):
            async def run(self, task, **kwargs):
                captured_tasks.append(task)
                return AgentResult(status="completed", content='{"title": "test"}')

        runner.define("test_agent", SubAgentConfig(
            agent=CapturingAgent(name="TestAgent", role="test"),
            failure_strategy=FailureStrategy.STRICT,
        ))
        runner.register_parent_schema("export_doc", {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        })
        tools = runner.build_tools()
        result = await tools[0].execute(
            task="Do audit",
            output_for="export_doc",
        )
        assert result.success is True
        assert len(captured_tasks) == 1
        assert "输出格式要求" in captured_tasks[0]
        assert "export_doc" in captured_tasks[0]
        assert '"title"' in captured_tasks[0]

    @pytest.mark.asyncio
    async def test_output_for_no_enhancement_when_schema_not_found(self):
        """When output_for doesn't match any schema, task is unchanged."""
        runner = SubAgentRunner()
        captured_tasks: list[str] = []

        class CapturingAgent(Agent):
            async def run(self, task, **kwargs):
                captured_tasks.append(task)
                return AgentResult(status="completed", content="done")

        runner.define("test_agent", SubAgentConfig(
            agent=CapturingAgent(name="TestAgent", role="test"),
            failure_strategy=FailureStrategy.STRICT,
        ))
        tools = runner.build_tools()
        result = await tools[0].execute(
            task="Do audit",
            output_for="nonexistent_tool",
        )
        assert result.success is True
        assert len(captured_tasks) == 1
        assert "输出格式要求" not in captured_tasks[0]

    @pytest.mark.asyncio
    async def test_output_for_none_does_not_enhance_task(self):
        """When output_for is None, task is unchanged."""
        runner = SubAgentRunner()
        captured_tasks: list[str] = []

        class CapturingAgent(Agent):
            async def run(self, task, **kwargs):
                captured_tasks.append(task)
                return AgentResult(status="completed", content="done")

        runner.define("test_agent", SubAgentConfig(
            agent=CapturingAgent(name="TestAgent", role="test"),
            failure_strategy=FailureStrategy.STRICT,
        ))
        tools = runner.build_tools()
        result = await tools[0].execute(task="Do audit")
        assert result.success is True
        assert len(captured_tasks) == 1
        assert captured_tasks[0] == "Do audit"
```

- [ ] **步骤 2：运行测试验证通过**

```bash
python -m pytest tests/agent/test_subagent.py::TestSubAgentToolOutputFor -v
```

预期：5 passed (2 from previous + 3 new)

- [ ] **步骤 3：Commit**

```bash
git add tests/agent/test_subagent.py
git commit -m "test: add output_for task enhancement integration tests"
```

---

### 任务 4：_extract_result_data 透传 __persisted_output__

**文件：**
- 修改：`src/agent/agents/subagent.py:73-79`

- [ ] **步骤 1：修改 _extract_result_data**

当前代码：
```python
def _extract_result_data(result: AgentResult) -> dict[str, Any]:
    """Extract structured data from an AgentResult."""
    if result.tool_results:
        for tr in reversed(result.tool_results):
            if tr.success and tr.data is not None:
                return {"content": result.content, "data": tr.data}
    return {"content": result.content}
```

改为：
```python
def _extract_result_data(result: AgentResult) -> dict[str, Any]:
    """Extract structured data from an AgentResult."""
    if result.tool_results:
        for tr in reversed(result.tool_results):
            if tr.success and tr.data is not None:
                # If subagent already persisted its output, return the reference as-is
                # so the parent loop doesn't re-persist it.
                if isinstance(tr.data, dict) and tr.data.get("__persisted_output__"):
                    return tr.data
                return {"content": result.content, "data": tr.data}
    return {"content": result.content}
```

- [ ] **步骤 2：编写单元测试**

在 `TestExtractResultData` 中新增：

```python
    def test_passes_through_persisted_output_marker(self):
        """When subagent output is already persisted, return the marker directly."""
        result = AgentResult(
            status="completed",
            content='{"summary": "long content"}',
            tool_results=(
                ToolResult(success=True, data={
                    "__persisted_output__": True,
                    "ref_id": "$ref:some_tool:1",
                    "size_chars": 5000,
                    "preview": "sample...",
                }),
            ),
        )
        extracted = _extract_result_data(result)
        assert extracted["__persisted_output__"] is True
        assert extracted["ref_id"] == "$ref:some_tool:1"
        assert "content" not in extracted

    def test_skips_persisted_marker_and_finds_next_data(self):
        """If first (reversed) has persisted marker, skip to next non-persisted."""
        result = AgentResult(
            status="completed",
            content="done",
            tool_results=(
                ToolResult(success=True, data={
                    "__persisted_output__": True, "ref_id": "$ref:x:1",
                }),
                ToolResult(success=True, data={"actual": "data"}),
            ),
        )
        extracted = _extract_result_data(result)
        assert extracted == {"content": "done", "data": {"actual": "data"}}
```

- [ ] **步骤 3：运行测试验证通过**

```bash
python -m pytest tests/agent/test_subagent.py::TestExtractResultData -v
```

预期：6 passed (4 existing + 2 new)

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent.py
git commit -m "feat: pass through __persisted_output__ in _extract_result_data"
```

---

### 任务 5：OrchestratorAgent 注册父代理工具 schema

**文件：**
- 修改：`src/agent/agents/orch.py:53-70`

- [ ] **步骤 1：在 __init__ 中注册 read_cached_output schema**

在 `self._subagent_runner = SubAgentRunner()` 之后，`for name, agent_cls, strategy in _SUBAGENTS:` 循环之前添加：

```python
# Register parent-level tool schemas so subagents can use output_for
self._subagent_runner.register_parent_schema("read_cached_output", {
    "type": "object",
    "properties": {
        "ref_id": {
            "type": "string",
            "description": "缓存结果的引用 ID，格式为 $ref:<工具名>:<序号>",
        },
    },
    "required": ["ref_id"],
})
```

- [ ] **步骤 2：运行现有测试确认无回归**

```bash
python -m pytest tests/agent/test_domain_agents.py -v
```

预期：全部通过

- [ ] **步骤 3：运行全部 agent 测试**

```bash
python -m pytest tests/agent/ -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "feat: register parent tool schemas for subagent output_for in orchestrator"
```
