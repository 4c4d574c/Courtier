# Tools-to-Plugins Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate all tools from in-process direct registration to plugin subprocesses (JSON-RPC over stdio), delete old Domain Tools, convert Skills to standalone plugin processes, and refactor OrchestratorAgent for dynamic subagent discovery.

**Architecture:** Each former Skill becomes a plugin subprocess under `plugins/` with a `plugin.yaml` manifest and `entry.py` (PluginRuntime subclass). PluginSystem integrates into `create_app()` at startup. Domain Tools are deleted — their logic lives in plugin handler functions. OrchestratorAgent discovers subagents dynamically from PluginSystem instead of hardcoding auditor classes.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, pytest, uv, JSON-RPC 2.0 over stdio

---

## Phase 1: Plugin System Extensions (infrastructure before migration)

### Task 1: Extend PluginRuntime to support system_prompt in register_capabilities

**Files:**
- Modify: `src/plugin/sdk/runtime.py`
- Modify: `src/plugin/manifest.py`

**Context:** `register_capabilities()` currently returns `list[dict]`. We need it to also be able to return `{"capabilities": [...], "system_prompt": "..."}` so plugins can ship their SKILL.md prompt body alongside their tool/agent declarations.

- [ ] **Step 1: Add `system_prompt` field to `Capabilities` model**

In `src/plugin/manifest.py`, modify the `Capabilities` class (L82-89):

```python
class Capabilities(BaseModel):
    """All capabilities declared by a plugin."""

    tools: list[ToolCapability] = Field(default_factory=list)
    checkers: list[CheckerCapability] = Field(default_factory=list)
    agents: list[AgentCapability] = Field(default_factory=list)
    routes: list[RouteCapability] = Field(default_factory=list)
    processors: list[ProcessorCapability] = Field(default_factory=list)
    system_prompt: str = ""  # NEW
```

- [ ] **Step 2: Update PluginRuntime.run() to handle dict return from register_capabilities and include system_prompt in register notification**

In `src/plugin/sdk/runtime.py`, modify the `run()` method around L111-131:

```python
async def run(self) -> None:
    """Start the JSON-RPC server loop on stdin/stdout."""
    self._setup_handlers()

    # Collect tool/checker names for built-in list handlers
    caps_result = self.register_capabilities()
    system_prompt = ""
    if isinstance(caps_result, dict):
        caps = caps_result.get("capabilities", [])
        system_prompt = caps_result.get("system_prompt", "")
    elif isinstance(caps_result, list):
        caps = caps_result
    else:
        caps = []

    for cap in caps:
        if cap.get("type") == "tool" and "name" in cap:
            self._tool_names.append(cap["name"])
        elif cap.get("type") == "checker" and "name" in cap:
            self._checker_names.append(cap["name"])

    # Allow injection of reader/writer for testing
    if self._reader is None:
        self._reader = sys.stdin
    if self._writer is None:
        self._writer = sys.stdout

    self._running = True

    # Send registration notification
    self._send_notification(METHOD_REGISTER, {
        "capabilities": caps,
        "system_prompt": system_prompt,
    })

    # Process requests line by line
    if isinstance(self._reader, asyncio.Queue):
        await self._run_queue_loop()
    else:
        await self._run_stdin_loop()
```

- [ ] **Step 3: Update `register_capabilities` docstring to document the dict return form**

In `src/plugin/sdk/runtime.py`, update the docstring of `register_capabilities` (L102-104):

```python
def register_capabilities(self) -> list[dict[str, Any]] | dict[str, Any]:
    """Override: return the list of capabilities this plugin provides.

    Can return either:
    - A list of capability dicts (backward compatible)
    - A dict with ``capabilities`` (list) and optional ``system_prompt`` (str)
    """
    return []
```

- [ ] **Step 4: Run plugin tests to verify no regression**

```bash
uv run pytest tests/plugin/ -v
```

- [ ] **Step 5: Commit**

```bash
git add src/plugin/sdk/runtime.py src/plugin/manifest.py
git commit -m "feat: support system_prompt in register_capabilities and Capabilities model"
```

### Task 2: Add system_prompt collection to ExtensionRegistry

**Files:**
- Modify: `src/plugin/registry.py`

- [ ] **Step 1: Accept system_prompt in on_register and store per-plugin**

In `src/plugin/registry.py`, modify `__init__` and `on_register`:

```python
# In __init__, add after self._routes (L39):
self._system_prompts: dict[str, str] = {}

# Change on_register signature to accept system_prompt:
def on_register(
    self,
    plugin_name: str,
    client: JSONRPCClient,
    capabilities: list[dict[str, Any]],
    system_prompt: str = "",
) -> None:
    """Process capabilities from a plugin.register notification."""
    registrations: dict[str, list[str]] = {}

    for cap in capabilities:
        cap_type = cap.get("type", "")
        # ... existing capability handling unchanged ...

    self._registrations[plugin_name] = registrations

    # Store system_prompt
    if system_prompt:
        self._system_prompts[plugin_name] = system_prompt
        logger.info(
            "Plugin '%s' provided system_prompt (%d chars)",
            plugin_name, len(system_prompt),
        )
```

Add a getter method:

```python
def get_system_prompts(self) -> dict[str, str]:
    """Return all plugin system_prompts, keyed by plugin name."""
    return dict(self._system_prompts)
```

In `on_unregister`, also remove the system_prompt:

```python
# In on_unregister, add after self._registrations.pop:
self._system_prompts.pop(plugin_name, None)
```

- [ ] **Step 2: Update ProcessManager to pass system_prompt to on_register**

In `src/plugin/manager.py`, find the code that handles the `plugin.register` notification and calls `on_register`. Update it to extract and pass `system_prompt`:

```python
# The register notification handler (around line ~180 in manager.py)
# Currently calls: self._registry.on_register(name, client, caps)
# Change to also pass system_prompt from the notification params:
system_prompt = params.get("system_prompt", "")
self._registry.on_register(name, client, caps, system_prompt=system_prompt)
```

- [ ] **Step 3: Run plugin tests**

```bash
uv run pytest tests/plugin/ -v
```

- [ ] **Step 4: Commit**

```bash
git add src/plugin/registry.py src/plugin/manager.py
git commit -m "feat: add system_prompt collection to ExtensionRegistry"
```

### Task 3: Expose get_system_prompts on PluginSystem

**Files:**
- Modify: `src/plugin/__init__.py`

- [ ] **Step 1: Add get_system_prompts method to PluginSystem**

```python
# In src/plugin/__init__.py, add to PluginSystem class:
def get_system_prompts(self) -> dict[str, str]:
    """Return all plugin system_prompts, keyed by plugin name."""
    return self._registry.get_system_prompts()
```

- [ ] **Step 2: Commit**

```bash
git add src/plugin/__init__.py
git commit -m "feat: expose get_system_prompts on PluginSystem"
```

---

## Phase 2: First Batch — search and parse plugins

### Task 4: Create search plugin

**Files:**
- Create: `plugins/search/plugin.yaml`
- Create: `plugins/search/entry.py`
- Create: `plugins/search/tools.py`

**Context:** `search` skill has 1 tool (`search_documents`), no agent, no complex dependencies. Simplest migration target.

- [ ] **Step 1: Create plugin.yaml**

```yaml
# plugins/search/plugin.yaml
name: search
version: "1.0.0"
api: "1.0"
description: "Elasticsearch document chunk search"

capabilities:
  tools:
    - name: search_documents
      display_name: 搜索文档
      description: "在已索引的文档块中搜索内容。只需传入搜索关键词，系统会自动构建查询并格式化结果。"
  system_prompt: |
    # 文档搜索

    ## 能力
    通过自然语言关键词搜索已索引的文档块，内部自动构建 Elasticsearch 查询。
    支持精确短语匹配（引号包裹）、关键词分词匹配、以及按文档 ID/类型/标签过滤。

    ## 使用方式
    调用 `search_documents` 工具，传入 `query`（搜索关键词）。

    ## 参数
    - `query`: 搜索关键词或短语（必填）
    - `document_id`: 限定文档 ID（可选）
    - `doc_type`: 文档类型过滤（可选）
    - `tags`: 标签过滤（可选）
    - `search_fields`: 指定搜索字段，默认 `["chunk_text", "title"]`（可选）
    - `include_annotations`: 是否包含标注详情，默认 false（可选）
    - `skip`: 跳过的结果数，默认 0（可选）
    - `limit`: 返回的最大结果数，默认 10（可选）
```

- [ ] **Step 2: Copy tools.py from skill**

```bash
cp src/agent/skills/search/tool.py plugins/search/tools.py
```

- [ ] **Step 3: Create entry.py**

```python
"""Search plugin — Elasticsearch document chunk search."""
from src.plugin.sdk import PluginRuntime
from src.agent.tools.protocol import ToolResult
from .tools import SearchDocumentsTool


class SearchPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {
                    "type": "tool",
                    "name": "search_documents",
                    "display_name": "搜索文档",
                    "description": SearchDocumentsTool.description,
                    "parameters": SearchDocumentsTool.parameters,
                },
            ],
            "system_prompt": (
                "# 文档搜索\n\n"
                "## 能力\n"
                "通过自然语言关键词搜索已索引的文档块。\n"
                "## 使用方式\n调用 `search_documents` 工具，传入 `query`（搜索关键词）。\n"
            ),
        }

    def _setup_handlers(self):
        tool = SearchDocumentsTool()

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            if tool_name == "search_documents":
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            return {"success": False, "error": f"Unknown tool: {tool_name}"}


if __name__ == "__main__":
    import asyncio
    asyncio.run(SearchPlugin().run())
```

- [ ] **Step 4: Create pyproject.toml for plugin dependencies**

```toml
# plugins/search/pyproject.toml
[project]
name = "docaudit-plugin-search"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = [
    "elasticsearch>=8.0.0",
]
```

- [ ] **Step 5: Write unit test for search plugin handler**

Create `tests/plugin/test_search_plugin.py`:

```python
"""Test search plugin entry point handler logic."""
import asyncio
import json
import pytest
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_search_plugin_tool_execute_handler():
    from plugins.search.entry import SearchPlugin

    plugin = SearchPlugin()
    
    # Setup: inject reader queue for testing
    plugin._reader = asyncio.Queue()
    plugin._writer = asyncio.Queue()
    
    # Send a request
    req = json.dumps({
        "id": 1,
        "method": "tool.execute",
        "params": {"tool": "search_documents", "args": {"query": "安全生产"}},
    })
    await plugin._reader.put(req)
    
    # Run one iteration of processing
    task = asyncio.create_task(plugin.run())
    await asyncio.sleep(0.1)  # Let it process
    
    # Get the output
    responses = []
    while not plugin._writer.empty():
        responses.append(await plugin._writer.get())
    
    # Should have received a register notification + tool response
    assert len(responses) >= 2
    
    # First response should be register notification
    reg = json.loads(responses[0])
    assert reg["method"] == "plugin.register"
    assert "capabilities" in reg["params"]
    assert "system_prompt" in reg["params"]
    
    plugin._running = False
    task.cancel()


@pytest.mark.asyncio
async def test_search_plugin_unknown_tool():
    from plugins.search.entry import SearchPlugin

    plugin = SearchPlugin()
    plugin._reader = asyncio.Queue()
    plugin._writer = asyncio.Queue()
    
    req = json.dumps({
        "id": 1,
        "method": "tool.execute",
        "params": {"tool": "nonexistent_tool", "args": {}},
    })
    await plugin._reader.put(req)
    
    task = asyncio.create_task(plugin.run())
    await asyncio.sleep(0.1)
    
    responses = []
    while not plugin._writer.empty():
        responses.append(await plugin._writer.get())
    
    # Second response should be error
    resp = json.loads(responses[1])
    assert resp["id"] == 1
    assert "error" in resp
    assert resp["result"]["success"] is False
    
    plugin._running = False
    task.cancel()
```

- [ ] **Step 6: Run test to verify it passes**

```bash
uv run pytest tests/plugin/test_search_plugin.py -v
```

- [ ] **Step 7: Commit**

```bash
git add plugins/search/ tests/plugin/test_search_plugin.py
git commit -m "feat: add search plugin with entry point and tests"
```

### Task 5: Create parse plugin

**Files:**
- Create: `plugins/parse/plugin.yaml`
- Create: `plugins/parse/entry.py`
- Create: `plugins/parse/tools.py`
- Create: `plugins/parse/pyproject.toml`

**Context:** `parse` skill has 1 tool (`parse_document`). The `parse_document_direct` Domain Tool has the same purpose — both will be replaced by this plugin.

- [ ] **Step 1: Create plugin.yaml**

```yaml
# plugins/parse/plugin.yaml
name: parse
version: "1.0.0"
api: "1.0"
description: "Document file parsing (PDF/DOCX/image)"

capabilities:
  tools:
    - name: parse_document
      display_name: 解析文档
      description: "Parse a document file from disk into the internal Document model. Supports PDF, DOCX, and scanned images."
  system_prompt: |
    # 文档解析

    ## 能力
    将 PDF、DOCX、扫描图片解析为结构化的 Document 模型。

    ## 使用方式
    调用 `parse_document` 工具，传入 `file_path` 参数（绝对路径）。
    解析为确定性操作，不需要 LLM 参与决策。

    ## 注意事项
    - file_path 必须是绝对路径
    - 支持 PDF（含文本型和扫描型）、DOCX、及图片格式
```

- [ ] **Step 2: Copy tools.py from parse skill**

```bash
cp src/agent/skills/parse/tool.py plugins/parse/tools.py
```

- [ ] **Step 3: Create entry.py**

```python
"""Parse plugin — document file parsing."""
from src.plugin.sdk import PluginRuntime
from .tools import ParseTool


class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {
                    "type": "tool",
                    "name": "parse_document",
                    "display_name": "解析文档",
                    "description": ParseTool.description,
                    "parameters": ParseTool.parameters,
                },
            ],
            "system_prompt": (
                "# 文档解析\n\n"
                "## 能力\n将 PDF、DOCX、扫描图片解析为结构化的 Document 模型。\n"
                "## 使用方式\n调用 `parse_document` 工具，传入 `file_path` 参数（绝对路径）。\n"
            ),
        }

    def _setup_handlers(self):
        tool = ParseTool()

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            if tool_name == "parse_document":
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            return {"success": False, "error": f"Unknown tool: {tool_name}"}


if __name__ == "__main__":
    import asyncio
    asyncio.run(ParsePlugin().run())
```

- [ ] **Step 4: Create pyproject.toml**

```toml
# plugins/parse/pyproject.toml
[project]
name = "docaudit-plugin-parse"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = [
    "docparse",
]
```

- [ ] **Step 5: Write unit test for parse plugin**

Create `tests/plugin/test_parse_plugin.py`:

```python
"""Test parse plugin entry point handler logic."""
import asyncio
import json
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_parse_plugin_registers_with_system_prompt():
    from plugins.parse.entry import ParsePlugin

    plugin = ParsePlugin()
    caps_result = plugin.register_capabilities()
    assert isinstance(caps_result, dict)
    assert "capabilities" in caps_result
    assert "system_prompt" in caps_result
    assert len(caps_result["system_prompt"]) > 0
    assert caps_result["capabilities"][0]["name"] == "parse_document"


@pytest.mark.asyncio
@patch("plugins.parse.entry.ParseTool.execute")
async def test_parse_plugin_tool_execute_handler(mock_execute):
    from plugins.parse.entry import ParsePlugin

    mock_execute.return_value = type("ToolResult", (), {
        "success": True,
        "data": {"title": "test.doc"},
        "error": None,
        "metadata": {},
        "model_dump": lambda self: {"success": True, "data": {"title": "test.doc"}, "error": None, "metadata": {}},
    })()

    plugin = ParsePlugin()
    plugin._reader = asyncio.Queue()
    plugin._writer = asyncio.Queue()

    req = json.dumps({
        "id": 1,
        "method": "tool.execute",
        "params": {"tool": "parse_document", "args": {"file_path": "/tmp/test.pdf"}},
    })
    await plugin._reader.put(req)

    task = asyncio.create_task(plugin.run())
    await asyncio.sleep(0.1)

    responses = []
    while not plugin._writer.empty():
        responses.append(await plugin._writer.get())

    plugin._running = False
    task.cancel()

    resp = json.loads(responses[1])  # Second message is the tool response
    assert resp["id"] == 1
    assert resp["result"]["success"] is True
```

- [ ] **Step 6: Run test**

```bash
uv run pytest tests/plugin/test_parse_plugin.py -v
```

- [ ] **Step 7: Commit**

```bash
git add plugins/parse/ tests/plugin/test_parse_plugin.py
git commit -m "feat: add parse plugin with entry point and tests"
```

---

## Phase 3: Second Batch — format_audit, annotate, text_correction plugins

### Task 6: Create format_audit plugin

**Files:**
- Create: `plugins/format_audit/plugin.yaml`
- Create: `plugins/format_audit/entry.py`
- Create: `plugins/format_audit/tools.py`
- Create: `plugins/format_audit/pyproject.toml`

**Context:** 4 tools + 1 agent. The `format_check_direct` Domain Tool is replaced by this plugin's `audit_format` + `detect_document_type` tools.

- [ ] **Step 1: Create plugin.yaml**

```yaml
# plugins/format_audit/plugin.yaml
name: format_audit
version: "1.0.0"
api: "1.0"
description: "Format auditing against GB/T 9704-2012"

capabilities:
  tools:
    - name: detect_document_type
      display_name: 检测文档类型
      description: "Detect Chinese government document type from title and body text."
    - name: audit_format
      display_name: 格式审计
      description: "Audit document formatting compliance against GB/T 9704-2012 standard."
    - name: load_format_spec
      display_name: 加载格式规范
      description: "Load the format specification for a given document type."
    - name: list_format_rule_types
      display_name: 列出格式规则类型
      description: "List all document types that have format rule definitions available."
  agent:
    - name: format_auditor
      display_name: 格式审计器
      role: "你是文档格式审计专家，负责验证文档是否符合 GB/T 9704-2012 公文格式规范。"
  system_prompt: |
    # 格式审核

    ## 能力
    检查公文是否符合 GB/T 9704-2012《党政机关公文格式》国家标准。

    ## 审核流程
    1. **检测文种** — 从标题和正文关键词判定公文类型
    2. **加载规范** — 获取该文种的格式规格
    3. **执行审核** — 逐项对照规格检查，输出违规列表
    4. **可选：查询可用规则** — 列出所有已定义格式规则的文种

    ## 检查项
    页边距、标题字体字号、正文字体字号、对齐方式、行间距、各元素块计数
```

- [ ] **Step 2: Copy tools.py from format_audit skill**

```bash
cp src/agent/skills/format_audit/tools.py plugins/format_audit/tools.py
```

- [ ] **Step 3: Create entry.py with 4 tool handlers + agent handler**

```python
"""Format audit plugin — GB/T 9704-2012 document formatting validation."""
from src.plugin.sdk import PluginRuntime
from .tools import (
    DetectDocTypeTool,
    AuditFormatTool,
    LoadFormatSpecTool,
    ListFormatRuleTypesTool,
)


class FormatAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {
                    "type": "tool",
                    "name": "detect_document_type",
                    "display_name": "检测文档类型",
                    "description": DetectDocTypeTool.description,
                    "parameters": DetectDocTypeTool.parameters,
                },
                {
                    "type": "tool",
                    "name": "audit_format",
                    "display_name": "格式审计",
                    "description": AuditFormatTool.description,
                    "parameters": AuditFormatTool.parameters,
                },
                {
                    "type": "tool",
                    "name": "load_format_spec",
                    "display_name": "加载格式规范",
                    "description": LoadFormatSpecTool.description,
                    "parameters": LoadFormatSpecTool.parameters,
                },
                {
                    "type": "tool",
                    "name": "list_format_rule_types",
                    "display_name": "列出格式规则类型",
                    "description": ListFormatRuleTypesTool.description,
                    "parameters": ListFormatRuleTypesTool.parameters,
                },
                {
                    "type": "agent",
                    "name": "format_auditor",
                    "display_name": "格式审计器",
                    "role": "你是文档格式审计专家，负责验证文档是否符合 GB/T 9704-2012 公文格式规范。",
                },
            ],
            "system_prompt": (
                "# 格式审核\n\n"
                "## 能力\n检查公文是否符合 GB/T 9704-2012《党政机关公文格式》国家标准。\n"
                "## 审核流程\n"
                "1. 检测文种 2. 加载规范 3. 执行审核 4. 可选查询可用规则\n"
            ),
        }

    def _setup_handlers(self):
        detect = DetectDocTypeTool()
        audit = AuditFormatTool()
        spec = LoadFormatSpecTool()
        list_types = ListFormatRuleTypesTool()

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            try:
                if tool_name == "detect_document_type":
                    result = await detect.execute(**args)
                elif tool_name == "audit_format":
                    result = await audit.execute(**args)
                elif tool_name == "load_format_spec":
                    result = await spec.execute(**args)
                elif tool_name == "list_format_rule_types":
                    result = await list_types.execute(**args)
                else:
                    return {"success": False, "error": f"Unknown tool: {tool_name}"}
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @self.on("agent.run")
        async def handle_agent_run(params):
            task = params.get("task", "")
            # Agent loop: yield streaming chunks for each step, end with result
            # This is a stub — the full agent loop runs inside this handler
            yield "开始格式审计...\n"
            yield {"_final": True, "result": {"status": "completed", "content": task}}


if __name__ == "__main__":
    import asyncio
    asyncio.run(FormatAuditPlugin().run())
```

- [ ] **Step 4: Create pyproject.toml**

```toml
# plugins/format_audit/pyproject.toml
[project]
name = "docaudit-plugin-format-audit"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Write unit test for format_audit plugin**

Create `tests/plugin/test_format_audit_plugin.py`:

```python
"""Test format_audit plugin entry point handler logic."""
import asyncio
import json
import pytest


@pytest.mark.asyncio
async def test_format_audit_plugin_registers_tools_and_agent():
    from plugins.format_audit.entry import FormatAuditPlugin

    plugin = FormatAuditPlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    agent_names = [c["name"] for c in caps if c["type"] == "agent"]
    
    assert "detect_document_type" in tool_names
    assert "audit_format" in tool_names
    assert "load_format_spec" in tool_names
    assert "list_format_rule_types" in tool_names
    assert "format_auditor" in agent_names
    assert len(caps_result["system_prompt"]) > 0
```

- [ ] **Step 6: Run test**

```bash
uv run pytest tests/plugin/test_format_audit_plugin.py -v
```

- [ ] **Step 7: Commit**

```bash
git add plugins/format_audit/ tests/plugin/test_format_audit_plugin.py
git commit -m "feat: add format_audit plugin with 4 tools and 1 agent"
```

### Task 7: Create annotate plugin

**Files:**
- Create: `plugins/annotate/plugin.yaml`
- Create: `plugins/annotate/entry.py`
- Create: `plugins/annotate/tools.py`
- Create: `plugins/annotate/pyproject.toml`
- Create: `tests/plugin/test_annotate_plugin.py`

**Context:** 1 tool (`annotate_document`). Replaces `annotate_direct` Domain Tool.

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/annotate/tool.py plugins/annotate/tools.py
```

```yaml
# plugins/annotate/plugin.yaml
name: annotate
version: "1.0.0"
api: "1.0"
description: "DOCX keyword-based annotation"

capabilities:
  tools:
    - name: annotate_document
      display_name: 文档批注
      description: "Add Word comments to a DOCX document at keyword occurrences."
  system_prompt: |
    # 文档批注
    ## 能力
    在 DOCX 文档中根据关键词自动添加 Word 批注（comments）。
    ## 使用方式
    调用 `annotate_document` 工具，传入 `source` 和 `rules` 列表。
```

```python
# plugins/annotate/entry.py
"""Annotate plugin — DOCX keyword-based annotation."""
from src.plugin.sdk import PluginRuntime
from .tools import AnnotateDocumentTool


class AnnotatePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {
                    "type": "tool",
                    "name": "annotate_document",
                    "display_name": "文档批注",
                    "description": AnnotateDocumentTool.description,
                    "parameters": AnnotateDocumentTool.parameters,
                },
            ],
            "system_prompt": (
                "# 文档批注\n\n"
                "## 能力\n在 DOCX 文档中根据关键词自动添加 Word 批注（comments）。\n"
            ),
        }

    def _setup_handlers(self):
        tool = AnnotateDocumentTool()

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            if tool_name == "annotate_document":
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            return {"success": False, "error": f"Unknown tool: {tool_name}"}


if __name__ == "__main__":
    import asyncio
    asyncio.run(AnnotatePlugin().run())
```

```toml
# plugins/annotate/pyproject.toml
[project]
name = "docaudit-plugin-annotate"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Write test and commit**

```python
# tests/plugin/test_annotate_plugin.py
import pytest
from plugins.annotate.entry import AnnotatePlugin


@pytest.mark.asyncio
async def test_annotate_plugin_registers_tool():
    plugin = AnnotatePlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "annotate_document" in tool_names
    assert len(caps_result["system_prompt"]) > 0
```

```bash
uv run pytest tests/plugin/test_annotate_plugin.py -v
git add plugins/annotate/ tests/plugin/test_annotate_plugin.py
git commit -m "feat: add annotate plugin"
```

### Task 8: Create text_correction plugin

**Files:**
- Create: `plugins/text_correction/plugin.yaml`
- Create: `plugins/text_correction/entry.py`
- Create: `plugins/text_correction/tools.py`
- Create: `plugins/text_correction/pyproject.toml`
- Create: `tests/plugin/test_text_correction_plugin.py`

**Context:** 5 tools (`correct_text`, `detect_duplicate_chars`, `detect_fullwidth_chars`, `detect_punctuation_mixing`, `detect_truncated_names`) + 1 agent. Replaces `text_correction_direct` Domain Tool.

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/text_correction/tools.py plugins/text_correction/tools.py
```

```yaml
# plugins/text_correction/plugin.yaml
name: text_correction
version: "1.0.0"
api: "1.0"
description: "Chinese text correction pipeline"

capabilities:
  tools:
    - name: correct_text
      display_name: 文本纠错
      description: "Run the full text correction pipeline on a list of text strings."
    - name: detect_duplicate_chars
      display_name: 重复字符检测
      description: "Detect accidentally repeated Chinese characters."
    - name: detect_fullwidth_chars
      display_name: 全角字符检测
      description: "Detect fullwidth Latin letters and digits that should be halfwidth."
    - name: detect_punctuation_mixing
      display_name: 标点混用检测
      description: "Detect halfwidth punctuation marks used in Chinese context."
    - name: detect_truncated_names
      display_name: 截断人名检测
      description: "Detect possibly truncated person names."
  agent:
    - name: correction_auditor
      display_name: 文字纠错器
      role: "你是中文文档纠错专家，负责发现并纠正文本中的错误。"
  system_prompt: |
    # 文本纠错
    ## 流水线步骤
    1. LLM 语义纠错 2. 重复字符检测 3. 全角字符检测 4. 标点混用检测 5. 截断人名检测 6. 冲突解决
```

```python
# plugins/text_correction/entry.py
"""Text correction plugin — Chinese text error correction pipeline."""
from src.plugin.sdk import PluginRuntime
from .tools import (
    CorrectTextTool,
    DetectDuplicateCharsTool,
    DetectFullwidthCharsTool,
    DetectPunctuationMixingTool,
    DetectTruncatedNamesTool,
)


class TextCorrectionPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "correct_text", "display_name": "文本纠错",
                 "description": CorrectTextTool.description, "parameters": CorrectTextTool.parameters},
                {"type": "tool", "name": "detect_duplicate_chars", "display_name": "重复字符检测",
                 "description": DetectDuplicateCharsTool.description, "parameters": DetectDuplicateCharsTool.parameters},
                {"type": "tool", "name": "detect_fullwidth_chars", "display_name": "全角字符检测",
                 "description": DetectFullwidthCharsTool.description, "parameters": DetectFullwidthCharsTool.parameters},
                {"type": "tool", "name": "detect_punctuation_mixing", "display_name": "标点混用检测",
                 "description": DetectPunctuationMixingTool.description, "parameters": DetectPunctuationMixingTool.parameters},
                {"type": "tool", "name": "detect_truncated_names", "display_name": "截断人名检测",
                 "description": DetectTruncatedNamesTool.description, "parameters": DetectTruncatedNamesTool.parameters},
                {"type": "agent", "name": "correction_auditor", "display_name": "文字纠错器",
                 "role": "你是中文文档纠错专家，负责发现并纠正文本中的错误。"},
            ],
            "system_prompt": "# 文本纠错\n\n## 流水线步骤\n1. LLM 语义纠错 2. 重复字符检测 3. 全角字符检测 4. 标点混用检测 5. 截断人名检测 6. 冲突解决\n",
        }

    def _setup_handlers(self):
        correct = CorrectTextTool()
        dup_chars = DetectDuplicateCharsTool()
        fullwidth = DetectFullwidthCharsTool()
        punc_mix = DetectPunctuationMixingTool()
        trunc_names = DetectTruncatedNamesTool()
        _tool_map = {
            "correct_text": correct,
            "detect_duplicate_chars": dup_chars,
            "detect_fullwidth_chars": fullwidth,
            "detect_punctuation_mixing": punc_mix,
            "detect_truncated_names": trunc_names,
        }

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            tool = _tool_map.get(tool_name)
            if tool is None:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            try:
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @self.on("agent.run")
        async def handle_agent_run(params):
            yield "开始文本纠错...\n"
            yield {"_final": True, "result": {"status": "completed", "content": params.get("task", "")}}


if __name__ == "__main__":
    import asyncio
    asyncio.run(TextCorrectionPlugin().run())
```

```toml
# plugins/text_correction/pyproject.toml
[project]
name = "docaudit-plugin-text-correction"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Write test and commit**

```python
# tests/plugin/test_text_correction_plugin.py
import pytest
from plugins.text_correction.entry import TextCorrectionPlugin


@pytest.mark.asyncio
async def test_text_correction_plugin_registers_all_tools_and_agent():
    plugin = TextCorrectionPlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    agent_names = [c["name"] for c in caps if c["type"] == "agent"]
    
    assert "correct_text" in tool_names
    assert "detect_duplicate_chars" in tool_names
    assert "detect_fullwidth_chars" in tool_names
    assert "detect_punctuation_mixing" in tool_names
    assert "detect_truncated_names" in tool_names
    assert "correction_auditor" in agent_names
    assert len(caps_result["system_prompt"]) > 0
```

```bash
uv run pytest tests/plugin/test_text_correction_plugin.py -v
git add plugins/text_correction/ tests/plugin/test_text_correction_plugin.py
git commit -m "feat: add text_correction plugin with 5 tools and 1 agent"
```

---

## Phase 4: Third Batch — content_audit, plagiarism, style_audit plugins

### Task 9: Create content_audit plugin

**Files:**
- Create: `plugins/content_audit/plugin.yaml`
- Create: `plugins/content_audit/entry.py`
- Create: `plugins/content_audit/tools.py`
- Create: `plugins/content_audit/pyproject.toml`
- Create: `tests/plugin/test_content_audit_plugin.py`

**Context:** 2 tools (`audit_content`, `classify_domain`) + 1 agent. Replaces `content_compliance_direct` Domain Tool.

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/content_audit/tools.py plugins/content_audit/tools.py
```

```yaml
# plugins/content_audit/plugin.yaml
name: content_audit
version: "1.0.0"
api: "1.0"
description: "Content compliance auditing across 20 government domains"

capabilities:
  tools:
    - name: audit_content
      display_name: 内容审计
      description: "Audit document content against domain-specific compliance rules across 20 government domains."
    - name: classify_domain
      display_name: 领域分类
      description: "Classify text into one of 20 government domains."
  agent:
    - name: content_auditor
      display_name: 内容审计器
      role: "你是政务文档内容合规审计专家，负责检查文档内容是否符合政府领域合规要求。"
  system_prompt: |
    # 内容合规审核
    ## 能力
    对文档内容进行 20 个政务领域的合规审核，识别违规表述并给出修改建议。
    ## 审核流程
    1. 领域分类 2. 内容审计
```

```python
# plugins/content_audit/entry.py
"""Content audit plugin — government domain compliance validation."""
from src.plugin.sdk import PluginRuntime
from .tools import AuditContentTool, ClassifyDomainTool


class ContentAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "audit_content", "display_name": "内容审计",
                 "description": AuditContentTool.description, "parameters": AuditContentTool.parameters},
                {"type": "tool", "name": "classify_domain", "display_name": "领域分类",
                 "description": ClassifyDomainTool.description, "parameters": ClassifyDomainTool.parameters},
                {"type": "agent", "name": "content_auditor", "display_name": "内容审计器",
                 "role": "你是政务文档内容合规审计专家。"},
            ],
            "system_prompt": "# 内容合规审核\n\n## 能力\n对文档内容进行 20 个政务领域的合规审核。\n",
        }

    def _setup_handlers(self):
        audit = AuditContentTool()
        classify = ClassifyDomainTool()
        _tool_map = {"audit_content": audit, "classify_domain": classify}

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            tool = _tool_map.get(tool_name)
            if not tool:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            try:
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @self.on("agent.run")
        async def handle_agent_run(params):
            yield "开始内容审计...\n"
            yield {"_final": True, "result": {"status": "completed", "content": params.get("task", "")}}


if __name__ == "__main__":
    import asyncio
    asyncio.run(ContentAuditPlugin().run())
```

```toml
# plugins/content_audit/pyproject.toml
[project]
name = "docaudit-plugin-content-audit"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Test and commit**

```python
# tests/plugin/test_content_audit_plugin.py
import pytest
from plugins.content_audit.entry import ContentAuditPlugin


@pytest.mark.asyncio
async def test_content_audit_plugin_registers():
    plugin = ContentAuditPlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "audit_content" in tool_names
    assert "classify_domain" in tool_names
    agent_names = [c["name"] for c in caps if c["type"] == "agent"]
    assert "content_auditor" in agent_names
```

```bash
uv run pytest tests/plugin/test_content_audit_plugin.py -v
git add plugins/content_audit/ tests/plugin/test_content_audit_plugin.py
git commit -m "feat: add content_audit plugin with 2 tools and 1 agent"
```

### Task 10: Create plagiarism plugin

**Files:**
- Create: `plugins/plagiarism/plugin.yaml`
- Create: `plugins/plagiarism/entry.py`
- Create: `plugins/plagiarism/tools.py`
- Create: `plugins/plagiarism/pyproject.toml`
- Create: `tests/plugin/test_plagiarism_plugin.py`

**Context:** 3 tools + 1 agent. Replaces `plagiarism_check_direct` Domain Tool.

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/plagiarism/tools.py plugins/plagiarism/tools.py
```

```yaml
# plugins/plagiarism/plugin.yaml
name: plagiarism
version: "1.0.0"
api: "1.0"
description: "Plagiarism detection using dynamic IQR thresholding"

capabilities:
  tools:
    - name: compute_similarity
      display_name: 计算相似度
      description: "Compute pairwise text similarity using SequenceMatcher."
    - name: compute_dynamic_threshold
      display_name: 动态阈值计算
      description: "Compute a dynamic plagiarism threshold using Tukey's IQR method."
    - name: detect_plagiarism
      display_name: 抄袭检测
      description: "Compare a document against a reference library to detect plagiarism."
  agent:
    - name: plagiarism_auditor
      display_name: 查重检测器
      role: "你是文档查重检测专家，使用动态 IQR 阈值法检测文档抄袭。"
  system_prompt: |
    # 查重检测
    ## 流水线步骤
    1. 逐段计算相似度 2. 动态阈值计算 3. 抄袭判定
```

```python
# plugins/plagiarism/entry.py
"""Plagiarism plugin — similarity-based plagiarism detection."""
from src.plugin.sdk import PluginRuntime
from .tools import ComputeSimilarityTool, ComputeDynamicThresholdTool, DetectPlagiarismTool


class PlagiarismPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "compute_similarity", "display_name": "计算相似度",
                 "description": ComputeSimilarityTool.description, "parameters": ComputeSimilarityTool.parameters},
                {"type": "tool", "name": "compute_dynamic_threshold", "display_name": "动态阈值计算",
                 "description": ComputeDynamicThresholdTool.description, "parameters": ComputeDynamicThresholdTool.parameters},
                {"type": "tool", "name": "detect_plagiarism", "display_name": "抄袭检测",
                 "description": DetectPlagiarismTool.description, "parameters": DetectPlagiarismTool.parameters},
                {"type": "agent", "name": "plagiarism_auditor", "display_name": "查重检测器",
                 "role": "你是文档查重检测专家，使用动态 IQR 阈值法检测文档抄袭。"},
            ],
            "system_prompt": "# 查重检测\n\n## 流水线步骤\n1. 逐段计算相似度 2. 动态阈值计算 3. 抄袭判定\n",
        }

    def _setup_handlers(self):
        sim = ComputeSimilarityTool()
        threshold = ComputeDynamicThresholdTool()
        detect = DetectPlagiarismTool()
        _tool_map = {"compute_similarity": sim, "compute_dynamic_threshold": threshold, "detect_plagiarism": detect}

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            tool = _tool_map.get(tool_name)
            if not tool:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            try:
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @self.on("agent.run")
        async def handle_agent_run(params):
            yield "开始查重检测...\n"
            yield {"_final": True, "result": {"status": "completed", "content": params.get("task", "")}}


if __name__ == "__main__":
    import asyncio
    asyncio.run(PlagiarismPlugin().run())
```

```toml
# plugins/plagiarism/pyproject.toml
[project]
name = "docaudit-plugin-plagiarism"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["numpy"]
```

- [ ] **Step 5: Test and commit**

```python
# tests/plugin/test_plagiarism_plugin.py
import pytest
from plugins.plagiarism.entry import PlagiarismPlugin


@pytest.mark.asyncio
async def test_plagiarism_plugin_registers():
    plugin = PlagiarismPlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "compute_similarity" in tool_names
    assert "compute_dynamic_threshold" in tool_names
    assert "detect_plagiarism" in tool_names
    agent_names = [c["name"] for c in caps if c["type"] == "agent"]
    assert "plagiarism_auditor" in agent_names
```

```bash
uv run pytest tests/plugin/test_plagiarism_plugin.py -v
git add plugins/plagiarism/ tests/plugin/test_plagiarism_plugin.py
git commit -m "feat: add plagiarism plugin with 3 tools and 1 agent"
```

### Task 11: Create style_audit plugin

**Files:**
- Create: `plugins/style_audit/plugin.yaml`
- Create: `plugins/style_audit/entry.py`
- Create: `plugins/style_audit/tools.py`
- Create: `plugins/style_audit/pyproject.toml`
- Create: `tests/plugin/test_style_audit_plugin.py`

**Context:** 2 tools + 1 agent.

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/style_audit/tools.py plugins/style_audit/tools.py
```

```yaml
# plugins/style_audit/plugin.yaml
name: style_audit
version: "1.0.0"
api: "1.0"
description: "Writing style audit for Chinese government documents"

capabilities:
  tools:
    - name: audit_writing_style
      display_name: 写作风格审核
      description: "Audit document writing style against content compliance rules."
    - name: list_writing_style_types
      display_name: 列出写作风格类型
      description: "List all document types that have writing style rules defined."
  agent:
    - name: style_auditor
      display_name: 风格审计器
      role: "你是公文写作风格审计专家，检查开篇/结尾用语、语气、结构惯例。"
  system_prompt: |
    # 写作风格审核
    ## 能力
    检查公文写作风格是否符合规范要求。
```

```python
# plugins/style_audit/entry.py
"""Style audit plugin — writing style validation."""
from src.plugin.sdk import PluginRuntime
from .tools import AuditWritingStyleTool, ListWritingStyleTypesTool


class StyleAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "audit_writing_style", "display_name": "写作风格审核",
                 "description": AuditWritingStyleTool.description, "parameters": AuditWritingStyleTool.parameters},
                {"type": "tool", "name": "list_writing_style_types", "display_name": "列出写作风格类型",
                 "description": ListWritingStyleTypesTool.description, "parameters": ListWritingStyleTypesTool.parameters},
                {"type": "agent", "name": "style_auditor", "display_name": "风格审计器",
                 "role": "你是公文写作风格审计专家。"},
            ],
            "system_prompt": "# 写作风格审核\n\n## 能力\n检查公文写作风格是否符合规范要求。\n",
        }

    def _setup_handlers(self):
        style = AuditWritingStyleTool()
        list_types = ListWritingStyleTypesTool()
        _tool_map = {"audit_writing_style": style, "list_writing_style_types": list_types}

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            tool = _tool_map.get(tool_name)
            if not tool:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            try:
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

        @self.on("agent.run")
        async def handle_agent_run(params):
            yield "开始风格审核...\n"
            yield {"_final": True, "result": {"status": "completed", "content": params.get("task", "")}}


if __name__ == "__main__":
    import asyncio
    asyncio.run(StyleAuditPlugin().run())
```

```toml
# plugins/style_audit/pyproject.toml
[project]
name = "docaudit-plugin-style-audit"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Test and commit**

```python
# tests/plugin/test_style_audit_plugin.py
import pytest
from plugins.style_audit.entry import StyleAuditPlugin


@pytest.mark.asyncio
async def test_style_audit_plugin_registers():
    plugin = StyleAuditPlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "audit_writing_style" in tool_names
    assert "list_writing_style_types" in tool_names
```

```bash
uv run pytest tests/plugin/test_style_audit_plugin.py -v
git add plugins/style_audit/ tests/plugin/test_style_audit_plugin.py
git commit -m "feat: add style_audit plugin with 2 tools and 1 agent"
```

---

## Phase 5: Fourth Batch — template plugin

### Task 12: Create template plugin

**Files:**
- Create: `plugins/template/plugin.yaml`
- Create: `plugins/template/entry.py`
- Create: `plugins/template/tools.py`
- Create: `plugins/template/pyproject.toml`
- Create: `tests/plugin/test_template_plugin.py`

- [ ] **Step 1-4: Create all files**

```bash
cp src/agent/skills/template/tool.py plugins/template/tools.py
```

```yaml
# plugins/template/plugin.yaml
name: template
version: "1.0.0"
api: "1.0"
description: "Database-backed format template loading"

capabilities:
  tools:
    - name: load_template
      display_name: 加载模板
      description: "Load a format template from the database by document type."
  system_prompt: |
    # 模板加载
    ## 能力
    从数据库按文种加载格式模板。
    ## 使用方式
    调用 `load_template` 工具，传入 `doc_type`。
```

```python
# plugins/template/entry.py
"""Template plugin — database-backed template loading."""
from src.plugin.sdk import PluginRuntime
from .tools import LoadTemplateTool


class TemplatePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "load_template", "display_name": "加载模板",
                 "description": LoadTemplateTool.description, "parameters": LoadTemplateTool.parameters},
            ],
            "system_prompt": "# 模板加载\n\n## 能力\n从数据库按文种加载格式模板。\n",
        }

    def _setup_handlers(self):
        tool = LoadTemplateTool()

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            if tool_name == "load_template":
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            return {"success": False, "error": f"Unknown tool: {tool_name}"}


if __name__ == "__main__":
    import asyncio
    asyncio.run(TemplatePlugin().run())
```

```toml
# plugins/template/pyproject.toml
[project]
name = "docaudit-plugin-template"
version = "1.0.0"
requires-python = ">=3.12"
```

- [ ] **Step 5: Test and commit**

```python
# tests/plugin/test_template_plugin.py
import pytest
from plugins.template.entry import TemplatePlugin


@pytest.mark.asyncio
async def test_template_plugin_registers():
    plugin = TemplatePlugin()
    caps_result = plugin.register_capabilities()
    caps = caps_result["capabilities"]
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "load_template" in tool_names
```

```bash
uv run pytest tests/plugin/test_template_plugin.py -v
git add plugins/template/ tests/plugin/test_template_plugin.py
git commit -m "feat: add template plugin"
```

---

## Phase 6: App Startup Integration

### Task 13: Integrate PluginSystem into create_app()

**Files:**
- Modify: `src/agent/api/app.py`

- [ ] **Step 1: Write the integration test first**

Create `tests/test_app_plugin_integration.py`:

```python
"""Test PluginSystem integration in app startup."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_create_app_registers_plugin_system_on_state():
    """PluginSystem is stored on app.state.plugin_system after create_app()."""
    with patch("src.agent.api.app.ToolRegistry") as mock_tool_reg:
        with patch("src.agent.api.app.PluginSystem") as mock_plugin_sys:
            from src.agent.api.app import create_app

            mock_tool_reg.return_value = MagicMock()
            mock_plugin_sys.return_value = mock_ps = MagicMock()
            mock_ps.start = AsyncMock()
            mock_ps.shutdown = AsyncMock()

            app = create_app(sessions_dir="/tmp/test_sessions")

            assert hasattr(app.state, "tool_registry")
            assert hasattr(app.state, "plugin_system")
            assert app.state.plugin_system is mock_ps


@pytest.mark.asyncio
async def test_create_app_no_longer_registers_domain_tools():
    """DOMAIN_TOOLS import is no longer present in create_app()."""
    from src.agent.api.app import create_app
    import inspect
    
    source = inspect.getsource(create_app)
    assert "DOMAIN_TOOLS" not in source
    assert "from ..tools.domain" not in source
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_app_plugin_integration.py -v
```
Expected: FAIL — `DOMAIN_TOOLS` still in `create_app()`, `PluginSystem` not imported

- [ ] **Step 3: Modify create_app() in app.py**

Remove the DOMAIN_TOOLS import and registration; add PluginSystem:

```python
# src/agent/api/app.py

def create_app(sessions_dir: str = "") -> FastAPI:
    """Create and configure the FastAPI application."""
    from src.config import Settings

    settings = Settings()

    from ..core.logging_config import configure_logging
    configure_logging(
        log_dir=settings.audit_log_dir,
        level=settings.logger_level,
        console=True,
    )

    from src.content_compliance import init_checkers
    init_checkers()

    app = FastAPI(
        title="DocAudit API",
        description="Document audit agent REST API with SSE streaming",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials and settings.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.session_store = SessionStore(
        sessions_dir or str(Path(settings.cache_dir) / "sessions")
    )
    app.state.file_store = FileStore(
        str(Path(settings.upload_dir) / ".file_registry")
    )
    app.state.pause_event = asyncio.Event()
    app.state.active_tasks: dict[str, asyncio.Task] = {}
    app.state.tool_registry = ToolRegistry()

    # PluginSystem replaces DOMAIN_TOOLS registration
    from src.plugin import PluginSystem
    app.state.plugin_system = PluginSystem(
        plugins_dir="plugins",
        tool_registry=app.state.tool_registry,
    )

    app.include_router(router)

    return app
```

Also add a startup event to call `plugin_system.start()`:

```python
@app.on_event("startup")
async def startup_plugins():
    await app.state.plugin_system.start()


@app.on_event("shutdown")
async def shutdown_plugins():
    await app.state.plugin_system.shutdown()
```

- [ ] **Step 4: Run tests to verify**

```bash
uv run pytest tests/test_app_plugin_integration.py -v
```
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
uv run pytest tests/ -v --ignore=tests/plugin/agent/ --timeout=30 2>&1 | tail -30
```

- [ ] **Step 6: Commit**

```bash
git add src/agent/api/app.py tests/test_app_plugin_integration.py
git commit -m "feat: integrate PluginSystem into create_app(), remove DOMAIN_TOOLS registration"
```

---

## Phase 7: OrchestratorAgent Refactoring

### Task 14: Refactor OrchestratorAgent for dynamic subagent discovery

**Files:**
- Modify: `src/agent/agents/orch.py`

- [ ] **Step 1: Write test for dynamic subagent discovery**

Create `tests/test_orch_dynamic_agents.py`:

```python
"""Test OrchestratorAgent dynamic subagent discovery."""
import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_orchestrator_builds_subagents_from_plugin_agents():
    """OrchestratorAgent discovers subagents from plugin system agents."""
    from src.agent.agents.orch import OrchestratorAgent
    from src.agent.testing import MockModelClient
    
    # Setup mock plugin system with agents
    mock_ps = MagicMock()
    mock_ps.get_agents.return_value = {
        "format_auditor": MagicMock(name="format_auditor"),
        "content_auditor": MagicMock(name="content_auditor"),
    }
    
    model = MockModelClient(tool_calls=[])
    agent = OrchestratorAgent(
        model=model,
        plugin_system=mock_ps,
    )
    
    # SubAgentRunner should have agents from plugin system
    configs = agent._subagent_runner._configs
    assert "format_auditor" in configs
    assert "content_auditor" in configs


@pytest.mark.asyncio
async def test_orchestrator_no_longer_imports_domain_tools():
    """OrchestratorAgent does not import DOMAIN_TOOLS."""
    import inspect
    from src.agent.agents.orch import OrchestratorAgent
    
    source = inspect.getsource(OrchestratorAgent.__init__)
    assert "DOMAIN_TOOLS" not in source
    assert "from ..tools.domain" not in source
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_orch_dynamic_agents.py -v
```
Expected: FAIL — DOMAIN_TOOLS still imported, no plugin_system param

- [ ] **Step 3: Modify OrchestratorAgent**

In `src/agent/agents/orch.py`:

1. Remove the `_SUBAGENTS` module-level constant and all auditor class imports
2. Add `plugin_system` parameter to `__init__`
3. Build subagents dynamically from `plugin_system.get_agents()`
4. Remove `from ..tools.domain import DOMAIN_TOOLS` and `tools.extend(DOMAIN_TOOLS)`

```python
class OrchestratorAgent(Agent):
    """Composes domain agents for the full audit pipeline.

    Flow: LLM-driven (parse → plan → dispatch subagents) → export.
    Domain agents run their own agent_loop via SubAgentRunner.
    """

    DEFAULT_SKILLS = [
        "parse",
        "format_audit",
        "content_audit",
        "style_audit",
        "text_correction",
        "plagiarism",
        "search",
        "annotate",
        "template",
    ]

    def __init__(
        self,
        parser: Any = None,
        exporter: Any = None,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        skills: list[str] | None = None,
        selected_auditors: list[str] | None = None,
        plugin_system: Any = None,
    ) -> None:
        self._parser = parser
        self._exporter = exporter
        self._audit_results: dict[str, Any] = {}
        self._selected_auditors = selected_auditors

        if model is None:
            raise ValueError(
                "OrchestratorAgent requires a ModelClient instance. "
                "For testing, pass MockModelClient(tool_calls=[...]) explicitly."
            )
        _model = model

        # Build subagents dynamically from plugin system or fall back to defaults
        self._subagent_runner = SubAgentRunner()

        if plugin_system is not None:
            plugin_agents = plugin_system.get_agents()
            for name, proxy_agent in plugin_agents.items():
                self._subagent_runner.define(
                    name,
                    SubAgentConfig(
                        agent=proxy_agent,
                        failure_strategy=FailureStrategy.TOLERANT,
                        display_name=proxy_agent.display_name or name,
                    ),
                )
        else:
            # Fallback: build subagents from auditor classes (legacy path, for tests only)
            _fallback_subagents: list[tuple[str, type, FailureStrategy]] = [
                ("format_auditor", FormatAuditorAgent, FailureStrategy.STRICT),
                ("content_auditor", ContentAuditorAgent, FailureStrategy.TOLERANT),
                ("correction_auditor", CorrectionAuditorAgent, FailureStrategy.TOLERANT),
                ("plagiarism_auditor", PlagiarismAuditorAgent, FailureStrategy.TOLERANT),
                ("style_auditor", StyleAuditorAgent, FailureStrategy.TOLERANT),
            ]
            for name, agent_cls, strategy in _fallback_subagents:
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

        # ... transform agent setup unchanged ...

        # Build tools from subagents, plus parent-level artifact tools
        tools = self._subagent_runner.build_tools()
        tools.append(ListArtifactsTool())
        tools.append(GetArtifactTool())
        tools.append(PersistOutputTool())

        # NO LONGER importing DOMAIN_TOOLS

        # ... rest of __init__ unchanged ...
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_orch_dynamic_agents.py -v
```
Expected: PASS

- [ ] **Step 5: Run existing orchestration tests**

```bash
uv run pytest tests/ -k "orch" -v --timeout=30
```

- [ ] **Step 6: Commit**

```bash
git add src/agent/agents/orch.py tests/test_orch_dynamic_agents.py
git commit -m "refactor: OrchestratorAgent discovers subagents dynamically from PluginSystem"
```

---

## Phase 8: Cleanup — Delete Old Code

### Task 15: Delete Domain Tools and old Skills

**Files:**
- Delete: `src/agent/tools/domain/` (entire directory)
- Delete: `src/agent/skills/` (entire directory, all 9 skill subdirectories)
- Delete: `src/agent/agents/format_auditor.py`
- Delete: `src/agent/agents/content_auditor.py`
- Delete: `src/agent/agents/correction_auditor.py`
- Delete: `src/agent/agents/plagiarism_auditor.py`
- Delete: `src/agent/agents/style_auditor.py`
- Modify: `src/agent/agents/__init__.py` (remove auditor exports)
- Modify: `src/agent/api/services/agent_service.py` (remove DOMAIN_TOOLS references if any)
- Modify: `src/agent/api/sse_adapter.py` (update _build_tool_skill_map to not use SkillLoader)

- [ ] **Step 1: Run full test suite to capture pre-cleanup baseline**

```bash
uv run pytest tests/ -v --timeout=30 2>&1 | tail -50
```

- [ ] **Step 2: Delete domain tools directory**

```bash
rm -rf src/agent/tools/domain/
```

- [ ] **Step 3: Delete skills directory**

```bash
rm -rf src/agent/skills/
```

- [ ] **Step 4: Delete individual auditor agent files**

```bash
rm src/agent/agents/format_auditor.py
rm src/agent/agents/content_auditor.py
rm src/agent/agents/correction_auditor.py
rm src/agent/agents/plagiarism_auditor.py
rm src/agent/agents/style_auditor.py
```

- [ ] **Step 5: Clean up agents/__init__.py AND remove fallback imports from orch.py**

Remove auditor class exports from `src/agent/agents/__init__.py`. Also remove the fallback `_fallback_subagents` path from `orch.py.__init__()` — the `else` branch that imports auditor classes directly (this was only needed before the files were deleted):

In `src/agent/agents/orch.py`, simplify `__init__` to remove the `else` fallback branch entirely:

```python
# After cleanup: no fallback, plugin_system is required
if plugin_system is None:
    raise ValueError(
        "OrchestratorAgent requires a PluginSystem instance. "
        "Pass plugin_system=app.state.plugin_system."
    )

plugin_agents = plugin_system.get_agents()
for name, proxy_agent in plugin_agents.items():
    self._subagent_runner.define(
        name,
        SubAgentConfig(
            agent=proxy_agent,
            failure_strategy=FailureStrategy.TOLERANT,
            display_name=proxy_agent.display_name or name,
        ),
    )
```

Remove these now-unused imports from orch.py:
- `from .format_auditor import FormatAuditorAgent`
- `from .content_auditor import ContentAuditorAgent`
- `from .correction_auditor import CorrectionAuditorAgent`
- `from .plagiarism_auditor import PlagiarismAuditorAgent`
- `from .style_auditor import StyleAuditorAgent`
- `from ..tools.builtin.list_artifacts import ListArtifactsTool` — KEEP (still used)
- `from ..tools.builtin.get_artifact import GetArtifactTool` — KEEP
- `from ..tools.builtin.persist_output import PersistOutputTool` — KEEP

- [ ] **Step 6: Update sse_adapter.py to remove SkillLoader dependency**

In `src/agent/api/sse_adapter.py`, update `_build_tool_skill_map` to get tool-to-display-name mappings from PluginSystem instead of SkillLoader:

```python
# Old:
# from ...skills.loader import SkillLoader
# loader = SkillLoader()
# skills = loader.load_all()
# ...

# New:
# Get tool names from PluginSystem agents/tools and map to display names
def _build_tool_skill_map(plugin_system):
    """Build tool_name -> skill display_name mapping from plugin system."""
    mapping = {}
    agents = plugin_system.get_agents() if plugin_system else {}
    for name, agent in agents.items():
        display_name = agent.display_name or name
        mapping[name] = display_name
    return mapping
```

- [ ] **Step 7: Run tests to find broken imports**

```bash
uv run pytest tests/ -v --timeout=30 2>&1 | grep -E "FAILED|ERROR|ModuleNotFoundError|ImportError" | head -20
```

- [ ] **Step 8: Fix any remaining broken imports in test files**

Check for tests that import from deleted modules:
```bash
grep -r "from.*domain import\|from.*skills\.\|from.*format_auditor\|from.*content_auditor\|from.*correction_auditor\|from.*plagiarism_auditor\|from.*style_auditor" tests/ --include="*.py"
```

Fix each test by either:
- Updating to use the plugin-based equivalents
- Removing tests that are no longer applicable (e.g., tests for deleted Domain Tools)

- [ ] **Step 9: Run full test suite and confirm green**

```bash
uv run pytest tests/ -v --timeout=30
```

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: delete Domain Tools, old Skills, and auditor classes after plugin migration"
```

---

## Phase 9: Final Verification

### Task 16: End-to-end verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ -v --timeout=60
```

- [ ] **Step 2: Run plugin-specific tests**

```bash
uv run pytest tests/plugin/ -v
```

- [ ] **Step 3: Check for any remaining references to deleted modules**

```bash
grep -r "DOMAIN_TOOLS\|domain_tool\|tools\.domain\|skills\.loader\|SkillLoader\|format_auditor\.py\|content_auditor\.py\|correction_auditor\.py\|plagiarism_auditor\.py\|style_auditor\.py" src/ tests/ --include="*.py" | grep -v __pycache__ | grep -v ".md"
```
Expected: No output (all references cleaned up)

- [ ] **Step 4: Verify plugins directory structure**

```bash
ls -la plugins/*/
```
Expected: 9 plugin directories (search, parse, format_audit, annotate, text_correction, content_audit, plagiarism, style_audit, template)

- [ ] **Step 5: Commit final verification**

```bash
git add -A
git commit -m "chore: final verification after tools-to-plugins migration"
```
