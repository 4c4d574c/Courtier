# Artifact 模块与插件子代理交互修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Artifact 模块与插件子代理（ProxyAgent）之间的 4 个数据流断裂点，实现 artifact 上下文推送、structured input 转发、子代理输出注册的完整闭环。

**Architecture:** 采用 Push + 结果回传模式，在 dispatch 时将 artifact 元数据和结构化输入序列化后随 JSON-RPC 转发，插件侧无需管理独立的 ArtifactStore。

**Tech Stack:** Python 3.12, Pydantic, asyncio, pytest, unittest.mock

---

## 文件结构

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `src/agent/agents/subagent/runner.py` | `_build_artifact_context()` + dispatch 传参 | 修改 |
| `src/plugin/proxies.py` | `ProxyAgent.run()` 接收 `artifact_context` / `input_data` | 修改 |
| `src/agent/agents/subagent/config.py` | `SubAgentConfig` 增加 `output_artifact_type` | 修改 |
| `src/agent/agents/subagent/tool.py` | `_SubAgentTool` 暴露 `output_artifact_type` | 修改 |
| `src/agent/agents/orch.py` | orchestrator 声​​明插件子代理的 output_artifact_type | 修改 |
| `tests/agent/test_subagent.py` | 新增测试：artifact_context、input_data、output_artifact_type | 修改 |
| `tests/plugin/test_proxies.py` | 新增测试：ProxyAgent 转发 artifact_context、input_data | 修改 |

---

### Task 1: SubAgentRunner 增加 _build_artifact_context 并注入 dispatch

**Files:**
- Modify: `src/agent/agents/subagent/runner.py:194-220`

- [ ] **Step 1: 在 SubAgentRunner 中增加 _build_artifact_context 静态方法**

在 `_build_scoped_store` 方法之后添加新方法：

```python
@staticmethod
def _build_artifact_context(artifact_store: Any) -> list[dict[str, Any]]:
    """Extract artifact metadata for serialization across JSON-RPC boundary.
    
    Returns a list of dicts with artifact_id, artifact_type, and
    projectable_to_types, suitable for inclusion in agent.run params.
    """
    if not isinstance(artifact_store, ArtifactStore):
        return []
    from ..artifacts.projectors import create_default_projector_registry
    
    projector_registry = create_default_projector_registry()
    projection_targets: dict[str, list[str]] = {}
    for proj in projector_registry.all():
        src = proj.spec.source_type
        tgt = proj.spec.target_type
        if src not in projection_targets:
            projection_targets[src] = []
        if tgt not in projection_targets[src]:
            projection_targets[src].append(tgt)
    
    entries: list[dict[str, Any]] = []
    for artifact in artifact_store.list_all():
        if artifact.metadata.debug_only:
            continue
        entries.append({
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "projectable_to": projection_targets.get(artifact.artifact_type, []),
            "created_by": artifact.metadata.created_by,
            "content_hash": artifact.metadata.content_hash,
            "semantic_role": artifact.metadata.semantic_role,
        })
    return entries
```

- [ ] **Step 2: 在 dispatch() 的 _run() 闭包中注入 artifact_context**

修改 `dispatch()` 方法中的 `_run()` 闭包（约 line 338-385），在构建 `run_kwargs` 之前调用 `_build_artifact_context`：

找到这段代码（约 line 339-340）：
```python
def _run():
    scoped_store = self._build_scoped_store(artifact_store)
```

替换为：
```python
def _run():
    scoped_store = self._build_scoped_store(artifact_store)
    artifact_context = self._build_artifact_context(scoped_store)
```

然后在 `run_kwargs`（约 line 364）中添加：
```python
run_kwargs: dict[str, Any] = dict(
    task=task,
    context=context,
    context_manager=context_manager,
    on_step=wrapped["on_step"],
    on_token=wrapped["on_token"],
    on_content_token=wrapped["on_content_token"],
    on_tool_result=wrapped["on_tool_result"],
    audit_logger=audit_logger,
    artifact_store=scoped_store,
    artifact_context=artifact_context,  # NEW: serialized artifact metadata
)
```

- [ ] **Step 3: 在 dispatch_structured() 的 _run() 闭包中同样注入**

在 `dispatch_structured()` 方法中做同样的修改（约 line 425-466）。在 `scoped_store` 赋值后添加：
```python
artifact_context = self._build_artifact_context(scoped_store)
```

在 `run_kwargs` 中添加：
```python
run_kwargs: dict[str, Any] = dict(
    input=input,
    context=context,
    context_manager=context_manager,
    on_step=wrapped["on_step"],
    on_token=wrapped["on_token"],
    on_content_token=wrapped["on_content_token"],
    on_tool_result=wrapped["on_tool_result"],
    audit_logger=audit_logger,
    artifact_store=scoped_store,
    artifact_context=artifact_context,  # NEW
)
```

- [ ] **Step 4: 运行现有测试确认无回归**

```bash
uv run pytest tests/agent/test_subagent.py -v -x
```

预期：所有已有测试通过。

- [ ] **Step 5: Commit**

```bash
git add src/agent/agents/subagent/runner.py
git commit -m "feat: push artifact_context metadata to sub-agents at dispatch time"
```

---

### Task 2: ProxyAgent.run() 接收并转发 artifact_context

**Files:**
- Modify: `src/plugin/proxies.py:171-178`
- Modify: `tests/plugin/test_proxies.py` (新增测试)

- [ ] **Step 1: 编写失败的测试 — ProxyAgent 转发 artifact_context 到 JSON-RPC params**

在 `tests/plugin/test_proxies.py` 的 `TestProxyAgent` 类中添加：

```python
@pytest.mark.asyncio
async def test_run_forwards_artifact_context(self):
    """ProxyAgent forwards artifact_context to agent.run JSON-RPC params."""
    agent_spec = {"name": "my_agent", "role": "auditor"}
    mock_client = MockClient(stream_chunks=[
        JSONRPCStreamChunk(
            id=1, status="end",
            result={"status": "completed", "content": "ok"},
        ),
    ])
    proxy = ProxyAgent(mock_client, agent_spec)

    artifact_context = [
        {
            "artifact_id": "$ref:parse_document:1",
            "artifact_type": "docaudit.parsed_document",
            "projectable_to": ["core.plain_text", "docaudit.paragraph_list"],
            "created_by": "parse_document",
            "content_hash": "sha256:abc",
            "semantic_role": "document",
        },
    ]

    result = await proxy.run(
        task="审计文档",
        artifact_context=artifact_context,
    )

    assert result.status == "completed"
    assert mock_client.calls[0]["method"] == "agent.run"
    assert "artifact_context" in mock_client.calls[0]["params"]
    assert mock_client.calls[0]["params"]["artifact_context"] == artifact_context

@pytest.mark.asyncio
async def test_run_without_artifact_context_is_fine(self):
    """ProxyAgent works fine when artifact_context is not provided."""
    agent_spec = {"name": "my_agent", "role": "auditor"}
    mock_client = MockClient(stream_chunks=[
        JSONRPCStreamChunk(
            id=1, status="end",
            result={"status": "completed", "content": "ok"},
        ),
    ])
    proxy = ProxyAgent(mock_client, agent_spec)

    result = await proxy.run(task="审计文档")

    assert result.status == "completed"
    params = mock_client.calls[0]["params"]
    # artifact_context absent if not provided
    assert params.get("artifact_context") is None
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent::test_run_forwards_artifact_context -v
```

预期：FAIL — `artifact_context` 不在 JSON-RPC params 中。

- [ ] **Step 3: 修改 ProxyAgent.run() 转发 artifact_context**

在 `src/plugin/proxies.py` 的 `ProxyAgent.run()` 方法中（约 line 171-178），修改 JSON-RPC params 构建逻辑。

找到：
```python
async def run(self, task: str | None = None, **kwargs: Any) -> AgentResult:
```

在 `model_config = kwargs.get("model_config", {})` 之后添加：
```python
artifact_context = kwargs.get("artifact_context")
```

找到 JSON-RPC params 构建（约 line 189-197）：
```python
{
    "agent": self._agent_name,
    "task": task,
    "model_config": model_config,
    **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
}
```

替换为：
```python
params = {
    "agent": self._agent_name,
    "task": task,
    "model_config": model_config,
}
if artifact_context is not None:
    params["artifact_context"] = artifact_context
params.update({k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS})
```

然后把 `self._client.stream("agent.run", params, ...)` 改为使用 `params` 变量。

- [ ] **Step 4: 运行测试确认 PASS**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent::test_run_forwards_artifact_context tests/plugin/test_proxies.py::TestProxyAgent::test_run_without_artifact_context_is_fine -v
```

预期：两个测试均 PASS。

- [ ] **Step 5: 运行全部 proxy 测试确认无回归**

```bash
uv run pytest tests/plugin/test_proxies.py -v
```

预期：全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/plugin/proxies.py tests/plugin/test_proxies.py
git commit -m "feat: forward artifact_context in ProxyAgent.run() JSON-RPC params"
```

---

### Task 3: 测试 SubAgentRunner 集成 — artifact_context 端到端

**Files:**
- Modify: `tests/agent/test_subagent.py` (新增测试)

- [ ] **Step 1: 编写集成测试 — dispatch 时 artifact_context 传递到 agent.run()**

在 `tests/agent/test_subagent.py` 的 `TestSubAgentRunnerDispatch` 类中添加：

```python
@pytest.mark.asyncio
async def test_artifact_context_passed_to_agent_run(self):
    """artifact_context is extracted from artifact_store and passed to agent.run()."""
    from src.agent.artifacts.store import ArtifactStore
    from src.agent.artifacts.models import ArtifactMetadata, Artifact
    
    runner = SubAgentRunner()
    captured_context = {}
    
    class ContextCapturingAgent(Agent):
        async def run(self, task=None, artifact_context=None, **kwargs):
            captured_context["artifact_context"] = artifact_context
            return AgentResult(status="completed", content="ok")
    
    runner.define(
        "test_agent",
        SubAgentConfig(
            agent=ContextCapturingAgent(name="TestAgent", role="test"),
            failure_strategy=FailureStrategy.STRICT,
        ),
    )
    
    # Build an artifact_store with a non-debug artifact
    store = ArtifactStore()
    metadata = ArtifactMetadata(
        created_by="parse_document",
        content_hash="sha256:abc",
        semantic_role="document",
        projection_allowed=True,
        debug_only=False,
    )
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={"pages": []},
        metadata=metadata,
    )
    store.put(artifact)
    
    result = await runner.dispatch(
        "test_agent",
        task="do something",
        artifact_store=store,
    )
    assert result.success is True
    assert captured_context["artifact_context"] is not None
    assert len(captured_context["artifact_context"]) == 1
    ctx = captured_context["artifact_context"][0]
    assert ctx["artifact_id"] == "$ref:parse_document:1"
    assert ctx["artifact_type"] == "docaudit.parsed_document"
    assert "projectable_to" in ctx
    assert "created_by" in ctx

@pytest.mark.asyncio
async def test_artifact_context_excludes_debug_artifacts(self):
    """debug_only artifacts are excluded from artifact_context."""
    from src.agent.artifacts.store import ArtifactStore
    from src.agent.artifacts.models import ArtifactMetadata, Artifact
    
    runner = SubAgentRunner()
    captured_context = {}
    
    class ContextCapturingAgent(Agent):
        async def run(self, task=None, artifact_context=None, **kwargs):
            captured_context["artifact_context"] = artifact_context
            return AgentResult(status="completed", content="ok")
    
    runner.define(
        "test_agent",
        SubAgentConfig(
            agent=ContextCapturingAgent(name="TestAgent", role="test"),
            failure_strategy=FailureStrategy.STRICT,
        ),
    )
    
    store = ArtifactStore()
    # Debug artifact — should be excluded
    store.put(Artifact(
        artifact_id="$ref:debug_tool:1",
        artifact_type="core.debug_view",
        data={"debug": True},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            content_hash="sha256:xyz",
            debug_only=True,
            projection_allowed=False,
        ),
    ))
    # Business artifact — should be included
    store.put(Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            content_hash="sha256:abc",
            semantic_role="document",
            projection_allowed=True,
            debug_only=False,
        ),
    ))
    
    result = await runner.dispatch(
        "test_agent",
        task="do something",
        artifact_store=store,
    )
    assert result.success is True
    assert len(captured_context["artifact_context"]) == 1
    assert captured_context["artifact_context"][0]["artifact_id"] == "$ref:parse_document:1"
```

- [ ] **Step 2: 运行测试确认 PASS**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentRunnerDispatch::test_artifact_context_passed_to_agent_run tests/agent/test_subagent.py::TestSubAgentRunnerDispatch::test_artifact_context_excludes_debug_artifacts -v
```

预期：两个测试均 PASS。

- [ ] **Step 3: 运行全部 subagent 测试确认无回归**

```bash
uv run pytest tests/agent/test_subagent.py -v
```

预期：全部通过。

- [ ] **Step 4: Commit**

```bash
git add tests/agent/test_subagent.py
git commit -m "test: add integration tests for artifact_context in sub-agent dispatch"
```

---

### Task 4: SubAgentRunner.dispatch_structured 序列化 input 并通过 input_data 传递

**Files:**
- Modify: `src/agent/agents/subagent/runner.py:404-466`

- [ ] **Step 1: 修改 dispatch_structured 中的 run_kwargs**

在 `dispatch_structured()` 的 `_run()` 闭包中，找到 `run_kwargs` 构建（约 line 451-461）：

```python
run_kwargs: dict[str, Any] = dict(
    input=input,
    context=context,
    ...
)
```

替换为（将 Pydantic model 序列化为 dict，通过 input_data 传递，保留 input 给本地 agent 使用）：

```python
# Serialize Pydantic input for JSON-RPC forwarding (input stays for local agents)
input_data = input.model_dump() if hasattr(input, 'model_dump') else None

run_kwargs: dict[str, Any] = dict(
    input=input,
    input_data=input_data,  # NEW: serialized for JSON-RPC forwarding
    context=context,
    context_manager=context_manager,
    on_step=wrapped["on_step"],
    on_token=wrapped["on_token"],
    on_content_token=wrapped["on_content_token"],
    on_tool_result=wrapped["on_tool_result"],
    audit_logger=audit_logger,
    artifact_store=scoped_store,
    artifact_context=artifact_context,
)
```

- [ ] **Step 2: 运行已有测试确认无回归**

```bash
uv run pytest tests/agent/test_subagent.py -v -x
```

预期：所有已有测试通过（input_data 不会影响本地 agent）。

- [ ] **Step 3: Commit**

```bash
git add src/agent/agents/subagent/runner.py
git commit -m "feat: serialize structured input as input_data for sub-agent dispatch"
```

---

### Task 5: ProxyAgent.run() 接收并转发 input_data

**Files:**
- Modify: `src/plugin/proxies.py:171-178`
- Modify: `tests/plugin/test_proxies.py` (新增测试)

- [ ] **Step 1: 编写失败的测试**

在 `tests/plugin/test_proxies.py` 的 `TestProxyAgent` 类中添加：

```python
@pytest.mark.asyncio
async def test_run_forwards_input_data(self):
    """ProxyAgent forwards input_data to agent.run JSON-RPC params."""
    agent_spec = {"name": "my_agent", "role": "auditor"}
    mock_client = MockClient(stream_chunks=[
        JSONRPCStreamChunk(
            id=1, status="end",
            result={"status": "completed", "content": "ok"},
        ),
    ])
    proxy = ProxyAgent(mock_client, agent_spec)

    input_data = {"task": "审核格式", "document": {"title": "测试"}, "doc_type": "通知"}

    result = await proxy.run(task="审计文档", input_data=input_data)

    assert result.status == "completed"
    assert mock_client.calls[0]["method"] == "agent.run"
    assert "input_data" in mock_client.calls[0]["params"]
    assert mock_client.calls[0]["params"]["input_data"] == input_data

@pytest.mark.asyncio
async def test_run_without_input_data_is_fine(self):
    """ProxyAgent works fine when input_data is not provided."""
    agent_spec = {"name": "my_agent", "role": "auditor"}
    mock_client = MockClient(stream_chunks=[
        JSONRPCStreamChunk(
            id=1, status="end",
            result={"status": "completed", "content": "ok"},
        ),
    ])
    proxy = ProxyAgent(mock_client, agent_spec)

    result = await proxy.run(task="审计文档")

    assert result.status == "completed"
    params = mock_client.calls[0]["params"]
    assert params.get("input_data") is None
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent::test_run_forwards_input_data -v
```

预期：FAIL — `input_data` 不在 JSON-RPC params 中。

- [ ] **Step 3: 修改 ProxyAgent.run() 转发 input_data**

在 `ProxyAgent.run()` 方法中，`artifact_context` 提取之后添加 `input_data` 提取，与 artifact_context 采用相同模式。

找到 JSON-RPC params 构建（上一步修改后的代码），在 `if artifact_context is not None:` 之后添加：

```python
input_data = kwargs.get("input_data")
```

在 params dict 构建中补充：
```python
params = {
    "agent": self._agent_name,
    "task": task,
    "model_config": model_config,
}
if artifact_context is not None:
    params["artifact_context"] = artifact_context
if input_data is not None:
    params["input_data"] = input_data
params.update({k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS})
```

- [ ] **Step 4: 运行测试确认 PASS**

```bash
uv run pytest tests/plugin/test_proxies.py::TestProxyAgent::test_run_forwards_input_data tests/plugin/test_proxies.py::TestProxyAgent::test_run_without_input_data_is_fine -v
```

预期：两个测试均 PASS。

- [ ] **Step 5: 运行全部 proxy 测试确认无回归**

```bash
uv run pytest tests/plugin/test_proxies.py -v
```

预期：全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/plugin/proxies.py tests/plugin/test_proxies.py
git commit -m "feat: forward input_data in ProxyAgent.run() JSON-RPC params"
```

---

### Task 6: SubAgentConfig 增加 output_artifact_type 字段

**Files:**
- Modify: `src/agent/agents/subagent/config.py:56-70`

- [ ] **Step 1: 在 SubAgentConfig 中添加字段**

在 `SubAgentConfig` dataclass 的字段列表末尾添加：

```python
@dataclass(frozen=True)
class SubAgentConfig:
    agent: Agent
    failure_strategy: FailureStrategy
    max_retries: int = 0
    timeout_seconds: float = 0
    description: str = ""
    extra_params: dict[str, Any] | None = None
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    display_name: str = ""
    output_artifact_type: str | None = None  # NEW
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/agents/subagent/config.py
git commit -m "feat: add output_artifact_type field to SubAgentConfig"
```

---

### Task 7: _SubAgentTool 暴露 output_artifact_type

**Files:**
- Modify: `src/agent/agents/subagent/tool.py:23-41`

- [ ] **Step 1: 编写失败的测试**

在 `tests/agent/test_subagent.py` 中添加测试：

```python
class TestSubAgentToolOutputArtifactType:
    """Tests for output_artifact_type on _SubAgentTool."""
    
    def test_output_artifact_type_from_config(self):
        """_SubAgentTool reads output_artifact_type from SubAgentConfig."""
        from src.agent.agents.subagent import (
            SubAgentRunner, SubAgentConfig, _SubAgentTool,
        )
        from src.agent.agents.echo import create_echo_agent
        
        agent = create_echo_agent()
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
            output_artifact_type="docaudit.audit_report",
        )
        runner = SubAgentRunner()
        tool = _SubAgentTool(name="fmt", config=config, runner=runner)
        
        assert hasattr(tool, "output_artifact_type")
        assert tool.output_artifact_type == "docaudit.audit_report"
    
    def test_output_artifact_type_defaults_to_none(self):
        """_SubAgentTool has output_artifact_type=None when config doesn't set it."""
        from src.agent.agents.echo import create_echo_agent
        
        agent = create_echo_agent()
        config = SubAgentConfig(
            agent=agent,
            failure_strategy=FailureStrategy.TOLERANT,
        )
        runner = SubAgentRunner()
        tool = _SubAgentTool(name="fmt", config=config, runner=runner)
        
        assert tool.output_artifact_type is None
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolOutputArtifactType -v
```

预期：FAIL — `_SubAgentTool` 没有 `output_artifact_type` 属性。

- [ ] **Step 3: 修改 _SubAgentTool.__init__ 读取 output_artifact_type**

在 `_SubAgentTool.__init__()` 方法中（`src/agent/agents/subagent/tool.py` 约 line 23-40），在现有属性赋值后添加：

```python
def __init__(
    self,
    name: str,
    config: SubAgentConfig,
    runner: SubAgentRunner,
    extra_params: dict[str, Any] | None = None,
) -> None:
    self.name = f"run_{name}"
    self.skill = config.display_name or name
    self.description = config.description or config.agent.role
    self._name = name
    self._runner = runner
    self.output_artifact_type = config.output_artifact_type  # NEW

    if config.input_model is not None:
        self.parameters = self._build_parameters_from_model(config)
    else:
        self.parameters = self._build_parameters_legacy(config, extra_params)
```

- [ ] **Step 4: 运行测试确认 PASS**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentToolOutputArtifactType -v
```

预期：两个测试均 PASS。

- [ ] **Step 5: 运行全部 subagent 测试确认无回归**

```bash
uv run pytest tests/agent/test_subagent.py -v
```

预期：全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/agent/agents/subagent/tool.py tests/agent/test_subagent.py
git commit -m "feat: expose output_artifact_type from SubAgentConfig on _SubAgentTool"
```

---

### Task 8: OrchestratorAgent 声明插件子代理的 output_artifact_type

**Files:**
- Modify: `src/agent/agents/orch.py:68-93`

- [ ] **Step 1: 修改 orchestrator 中插件子代理的定义**

在 `OrchestratorAgent.__init__()` 中，插件子代理的 define 调用目前为：

```python
self._subagent_runner.define(
    name,
    SubAgentConfig(
        agent=proxy_agent,
        failure_strategy=FailureStrategy.TOLERANT,
        display_name=proxy_agent.display_name or name,
    ),
)
```

替换为：

```python
self._subagent_runner.define(
    name,
    SubAgentConfig(
        agent=proxy_agent,
        failure_strategy=FailureStrategy.TOLERANT,
        display_name=proxy_agent.display_name or name,
        output_artifact_type="docaudit.audit_report",
    ),
)
```

- [ ] **Step 2: 运行 orchestrator 相关测试确认无回归**

```bash
uv run pytest tests/agent/test_orchestrator.py tests/test_orch_dynamic_agents.py -v
```

预期：全部通过。

- [ ] **Step 3: Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "feat: declare output_artifact_type for plugin sub-agents in orchestrator"
```

---

### Task 9: 验证双向 artifact 流对称性（问题 #4）

**Files:**
- Modify: `tests/agent/test_subagent.py` (集成验证测试)

- [ ] **Step 1: 编写端到端验证测试 — artifact 通过 ToolRegistry 自动注册子代理输出**

在 `tests/agent/test_subagent.py` 中添加：

```python
class TestSubAgentArtifactRegistration:
    """End-to-end tests for sub-agent output artifact registration."""
    
    @pytest.mark.asyncio
    async def test_subagent_output_registers_artifact_when_type_declared(self):
        """When _SubAgentTool has output_artifact_type, ToolRegistry.execute
        should register the result as an artifact."""
        from src.agent.artifacts.store import ArtifactStore
        
        runner = SubAgentRunner()
        agent = _make_agent(model=MockModelClient(tool_calls=[]), name="FormatAuditor")
        runner.define(
            "format_auditor",
            SubAgentConfig(
                agent=agent,
                failure_strategy=FailureStrategy.STRICT,
                output_artifact_type="docaudit.audit_report",
            ),
        )
        
        store = ArtifactStore()
        tools = runner.build_tools()
        
        # Execute the subagent tool with artifact_store
        result = await tools[0].execute(
            task="Check formatting",
            artifact_store=store,
        )
        
        assert result.success is True
        # Verify artifact was registered in store
        candidates = store.list_projection_candidates()
        audit_artifacts = [
            a for a in candidates
            if a.artifact_type == "docaudit.audit_report"
        ]
        assert len(audit_artifacts) >= 1
    
    @pytest.mark.asyncio
    async def test_subagent_without_output_type_does_not_register(self):
        """When output_artifact_type is None, no artifact is registered."""
        from src.agent.artifacts.store import ArtifactStore
        
        runner = SubAgentRunner()
        agent = _make_agent(model=MockModelClient(tool_calls=[]), name="FormatAuditor")
        runner.define(
            "format_auditor",
            SubAgentConfig(
                agent=agent,
                failure_strategy=FailureStrategy.STRICT,
                output_artifact_type=None,
            ),
        )
        
        store = ArtifactStore()
        tools = runner.build_tools()
        
        # Count artifacts before
        before = len(store.list_all())
        
        result = await tools[0].execute(
            task="Check formatting",
            artifact_store=store,
        )
        
        assert result.success is True
        # No new projectable artifact (only the test setup's artifacts)
        after = len(store.list_all())
        # Since output_artifact_type is None, no new artifacts should be registered
        assert after == before
```

- [ ] **Step 2: 运行验证测试**

```bash
uv run pytest tests/agent/test_subagent.py::TestSubAgentArtifactRegistration -v
```

预期：`test_subagent_output_registers_artifact_when_type_declared` PASS，`test_subagent_without_output_type_does_not_register` PASS。

- [ ] **Step 3: Commit**

```bash
git add tests/agent/test_subagent.py
git commit -m "test: add end-to-end verification of sub-agent output artifact registration"
```

---

### Task 10: 运行全部测试套件并最终验证

- [ ] **Step 1: 运行全部相关测试**

```bash
uv run pytest tests/agent/test_subagent.py tests/plugin/test_proxies.py tests/agent/test_orchestrator.py tests/test_orch_dynamic_agents.py tests/agent/test_subagent_coerce.py -v
```

预期：全部通过，无回归。

- [ ] **Step 2: 运行完整测试套件**

```bash
uv run pytest tests/ -v --timeout=60
```

预期：全部通过或已有失败不增加。

- [ ] **Step 3: Final commit (if any stray changes)**

```bash
git status
git diff
```
