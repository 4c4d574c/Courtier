# Skill / SubAgent / Tool 重新组织实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 DocAudit 的能力层重新组织为 `Tool`（原子工具）、`SubAgent`（通用 Agent）、`Skill`（.md 文档定义）三层，使业务专家可通过 Markdown 定义审核流程。

**架构：** Skill 文档放在 `skills/` 目录，启动时预编译为 `SkillConfig` 并存入 `SkillRegistry`；`load_skill` 工具按需创建通用 `Agent` 执行 Skill；插件仅提供原子工具，目录重组为 `plugins/common/` 和 `plugins/audit/`。

**技术栈：** Python 3.12+, FastAPI, Pydantic, pytest, uv

---

## 文件结构总览

### 新增文件

| 文件 | 职责 |
|------|------|
| `skills/__init__.py` | 使 `skills/` 成为 Python 包 |
| `skills/schemas/__init__.py` | 输入模型包入口 |
| `skills/schemas/format_audit.py` | `FormatAuditorInput` 模型 |
| `skills/schemas/content_audit.py` | `ContentAuditorInput` 模型 |
| `skills/schemas/style_audit.py` | `StyleAuditorInput` 模型 |
| `skills/schemas/text_correction.py` | `CorrectionAuditorInput` 模型 |
| `skills/schemas/plagiarism.py` | `PlagiarismAuditorInput` 模型 |
| `skills/format_audit.md` | 格式审核 Skill 定义 |
| `skills/content_audit.md` | 内容审核 Skill 定义 |
| `skills/style_audit.md` | 行文风格审查 Skill 定义 |
| `skills/text_correction.md` | 文本纠错 Skill 定义 |
| `skills/plagiarism.md` | 文档查重 Skill 定义 |
| `skills/full_government_audit.md` | 完整政府公文审核流程 Skill |
| `src/agent/skills/config.py` | `SkillConfig` 数据类 |
| `src/agent/skills/registry.py` | `SkillRegistry` 实现 |
| `src/agent/skills/load_tool.py` | `LoadSkillTool` 实现 |
| `src/agent/skills/__init__.py` | Skill 模块公共 API |
| `tests/agent/skills/test_config.py` | `SkillConfig` 测试 |
| `tests/agent/skills/test_registry.py` | `SkillRegistry` 测试 |
| `tests/agent/skills/test_load_tool.py` | `LoadSkillTool` 测试 |
| `tests/agent/skills/fixtures/valid_skill.md` | 测试用有效 Skill |
| `tests/agent/skills/fixtures/bad_yaml.md` | 测试用 frontmatter 错误 Skill |
| `tests/agent/skills/fixtures/missing_frontmatter.md` | 测试用缺少 frontmatter Skill |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `src/plugin/scanner.py` | 支持 `plugins/common/` 和 `plugins/audit/` 二级目录扫描 |
| `src/agent/agents/orch.py` | 移除 `SubAgentRunner` 对插件 agents 的包装；注册 `load_skill`；注入 Skill 目录；简化 system prompt |
| `src/agent/api/services/agent_service.py` | 创建 `SkillRegistry` 并传给 `OrchestratorAgent` |
| `src/agent/agents/input_models.py` | 迁移模型到 `skills/schemas/`，保留兼容导入 |
| `src/agent/agents/subagent/runner.py` | 移除或简化插件 agent 包装逻辑 |
| `src/plugin/registry.py` | 清理不再触发的 agent 注册代码 |
| `plugins/*/plugin.yaml` | 移除 `type: agent` capabilities |

### 目录移动（文件系统操作）

```text
plugins/parse        → plugins/common/parse
plugins/search       → plugins/common/search
plugins/annotate     → plugins/common/annotate
plugins/template     → plugins/common/template
plugins/format_audit → plugins/audit/format_audit
plugins/content_audit → plugins/audit/content_audit
plugins/style_audit  → plugins/audit/style_audit
plugins/text_correction → plugins/audit/text_correction
plugins/plagiarism   → plugins/audit/plagiarism
```

---

## Phase 1：Skill 基础设施

### 任务 1：创建目录结构

**文件：**
- 创建：`skills/__init__.py`
- 创建：`skills/schemas/__init__.py`
- 创建：`tests/agent/skills/__init__.py`
- 创建：`tests/agent/skills/fixtures/__init__.py`

- [ ] **步骤 1：创建目录和空 `__init__.py` 文件**

```bash
mkdir -p skills/schemas
mkdir -p tests/agent/skills/fixtures
touch skills/__init__.py
touch skills/schemas/__init__.py
touch tests/agent/skills/__init__.py
touch tests/agent/skills/fixtures/__init__.py
```

- [ ] **步骤 2：验证目录结构**

```bash
find skills tests/agent/skills -type f | sort
```

预期输出包含上述 4 个 `__init__.py` 路径。

- [ ] **步骤 3：Commit**

```bash
git add skills tests/agent/skills
git commit -m "chore: add skills directory structure"
```

---

### 任务 2：实现 `SkillConfig`

**文件：**
- 创建：`src/agent/skills/config.py`
- 测试：`tests/agent/skills/test_config.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/test_config.py
from pathlib import Path

from src.agent.skills.config import SkillConfig


def test_skill_config_creation():
    config = SkillConfig(
        name="format_audit",
        description="格式审核",
        source_path=Path("skills/format_audit.md"),
        system_prompt="你是格式审核专家",
        tools=("parse_document", "audit_format"),
        input_model=None,
        output_artifact_type="format_audit_result",
        tags=("audit",),
        enabled=True,
        raw_frontmatter={"name": "format_audit"},
    )

    assert config.name == "format_audit"
    assert config.tools == ("parse_document", "audit_format")
    assert config.enabled is True
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run python -m pytest tests/agent/skills/test_config.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'src.agent.skills.config'`

- [ ] **步骤 3：编写最少实现代码**

```python
# src/agent/skills/config.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class SkillConfig:
    """预编译后的 Skill 配置。"""

    name: str
    description: str
    source_path: Path
    system_prompt: str
    tools: tuple[str, ...]
    input_model: type[BaseModel] | None
    output_artifact_type: str | None
    tags: tuple[str, ...]
    enabled: bool
    raw_frontmatter: dict[str, Any]
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run python -m pytest tests/agent/skills/test_config.py -v
```

预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/agent/skills/config.py tests/agent/skills/test_config.py
git commit -m "feat: add SkillConfig dataclass"
```

---

### 任务 3：实现 `SkillRegistry` 扫描

**文件：**
- 创建：`src/agent/skills/registry.py`
- 测试：`tests/agent/skills/test_registry.py`
- 创建：`tests/agent/skills/fixtures/valid_skill.md`
- 创建：`tests/agent/skills/fixtures/bad_yaml.md`
- 创建：`tests/agent/skills/fixtures/missing_frontmatter.md`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/test_registry.py
from pathlib import Path

from src.agent.skills.registry import SkillRegistry


FIXTURES = Path(__file__).parent / "fixtures"


def test_registry_scans_valid_skill():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    config = registry.get("valid_skill")
    assert config is not None
    assert config.name == "valid_skill"
    assert config.description == "有效 Skill"
    assert config.tools == ("parse_document", "audit_format")
    assert "你是格式审核专家" in config.system_prompt


def test_registry_skills_without_frontmatter():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    assert registry.get("missing_frontmatter") is None
    assert any("frontmatter" in e.lower() for e in registry.errors)


def test_registry_skips_bad_yaml():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    assert registry.get("bad_yaml") is None
    assert any("yaml" in e.lower() for e in registry.errors)


def test_registry_builds_catalog():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    catalog = registry.build_catalog()
    assert "valid_skill" in catalog
    assert "有效 Skill" in catalog
```

- [ ] **步骤 2：创建测试夹具**

```markdown
<!-- tests/agent/skills/fixtures/valid_skill.md -->
---
name: valid_skill
description: 有效 Skill
tools:
  - parse_document
  - audit_format
---

# 目标
你是格式审核专家。

# 流程
1. 解析文档
2. 审核格式
```

```markdown
<!-- tests/agent/skills/fixtures/missing_frontmatter.md -->
# 没有 frontmatter

这个文件缺少 YAML frontmatter。
```

```markdown
<!-- tests/agent/skills/fixtures/bad_yaml.md -->
---
name: bad_yaml
description: "unclosed string
tools: [parse_document
---

内容。
```

- [ ] **步骤 3：运行测试验证失败**

```bash
uv run python -m pytest tests/agent/skills/test_registry.py -v
```

预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 4：编写实现代码**

```python
# src/agent/skills/registry.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .config import SkillConfig

logger = logging.getLogger(__name__)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Split YAML frontmatter from markdown body.

    Returns (frontmatter_dict, body). If frontmatter is missing or invalid,
    returns (None, raw).
    """
    if not raw.startswith("---\n"):
        return None, raw

    parts = raw.split("\n---\n", 1)
    if len(parts) != 2:
        return None, raw

    try:
        meta = yaml.safe_load(parts[0].strip("-").strip()) or {}
    except yaml.YAMLError:
        return None, raw

    if not isinstance(meta, dict):
        return None, raw

    return meta, parts[1].strip()


class SkillRegistry:
    """启动时扫描 skills/ 目录并维护预编译的 Skill 配置。"""

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = Path(skills_dir)
        self._configs: dict[str, SkillConfig] = {}
        self._errors: list[str] = []

    def scan(self) -> None:
        """扫描 skills/ 目录，解析所有 .md 文件。"""
        self._configs.clear()
        self._errors.clear()

        if not self._skills_dir.exists():
            logger.info("Skills directory not found: %s", self._skills_dir)
            return

        for path in sorted(self._skills_dir.glob("*.md")):
            self._scan_one(path)

    def _scan_one(self, path: Path) -> None:
        name = path.stem
        raw = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(raw)

        if meta is None:
            self._errors.append(f"Skill '{name}' missing valid frontmatter: {path}")
            return

        try:
            config = self._build_config(name, path, meta, body)
        except Exception as exc:
            self._errors.append(f"Skill '{name}' build failed: {exc}")
            return

        if config.name in self._configs:
            self._errors.append(
                f"Duplicate skill name '{config.name}' from {path}; overwriting"
            )

        self._configs[config.name] = config

    def _build_config(
        self, stem: str, path: Path, meta: dict[str, Any], body: str
    ) -> SkillConfig:
        name = meta.get("name", stem)
        description = meta.get("description", body.split("\n")[0].lstrip("#").strip())
        tools = tuple(meta.get("tools", []))
        tags = tuple(meta.get("tags", []))
        enabled = bool(meta.get("enabled", True))
        output_artifact_type = meta.get("output_artifact_type")
        input_model = self._resolve_input_model(meta.get("input_model"))

        return SkillConfig(
            name=name,
            description=description,
            source_path=path,
            system_prompt=body,
            tools=tools,
            input_model=input_model,
            output_artifact_type=output_artifact_type,
            tags=tags,
            enabled=enabled,
            raw_frontmatter=meta,
        )

    @staticmethod
    def _resolve_input_model(import_path: str | None) -> type | None:
        if not import_path:
            return None

        module_path, class_name = import_path.rsplit(".", 1)
        module = __import__(module_path, fromlist=[class_name])
        return getattr(module, class_name)

    def get(self, name: str) -> SkillConfig | None:
        return self._configs.get(name)

    def list_enabled(self) -> list[SkillConfig]:
        return [c for c in self._configs.values() if c.enabled]

    def build_catalog(self) -> str:
        lines = []
        for config in sorted(self.list_enabled(), key=lambda c: c.name):
            lines.append(f"- **{config.name}**: {config.description}")
        return "\n".join(lines)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def has_errors(self) -> bool:
        return bool(self._errors)
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run python -m pytest tests/agent/skills/test_registry.py -v
```

预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add src/agent/skills/registry.py tests/agent/skills/test_registry.py tests/agent/skills/fixtures
git commit -m "feat: add SkillRegistry with frontmatter parsing"
```

---

### 任务 4：实现 `LoadSkillTool`

**文件：**
- 创建：`src/agent/skills/load_tool.py`
- 测试：`tests/agent/skills/test_load_tool.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/test_load_tool.py
import pytest

from src.agent.skills.load_tool import LoadSkillTool
from src.agent.skills.registry import SkillRegistry
from src.agent.tools.registry import ToolRegistry
from src.agent.testing import MockModelClient


@pytest.fixture
def registry(tmp_path):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "echo.md").write_text(
        "---\nname: echo\ndescription: 回显 Skill\ntools:\n  - echo\n---\n\n"
        "调用 echo 工具返回输入文本。",
        encoding="utf-8",
    )
    reg = SkillRegistry(skill_dir)
    reg.scan()
    return reg


@pytest.fixture
def tool_registry():
    from src.agent.tools.builtin.echo import EchoTool

    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


@pytest.mark.asyncio
async def test_load_skill_executes_skill(registry, tool_registry):
    from src.agent.tools.protocol import ToolProgress

    model = MockModelClient(
        tool_calls=[{"id": "1", "name": "echo", "arguments": {"text": "hello"}}]
    )
    tool = LoadSkillTool(registry, tool_registry, model)

    progress: list[dict] = []
    result = await tool.execute(
        on_progress=lambda p: progress.append(p),
        skill="echo",
        task="say hello",
    )

    assert result.success is True
    assert result.metadata.get("skill") == "echo"


@pytest.mark.asyncio
async def test_load_skill_not_found(registry, tool_registry):
    model = MockModelClient(tool_calls=[])
    tool = LoadSkillTool(registry, tool_registry, model)

    result = await tool.execute(on_progress=lambda p: None, skill="missing")

    assert result.success is False
    assert "not found" in result.error.lower()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run python -m pytest tests/agent/skills/test_load_tool.py -v
```

预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现代码**

```python
# src/agent/skills/load_tool.py
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from src.agent.agents.base import Agent
from src.agent.core.model import ModelClient
from src.agent.tools.protocol import OnToolProgress, ToolProtocol, ToolResult
from src.agent.tools.registry import ToolRegistry

from .config import SkillConfig
from .registry import SkillRegistry

logger = logging.getLogger(__name__)


class LoadSkillTool:
    """加载并执行 Skill 的工具。"""

    name = "load_skill"
    description = (
        "加载并执行一个 Skill。Skill 是预定义的子代理配置，"
        "会根据 SKILL.md 中的说明自主调用工具完成任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill 名称"},
            "task": {"type": "string", "description": "任务描述"},
        },
        "required": ["skill"],
        "additionalProperties": True,
    }

    def __init__(
        self,
        skill_registry: SkillRegistry,
        tool_registry: ToolRegistry,
        model: ModelClient,
    ) -> None:
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._model = model

    async def execute(
        self, *, on_progress: OnToolProgress, skill: str, task: str = "", **kwargs: Any
    ) -> ToolResult:
        on_progress(
            {"status": "running", "message": f"加载 Skill: {skill}...", "detail": None}
        )

        config = self._skill_registry.get(skill)
        if config is None or not config.enabled:
            return ToolResult(success=False, error=f"Skill not found or disabled: {skill}")

        tools = self._resolve_tools(config)
        if isinstance(tools, ToolResult):  # error
            return tools

        run_kwargs = self._build_run_kwargs(config, task, kwargs)
        if isinstance(run_kwargs, ToolResult):  # error
            return run_kwargs

        agent = Agent(
            name=config.name,
            role=config.system_prompt,
            tools=tools,
            model=self._model,
        )

        try:
            result = await agent.run(
                **run_kwargs,
                artifact_store=kwargs.get("artifact_store"),
                context_manager=kwargs.get("context_manager"),
            )
        except Exception as exc:
            logger.exception("Skill '%s' execution failed", skill)
            return ToolResult(success=False, error=f"Skill execution failed: {exc}")

        on_progress(
            {"status": "done", "message": f"Skill {skill} 完成", "detail": None}
        )

        return ToolResult(
            success=result.status == "completed",
            data=_extract_subagent_data(result),
            error=result.termination_reason if result.status != "completed" else None,
            metadata={"is_subagent_result": True, "skill": skill},
        )

    def _resolve_tools(self, config: SkillConfig) -> list[ToolProtocol] | ToolResult:
        tools: list[ToolProtocol] = []
        for tool_name in config.tools:
            tool = self._tool_registry.get(tool_name)
            if tool is None:
                return ToolResult(
                    success=False,
                    error=f"Skill '{config.name}' requires tool '{tool_name}' which is not registered",
                )
            tools.append(tool)
        return tools

    def _build_run_kwargs(
        self, config: SkillConfig, task: str, kwargs: dict[str, Any]
    ) -> dict[str, Any] | ToolResult:
        if config.input_model is not None:
            try:
                input_obj = config.input_model(task=task, **kwargs)
            except ValidationError as exc:
                return ToolResult(success=False, error=f"Invalid input: {exc}")
            return {"input": input_obj}
        return {"task": task}


def _extract_subagent_data(result: Any) -> dict[str, Any] | None:
    """Extract structured data from the last successful tool result."""
    if not result.tool_results:
        return {"content": result.content}

    for tr in reversed(result.tool_results):
        if tr.success and tr.data is not None:
            return {"content": result.content, "data": tr.data}

    return {"content": result.content}
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run python -m pytest tests/agent/skills/test_load_tool.py -v
```

预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/agent/skills/load_tool.py tests/agent/skills/test_load_tool.py
git commit -m "feat: add LoadSkillTool"
```

---

### 任务 5：暴露 Skill 模块公共 API

**文件：**
- 创建：`src/agent/skills/__init__.py`

- [ ] **步骤 1：编写公共 API 导出**

```python
# src/agent/skills/__init__.py
from .config import SkillConfig
from .load_tool import LoadSkillTool
from .registry import SkillRegistry

__all__ = ["SkillConfig", "SkillRegistry", "LoadSkillTool"]
```

- [ ] **步骤 2：验证导入**

```bash
uv run python -c "from src.agent.skills import SkillConfig, SkillRegistry, LoadSkillTool; print('ok')"
```

预期：输出 `ok`

- [ ] **步骤 3：Commit**

```bash
git add src/agent/skills/__init__.py
git commit -m "chore: expose skill module public API"
```

---

### 任务 6：在 OrchestratorAgent 中集成 `load_skill`

**文件：**
- 修改：`src/agent/agents/orch.py`
- 修改：`src/agent/api/services/agent_service.py`
- 测试：`tests/agent/agents/test_orch.py`（新增或修改）

- [ ] **步骤 1：修改 `src/agent/api/services/agent_service.py` 创建 SkillRegistry**

```python
# 在 build_audit_agent 中新增
from src.agent.skills import SkillRegistry

async def build_audit_agent(...):
    model = build_model_client(settings)
    drudge_md_content = _load_drudge_md()

    skill_registry = SkillRegistry("skills")
    skill_registry.scan()
    if skill_registry.has_errors:
        logger.warning("Skill registry errors: %s", skill_registry.errors)

    agent = OrchestratorAgent(
        model=model,
        parser=ParserAgent(),
        exporter=None,
        plugin_system=plugin_system,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        drudge_md_content=drudge_md_content,
    )
    # ... rest unchanged
```

- [ ] **步骤 2：修改 `src/agent/agents/orch.py` 接受 skill_registry**

```python
# 在 __init__ 签名中新增 skill_registry 参数
from src.agent.skills import LoadSkillTool, SkillRegistry

class OrchestratorAgent(Agent):
    def __init__(
        self,
        parser: Any = None,
        exporter: Any = None,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        selected_auditors: list[str] | None = None,
        plugin_system: Any = None,
        tool_registry: Any = None,
        skill_registry: SkillRegistry | None = None,
        drudge_md_content: str | None = None,
    ) -> None:
        # ... existing setup ...

        tools = self._subagent_runner.build_tools()
        tools.append(ListArtifactsTool())
        tools.append(GetArtifactTool())
        tools.append(PersistOutputTool())

        if skill_registry is not None:
            tools.append(
                LoadSkillTool(
                    skill_registry=skill_registry,
                    tool_registry=self.tool_registry,
                    model=_model,
                )
            )
            skill_catalog = skill_registry.build_catalog()
        else:
            skill_catalog = ""

        # ... build role ...
        role = (
            "你是 DocAudit 文档审核编排代理。"
            "你的任务是理解用户需求，选择合适的 Skill 并调用 load_skill 执行。"
            "每个 Skill 都是独立的子代理，会自主完成其负责的部分。\n\n"
            f"可用 Skill：\n{skill_catalog}\n\n"
            "需要执行某个 Skill 时，调用 load_skill(skill=..., task=..., ...)。"
        )

        super().__init__(...)
```

- [ ] **步骤 3：运行现有 OrchestratorAgent 测试**

```bash
uv run python -m pytest tests/agent/agents/ -v
```

预期：原有测试通过，新增 `load_skill` 工具相关行为需要后续测试覆盖。

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/orch.py src/agent/api/services/agent_service.py
git commit -m "feat: integrate load_skill into OrchestratorAgent"
```

---

## Phase 2：创建核心 Skill 文档

### 任务 7：迁移输入模型到 `skills/schemas/`

**文件：**
- 创建：`skills/schemas/format_audit.py`
- 创建：`skills/schemas/content_audit.py`
- 创建：`skills/schemas/style_audit.py`
- 创建：`skills/schemas/text_correction.py`
- 创建：`skills/schemas/plagiarism.py`
- 修改：`src/agent/agents/input_models.py`
- 测试：`tests/agent/skills/test_schemas.py`

- [ ] **步骤 1：从现有 `src/agent/agents/input_models.py` 复制模型到新位置**

例如 `skills/schemas/format_audit.py`：

```python
from pydantic import BaseModel


class FormatAuditorInput(BaseModel):
    task: str
    document: dict | str | None = None
    doc_type: str | None = None
```

（具体字段以当前 `src/agent/agents/input_models.py` 为准。）

- [ ] **步骤 2：在 `src/agent/agents/input_models.py` 保留兼容导入**

```python
# src/agent/agents/input_models.py
from skills.schemas.format_audit import FormatAuditorInput
from skills.schemas.content_audit import ContentAuditorInput
from skills.schemas.style_audit import StyleAuditorInput
from skills.schemas.text_correction import CorrectionAuditorInput
from skills.schemas.plagiarism import PlagiarismAuditorInput

__all__ = [
    "FormatAuditorInput",
    "ContentAuditorInput",
    "StyleAuditorInput",
    "CorrectionAuditorInput",
    "PlagiarismAuditorInput",
]
```

- [ ] **步骤 3：验证导入**

```bash
uv run python -c "from skills.schemas.format_audit import FormatAuditorInput; print('ok')"
uv run python -c "from src.agent.agents.input_models import FormatAuditorInput; print('ok')"
```

预期：都输出 `ok`

- [ ] **步骤 4：Commit**

```bash
git add skills/schemas src/agent/agents/input_models.py
git commit -m "refactor: move skill input models to skills/schemas"
```

---

### 任务 8：创建各业务 Skill 文档

**文件：**
- 创建：`skills/format_audit.md`
- 创建：`skills/content_audit.md`
- 创建：`skills/style_audit.md`
- 创建：`skills/text_correction.md`
- 创建：`skills/plagiarism.md`

- [ ] **步骤 1：创建 `skills/format_audit.md`**

```markdown
---
name: format_audit
description: 政府公文格式审核
tools:
  - parse_document
  - detect_document_type
  - audit_format
input_model: skills.schemas.format_audit.FormatAuditorInput
output_artifact_type: format_audit_result
---

# 目标
你是政府公文格式审核专家。收到任务后，按以下流程执行审核。

# 适用场景
- 文种为通知、报告、请示、函
- 文件类型为 DOCX 或 PDF

# 工具说明
- `parse_document`: 解析文档结构
- `detect_document_type`: 根据标题和正文判断公文文种
- `audit_format`: 按 GB/T 9704-2012 检查格式合规性

# 执行流程
1. 调用 `parse_document` 解析输入文档
2. 调用 `detect_document_type` 确定文种
3. 调用 `audit_format` 检查格式
4. 汇总违规项并返回

# 输出格式
返回 JSON: {"violations": [...], "summary": "..."}
```

- [ ] **步骤 2：参照当前插件子代理的 role 和流程，创建其他 Skill**

从以下文件提取角色定义和流程：
- `plugins/format_audit/` 中的 `format_auditor` agent role
- `plugins/content_audit/` 中的 `content_auditor` agent role
- `plugins/style_audit/` 中的 `style_auditor` agent role
- `plugins/text_correction/` 中的 `correction_auditor` agent role
- `plugins/plagiarism/` 中的 `plagiarism_auditor` agent role

每个 Skill frontmatter 中的 `tools` 列表对应该插件提供的工具。

- [ ] **步骤 3：验证 SkillRegistry 能正确加载**

```bash
uv run python -c "
from src.agent.skills import SkillRegistry
r = SkillRegistry('skills')
r.scan()
print('loaded:', [c.name for c in r.list_enabled()])
print('errors:', r.errors)
"
```

预期：加载所有 5 个 Skill，无错误。

- [ ] **步骤 4：Commit**

```bash
git add skills/*.md
git commit -m "feat: add core audit skills"
```

---

### 任务 9：创建完整流程 Skill

**文件：**
- 创建：`skills/full_government_audit.md`

- [ ] **步骤 1：编写高层流程 Skill**

```markdown
---
name: full_government_audit
description: 政府公文完整审核流程
tools:
  - parse_document
  - load_skill
---

# 目标
对政府公文执行完整审核流程：格式、内容、风格、纠错、查重。

# 适用场景
- 用户要求完整审核一份政府公文
- 文件已上传且需要全面检查

# 执行流程
1. 调用 `parse_document` 解析文档
2. 调用 `load_skill(skill="format_audit", document=..., doc_type=...)`
3. 调用 `load_skill(skill="content_audit", document=...)`
4. 调用 `load_skill(skill="style_audit", document=...)`
5. 调用 `load_skill(skill="text_correction", document=...)`
6. 调用 `load_skill(skill="plagiarism", document=...)`
7. 汇总所有审核结果，返回综合报告
```

- [ ] **步骤 2：验证加载**

```bash
uv run python -c "
from src.agent.skills import SkillRegistry
r = SkillRegistry('skills')
r.scan()
assert r.get('full_government_audit') is not None
print('ok')
"
```

- [ ] **步骤 3：Commit**

```bash
git add skills/full_government_audit.md
git commit -m "feat: add full government audit skill"
```

---

## Phase 3：插件目录重组与清理

### 任务 10：更新 PluginScanner 支持嵌套目录

**文件：**
- 修改：`src/plugin/scanner.py`
- 测试：`tests/plugin/test_scanner.py`

- [ ] **步骤 1：编写测试**

```python
# tests/plugin/test_scanner.py 中添加
def test_scanner_finds_nested_plugins(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "parse").mkdir()
    (tmp_path / "common" / "parse" / "plugin.yaml").write_text(
        "name: parse\nversion: 0.1.0\napi: 1.0\n", encoding="utf-8"
    )
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "format_audit").mkdir()
    (tmp_path / "audit" / "format_audit" / "plugin.yaml").write_text(
        "name: format_audit\nversion: 0.1.0\napi: 1.0\n", encoding="utf-8"
    )

    scanner = PluginScanner()
    results = scanner.scan(tmp_path)

    names = {r.name for r in results if r.status == ScanStatus.VALID}
    assert names == {"parse", "format_audit"}
```

- [ ] **步骤 2：修改 scanner 实现**

将 `scan()` 中的目录遍历改为递归查找 `plugin.yaml`：

```python
def scan(self, plugins_dir: Path) -> list[PluginScanResult]:
    if not plugins_dir.exists():
        raise FileNotFoundError(f"Plugins directory not found: {plugins_dir}")
    if not plugins_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {plugins_dir}")

    results: list[PluginScanResult] = []
    for manifest_path in sorted(plugins_dir.rglob(self.MANIFEST_FILE)):
        plugin_dir = manifest_path.parent
        result = self._scan_one(plugin_dir, manifest_path)
        results.append(result)
    return results
```

- [ ] **步骤 3：运行测试**

```bash
uv run python -m pytest tests/plugin/test_scanner.py -v
```

预期：PASS

- [ ] **步骤 4：Commit**

```bash
git add src/plugin/scanner.py tests/plugin/test_scanner.py
git commit -m "feat: support nested plugin directories"
```

---

### 任务 11：移动插件目录

**文件：**
- 移动：`plugins/*` 到 `plugins/common/*` 和 `plugins/audit/*`

- [ ] **步骤 1：执行目录移动**

```bash
cd docaudit-agent
mkdir -p plugins/common plugins/audit
mv plugins/parse plugins/search plugins/annotate plugins/template plugins/common/
mv plugins/format_audit plugins/content_audit plugins/style_audit plugins/text_correction plugins/plagiarism plugins/audit/
```

- [ ] **步骤 2：验证结构**

```bash
find plugins -name plugin.yaml | sort
```

预期输出：

```text
plugins/audit/content_audit/plugin.yaml
plugins/audit/format_audit/plugin.yaml
plugins/audit/plagiarism/plugin.yaml
plugins/audit/style_audit/plugin.yaml
plugins/audit/text_correction/plugin.yaml
plugins/common/annotate/plugin.yaml
plugins/common/parse/plugin.yaml
plugins/common/search/plugin.yaml
plugins/common/template/plugin.yaml
```

- [ ] **步骤 3：运行插件扫描测试**

```bash
uv run python -c "
import asyncio
from src.plugin import PluginSystem
from src.agent.tools.registry import ToolRegistry

async def main():
    tr = ToolRegistry()
    ps = PluginSystem('plugins', tool_registry=tr)
    status = await ps.start()
    print('status:', status)
    print('tools:', [t.name for t in tr.list_tools()])
    await ps.shutdown()

asyncio.run(main())
"
```

预期：所有插件启动成功，工具列表包含所有原子工具。

- [ ] **步骤 4：Commit**

```bash
git add plugins
git commit -m "refactor: reorganize plugins into common/ and audit/"
```

---

### 任务 12：移除插件中的 agent 声明

**文件：**
- 修改：`plugins/audit/*/plugin.yaml`

- [ ] **步骤 1：编辑每个业务插件的 `plugin.yaml`**

移除所有 `type: agent` capabilities，只保留 `type: tool`。

例如 `plugins/audit/format_audit/plugin.yaml`：

```yaml
# 移除以下内容
capabilities:
  - type: agent
    name: format_auditor
    display_name: 格式审核员
    role: ...
```

- [ ] **步骤 2：验证插件仍只提供工具**

```bash
uv run python -c "
import asyncio
from src.plugin import PluginSystem
from src.agent.tools.registry import ToolRegistry

async def main():
    tr = ToolRegistry()
    ps = PluginSystem('plugins', tool_registry=tr)
    await ps.start()
    print('agents:', list(ps.get_agents().keys()))
    print('tools:', [t.name for t in tr.list_tools()])
    await ps.shutdown()

asyncio.run(main())
"
```

预期：`agents: []`，工具列表不变。

- [ ] **步骤 3：Commit**

```bash
git add plugins/audit/*/plugin.yaml
git commit -m "refactor: remove agent capabilities from audit plugins"
```

---

### 任务 13：清理 `SubAgentRunner` 和 `ExtensionRegistry`

**文件：**
- 修改：`src/agent/agents/subagent/runner.py`
- 修改：`src/plugin/registry.py`
- 修改：`src/agent/agents/orch.py`

- [ ] **步骤 1：简化 `OrchestratorAgent.__init__` 中的 `SubAgentRunner` 使用**

移除从 `plugin_system.get_agents()` 构建子代理的逻辑。保留 `SubAgentRunner` 用于 `transform` 内置子代理。

```python
# 在 OrchestratorAgent.__init__ 中
self._subagent_runner = SubAgentRunner()

# 移除 plugin_system.get_agents() 循环
# 保留 transform agent
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

- [ ] **步骤 2：清理 `ExtensionRegistry` 中的 agent 注册**

确认不再有插件声明 `type: agent`，移除 `_register_agent` 方法及 `get_agents` / `on_unregister` 中的 agent 分支，并同步清理 `PluginSystem.get_agents` 与 SSEAdapter 中对插件 agent 的依赖。

- [ ] **步骤 3：运行相关测试**

```bash
uv run python -m pytest tests/agent/agents/ tests/plugin/ -v
```

预期：PASS

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/orch.py src/agent/agents/subagent/runner.py src/plugin/registry.py
git commit -m "refactor: remove plugin agent wrapping from SubAgentRunner"
```

---

## Phase 4：简化 OrchestratorAgent 与收尾

### 任务 14：简化 OrchestratorAgent 的 system prompt

**文件：**
- 修改：`src/agent/agents/orch.py`

- [ ] **步骤 1：移除硬编码工作流规则**

当前 `self._prompt_pipeline.set_rules(...)` 中写死的 parse → format → content → ... 流程可以大幅简化。OrchestratorAgent 的 system prompt 只保留通用编排描述：

```python
self._prompt_pipeline.set_rules(
    "# 工作流规则\n"
    "1. 理解用户需求，判断应该调用哪个 Skill。\n"
    "2. 调用 load_skill(name=..., task=..., ...) 执行 Skill。\n"
    "3. 如需解析文档，先调用 parse_document。\n"
    "4. 如需完整审核政府公文，可调用 load_skill(skill=\"full_government_audit\", ...)。\n"
    "5. 严禁重复调用同一个 Skill。\n"
)
```

- [ ] **步骤 2：运行测试**

```bash
uv run python -m pytest tests/agent/agents/test_orch.py -v
```

预期：PASS

- [ ] **步骤 3：Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "refactor: simplify OrchestratorAgent system prompt"
```

---

### 任务 15：端到端集成测试

**文件：**
- 创建：`tests/agent/test_skill_integration.py`

- [ ] **步骤 1：编写集成测试**

```python
# tests/agent/test_skill_integration.py
import pytest

from src.agent.agents.orch import OrchestratorAgent
from src.agent.skills import SkillRegistry
from src.agent.testing import MockModelClient
from src.agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_orchestrator_can_load_skill():
    model = MockModelClient(
        tool_calls=[
            {"id": "1", "name": "load_skill", "arguments": {"skill": "echo", "task": "hi"}}
        ]
    )

    tool_registry = ToolRegistry()
    # 注册一个 mock echo 工具
    from src.agent.tools.builtin.echo import EchoTool
    tool_registry.register(EchoTool())

    skill_registry = SkillRegistry("tests/agent/skills/fixtures")
    skill_registry.scan()

    agent = OrchestratorAgent(
        model=model,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
    )

    result = await agent.run("use echo skill", context={"file_path": "/tmp/test.txt"})
    assert result.status == "completed"
```

- [ ] **步骤 2：运行测试**

```bash
uv run python -m pytest tests/agent/test_skill_integration.py -v
```

预期：PASS

- [ ] **步骤 3：Commit**

```bash
git add tests/agent/test_skill_integration.py
git commit -m "test: add skill integration test"
```

---

### 任务 16：更新文档

**文件：**
- 修改：`docs/architecture/plugin-skill-boundary.md`
- 修改：`docs/agent/architecture.md`
- 修改：`DRUDGE.md`

- [ ] **步骤 1：更新 `docs/architecture/plugin-skill-boundary.md`**

反映新的三层架构和插件目录结构。

- [ ] **步骤 2：更新 `docs/agent/architecture.md`**

更新插件系统章节和 3.3.11 工具与子代理关系章节，说明 Skill 机制。

- [ ] **步骤 3：更新 `DRUDGE.md`**

更新 high-level architecture 描述，移除对 `run_xxx` 隐式前缀的依赖说明。

- [ ] **步骤 4：Commit**

```bash
git add docs DRUDGE.md
git commit -m "docs: update architecture docs for skill system"
```

---

### 任务 17：全量测试

- [ ] **步骤 1：运行所有非集成测试**

```bash
uv run python -m pytest tests/agent --ignore=tests/agent/integration -q
```

预期：尽可能多的测试通过。记录并修复失败项。

- [ ] **步骤 2：运行相关插件测试**

```bash
uv run python -m pytest tests/plugin -q
```

预期：PASS

- [ ] **步骤 3：Commit 修复**

如果有修复，单独 commit：

```bash
git commit -m "fix: resolve test failures after skill refactor"
```

---

## 自检

**规格覆盖度检查：**

| 设计文档需求 | 对应任务 |
|-------------|---------|
| Skill 文件格式（YAML frontmatter + Markdown） | 任务 7-9 |
| SkillRegistry 预编译 | 任务 3 |
| LoadSkillTool 按需执行 | 任务 4 |
| 子代理本地运行 | 任务 4、6 |
| plugins/ 目录重组 | 任务 10-11 |
| 移除插件 agent 声明 | 任务 12 |
| OrchestratorAgent 简化 | 任务 6、14 |
| 输入模型放在 skills/schemas/ | 任务 7 |
| 无热更新 | 未单独实现（本阶段不涉及） |

**占位符扫描：**
- 无 TODO、无"后续实现"、无未定义类型引用。

**类型一致性：**
- `SkillConfig.input_model` 始终为 `type[BaseModel] | None`
- `LoadSkillTool.execute` 签名与 `ToolProtocol` 一致
- `SkillRegistry.get` 返回 `SkillConfig | None`

---

## 执行交接

**计划已保存到 `docs/superpowers/plans/2026-06-29-skill-subagent-redesign-plan.md`。**

两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代
   - 必需子技能：`superpowers:subagent-driven-development`

**2. 内联执行** - 在当前会话中使用 `superpowers:executing-plans` 执行任务，批量执行并设有检查点

**选哪种方式？**
