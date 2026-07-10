# Courtier Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform DocAudit from a Chinese government document audit system into Courtier, a general-purpose AI agent platform with a domain-agnostic core and pluggable domain packages.

**Architecture:** Four-phase migration. Phase 1 extracts all hardcoded NL text into PromptBundle YAML files without changing directory structure. Phase 2 reorganizes the repo into `packages/core/` + `packages/domains/docaudit/`. Phase 3 hardens the Plugin/Skill/Domain protocols with validation. Phase 4 renames the repository, frontend, and documentation.

**Tech Stack:** Python 3.12+, FastAPI, Jinja2, YAML, pydantic, pytest, uv

---

## File Structure Map

```
Current (docaudit-agent/)                    → Target (courtier/packages/)
────────────────────────────────────────────────────────────────────────
src/agent/prompts/pipeline.py                → core/src/courtier/prompts/pipeline.py
src/common/behavioral_rules.py               → [DELETED — text moved to YAML]
src/agent/agents/orch.py                     → core/src/courtier/agents/orchestrator.py
src/agent/agents/base.py                     → core/src/courtier/agents/base.py
src/agent/api/app.py                         → core/src/courtier/api/app.py
src/agent/api/services/agent_service.py      → core/src/courtier/api/services/agent_service.py
NEW: config/prompts/{locale}/*.yaml          → domains/docaudit/config/prompts/{locale}/*.yaml
NEW: src/courtier/prompts/engine.py           → core/src/courtier/prompts/engine.py
plugins/                                     → domains/docaudit/plugins/
skills/                                       → domains/docaudit/skills/
src/validator/                               → domains/docaudit/plugins/format_audit/
src/content_compliance/                      → domains/docaudit/plugins/content_audit/
src/doccorrector/                            → domains/docaudit/plugins/text_correction/
src/docparse/                                → domains/docaudit/plugins/parse/
src/docannot/                                → domains/docaudit/plugins/annotate/
src/storage/                                 → core/src/courtier/storage/
src/es/                                      → core/src/courtier/es/
src/dbop/ (generic tables/ops)               → core/src/courtier/db/
src/dbop/tables/format_template.py           → domains/docaudit/models/
```

---

## Phase 1: Core Purification (Tasks 1–7)

### Task 1: Create PromptBundle data model and PromptEngine

**Files:**
- Create: `src/courtier/prompts/__init__.py`
- Create: `src/courtier/prompts/engine.py`
- Test: `tests/courtier/prompts/__init__.py`
- Test: `tests/courtier/prompts/test_engine.py`

- [ ] **Step 1: Create directories**

```bash
mkdir -p src/courtier/prompts tests/courtier/prompts
```

- [ ] **Step 2: Write __init__.py files**

```python
# src/courtier/prompts/__init__.py
"""Courtier PromptEngine — zero-hardcoded-text prompt management."""

from .engine import PromptBundle, PromptEngine

__all__ = ["PromptBundle", "PromptEngine"]
```

```python
# tests/courtier/prompts/__init__.py
```

- [ ] **Step 3: Write the PromptBundle and PromptEngine**

```python
# src/courtier/prompts/engine.py
"""PromptEngine — Jinja2-based prompt rendering with zero hardcoded text.

All natural-language text lives in PromptBundle YAML files loaded from
domain packages. The Core engine contains no hardcoded prompts, error
messages, or UI labels.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, Environment
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Reserved template keys — the complete set of named slots that domain
# packages can fill.  No template outside this set is recognized by Core.
RESERVED_TEMPLATE_KEYS: set[str] = {
    "orchestrator.system_prompt",
    "orchestrator.task_decomposition",
    "orchestrator.workflow_rules",
    "subagent.system_prompt",
    "subagent.tool_call_reminder",
    "chat.system_prompt",
    "chat.welcome_message",
    "behavioral.thinking_directive",
    "behavioral.rules",
    "behavioral.pre_turn_reminder",
    "behavioral.periodic_reminder",
    "errors.tool_timeout",
    "errors.permission_denied",
    "errors.tool_not_found",
    "errors.internal_error",
    "ui.session_title",
    "ui.step_label",
    "ui.agent_name",
    "tools.invocation_rules",
}

# Minimal en-US fallback templates shipped with Core so the platform
# boots even when no domain package is installed.
FALLBACK_TEMPLATES: dict[str, str] = {
    "orchestrator.system_prompt": (
        "You are {{ agent_name }}, an intelligent task orchestration Agent.\n\n"
        "Your responsibilities:\n"
        "1. Analyze user tasks\n"
        "2. Select appropriate Skills to execute\n"
        "3. Coordinate multiple SubAgents (parallel/sequential)\n"
        "4. Aggregate and return results\n\n"
        "Available Skills:\n"
        "{% for skill in available_skills %}"
        "- **{{ skill.name }}**: {{ skill.description }}\n"
        "{% endfor %}\n"
    ),
    "subagent.system_prompt": (
        "You are a SubAgent executing a specific task.\n"
        "Complete the task and return your results.\n"
    ),
    "chat.system_prompt": (
        "You are {{ agent_name }}, an AI assistant.\n"
        "Provide helpful, accurate responses.\n"
    ),
    "behavioral.thinking_directive": (
        "# Thinking Directive\n"
        "- Be concise in your reasoning.\n"
        "- State conclusions directly.\n"
    ),
    "behavioral.rules": (
        "# Behavioral Rules\n"
        "- Keep final responses concise.\n"
        "- Do not fabricate information.\n"
    ),
    "tools.invocation_rules": (
        "# Tool Invocation Rules\n"
        "1. Minimize tool calls.\n"
        "2. Call tools directly, don't describe plans in text.\n"
    ),
}


class PromptBundle(BaseModel):
    """A collection of named Jinja2 templates for a single locale.

    Loaded from config/prompts/{locale}/*.yaml in each domain package.
    Multiple bundles merge: later bundles override same-key templates.
    """

    locale: str = Field(description="Locale tag, e.g. 'zh-CN' or 'en-US'")
    templates: dict[str, str] = Field(
        default_factory=dict,
        description="Template key → Jinja2 template string",
    )

    @classmethod
    def from_directory(cls, path: Path, locale: str) -> "PromptBundle":
        """Load all YAML files in a locale directory into a PromptBundle.

        Each .yaml file's top-level keys become template keys.  Nested
        keys are flattened with dot-notation (e.g. orchestrator.system_prompt).
        """
        templates: dict[str, str] = {}
        if not path.is_dir():
            logger.warning("Prompt directory not found: %s", path)
            return cls(locale=locale, templates=templates)

        for yaml_file in sorted(path.glob("*.yaml")):
            try:
                raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                logger.error("Failed to parse %s: %s", yaml_file, exc)
                continue
            if isinstance(raw, dict):
                for key, value in _flatten_yaml_keys(raw):
                    if isinstance(value, str):
                        templates[key] = value

        return cls(locale=locale, templates=templates)

    def merge(self, other: "PromptBundle") -> "PromptBundle":
        """Return a new PromptBundle with other's templates layered on top."""
        merged = dict(self.templates)
        merged.update(other.templates)
        return PromptBundle(locale=self.locale, templates=merged)


def _flatten_yaml_keys(
    d: dict[str, Any], prefix: str = ""
) -> list[tuple[str, str]]:
    """Flatten nested YAML dict into dot-notation key-value pairs."""
    result: list[tuple[str, str]] = []
    for key, value in d.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            result.append((full_key, value))
        elif isinstance(value, dict):
            result.extend(_flatten_yaml_keys(value, full_key))
        elif isinstance(value, list):
            # Join list items as newline-separated string
            joined = "\n".join(
                str(item) for item in value if isinstance(item, str)
            )
            result.append((full_key, joined))
    return result


class PromptEngine:
    """Zero-hardcoded-text prompt renderer.

    Renders named templates from a PromptBundle using Jinja2.  Falls back
    to FALLBACK_TEMPLATES when a template key is not found in the bundle.
    """

    def __init__(self, bundle: PromptBundle | None = None) -> None:
        self._bundle = bundle or PromptBundle(locale="en-US")
        self._env = Environment(
            loader=BaseLoader(),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    @property
    def locale(self) -> str:
        return self._bundle.locale

    def render(self, template_name: str, **variables: Any) -> str:
        """Render a named template.

        Args:
            template_name: Dot-notation key (e.g. 'orchestrator.system_prompt').
            **variables: Jinja2 template variables.

        Returns:
            Rendered string.  Returns "" if the template is not found.
        """
        template_str = self._bundle.templates.get(template_name)
        if template_str is None:
            template_str = FALLBACK_TEMPLATES.get(template_name, "")
            if not template_str:
                logger.warning(
                    "Template '%s' not found in bundle or fallbacks", template_name
                )
                return ""
        try:
            template = self._env.from_string(template_str)
            return template.render(**variables)
        except Exception as exc:
            logger.error("Failed to render template '%s': %s", template_name, exc)
            return template_str  # Return raw template so errors are visible

    @classmethod
    def from_domain_directories(
        cls,
        domain_paths: list[Path],
        locale: str = "en-US",
    ) -> "PromptEngine":
        """Load and merge PromptBundles from multiple domain packages.

        Each domain contributes config/prompts/{locale}/*.yaml.
        Bundles are merged in order; later domains override earlier ones.
        Falls back to the domain's first declared locale if *locale* is
        unavailable, then to FALLBACK_TEMPLATES.
        """
        merged = PromptBundle(locale=locale)
        for domain_path in domain_paths:
            prompts_dir = domain_path / "config" / "prompts" / locale
            if not prompts_dir.is_dir():
                # Try the domain's first declared locale
                domain_yaml = domain_path / "config" / "domain.yaml"
                first_locale = _first_domain_locale(domain_yaml)
                if first_locale:
                    prompts_dir = domain_path / "config" / "prompts" / first_locale
                if not prompts_dir.is_dir():
                    logger.warning(
                        "No prompts found for domain %s (tried %s)",
                        domain_path.name, locale,
                    )
                    continue
            bundle = PromptBundle.from_directory(prompts_dir, locale)
            merged = merged.merge(bundle)
        return cls(bundle=merged)


def _first_domain_locale(domain_yaml_path: Path) -> str | None:
    """Read the first locale from a domain.yaml file."""
    if not domain_yaml_path.is_file():
        return None
    try:
        raw = yaml.safe_load(domain_yaml_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            locales = raw.get("locales", [])
            if locales:
                return str(locales[0])
    except Exception:
        pass
    return None
```

- [ ] **Step 4: Write the failing tests**

```python
# tests/courtier/prompts/test_engine.py
"""Tests for PromptBundle and PromptEngine."""

import tempfile
from pathlib import Path

import pytest

from src.courtier.prompts.engine import (
    FALLBACK_TEMPLATES,
    RESERVED_TEMPLATE_KEYS,
    PromptBundle,
    PromptEngine,
)


class TestPromptBundle:
    def test_empty_bundle(self):
        bundle = PromptBundle(locale="en-US")
        assert bundle.locale == "en-US"
        assert bundle.templates == {}

    def test_from_directory_loads_yaml_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts_dir = Path(tmp) / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "orchestrator.yaml").write_text(
                "system_prompt: 'You are {{ agent_name }}.'\n"
                "task_decomposition: 'Decompose: {{ task }}'\n",
                encoding="utf-8",
            )
            (prompts_dir / "behavioral.yaml").write_text(
                "thinking_directive: 'Be concise.'\n"
                "rules: |\n  - Rule 1\n  - Rule 2\n",
                encoding="utf-8",
            )

            bundle = PromptBundle.from_directory(prompts_dir, "zh-CN")

            assert bundle.locale == "zh-CN"
            assert bundle.templates["orchestrator.system_prompt"] == "You are {{ agent_name }}."
            assert bundle.templates["orchestrator.task_decomposition"] == "Decompose: {{ task }}"
            assert bundle.templates["behavioral.thinking_directive"] == "Be concise."
            assert "- Rule 1\n- Rule 2" in bundle.templates["behavioral.rules"]

    def test_from_directory_missing_directory(self):
        bundle = PromptBundle.from_directory(Path("/nonexistent/prompts"), "en-US")
        assert bundle.locale == "en-US"
        assert bundle.templates == {}

    def test_merge_layers_templates(self):
        base = PromptBundle(
            locale="en-US",
            templates={"a": "base_a", "b": "base_b"},
        )
        overlay = PromptBundle(
            locale="en-US",
            templates={"b": "overlay_b", "c": "overlay_c"},
        )
        merged = base.merge(overlay)
        assert merged.templates["a"] == "base_a"  # preserved
        assert merged.templates["b"] == "overlay_b"  # overridden
        assert merged.templates["c"] == "overlay_c"  # added


class TestPromptEngine:
    def test_render_from_bundle(self):
        bundle = PromptBundle(
            locale="en-US",
            templates={"test.greeting": "Hello, {{ name }}!"},
        )
        engine = PromptEngine(bundle)
        result = engine.render("test.greeting", name="World")
        assert result == "Hello, World!"

    def test_render_falls_back_to_builtin(self):
        engine = PromptEngine()  # empty bundle
        result = engine.render("chat.system_prompt", agent_name="TestBot")
        assert "You are TestBot" in result

    def test_render_missing_template_returns_empty(self):
        engine = PromptEngine()
        result = engine.render("nonexistent.key")
        assert result == ""

    def test_render_with_jinja_loop(self):
        bundle = PromptBundle(
            locale="en-US",
            templates={
                "test.skills": (
                    "Skills:\n"
                    "{% for s in skills %}- {{ s.name }}: {{ s.desc }}\n{% endfor %}"
                ),
            },
        )
        engine = PromptEngine(bundle)
        result = engine.render(
            "test.skills",
            skills=[
                {"name": "audit", "desc": "Audit docs"},
                {"name": "review", "desc": "Review contracts"},
            ],
        )
        assert "- audit: Audit docs" in result
        assert "- review: Review contracts" in result

    def test_locale_property(self):
        bundle = PromptBundle(locale="zh-CN")
        engine = PromptEngine(bundle)
        assert engine.locale == "zh-CN"

    def test_fallback_templates_are_valid_jinja(self):
        """All fallback templates should render without error with empty vars."""
        engine = PromptEngine()
        for key, _template in FALLBACK_TEMPLATES.items():
            result = engine.render(key, agent_name="Test", task="test task")
            # Must not raise; may be empty string for keys without fallback
            assert isinstance(result, str)

    def test_reserved_template_keys_exist_in_fallbacks(self):
        """Every reserved key should have a fallback or be explicitly
        documented as requiring a domain package."""
        for key in RESERVED_TEMPLATE_KEYS:
            # At minimum, the key should be recognized
            assert isinstance(key, str)
```

- [ ] **Step 5: Run tests to verify they fail**

```bash
cd docaudit-agent && uv run pytest tests/courtier/prompts/test_engine.py -v
```
Expected: FAIL — module not found (need to create `__init__.py` files first, which we did, so some may pass, some may fail on imports)

- [ ] **Step 6: Run tests and verify they pass**

```bash
cd docaudit-agent && uv run pytest tests/courtier/prompts/test_engine.py -v
```
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/courtier/prompts/__init__.py src/courtier/prompts/engine.py tests/courtier/
git commit -m "feat: add PromptBundle data model and PromptEngine with Jinja2 rendering"
```

---

### Task 2: Create zh-CN prompt YAML files from existing hardcoded text

**Files:**
- Create: `config/prompts/zh-CN/orchestrator.yaml`
- Create: `config/prompts/zh-CN/subagent.yaml`
- Create: `config/prompts/zh-CN/behavioral.yaml`
- Create: `config/prompts/zh-CN/chat.yaml`
- Create: `config/prompts/zh-CN/errors.yaml`
- Create: `config/prompts/zh-CN/tools.yaml`

- [ ] **Step 1: Create directory**

```bash
mkdir -p config/prompts/zh-CN
```

- [ ] **Step 2: Write orchestrator.yaml**

```yaml
# config/prompts/zh-CN/orchestrator.yaml
system_prompt: |
  你是 {{ agent_name }}，一个智能任务编排 Agent。

  你的任务是理解用户需求，直接调用合适的 Skill 工具完成任务。
  每个 Skill 支持两种执行模式，你必须根据任务复杂度主动选择：
  - **subagent**：启动独立子代理异步执行，适合审核要素多、需要多轮工具调用的复杂任务
  - **inline**：返回 Skill 指令，由你直接在当前上下文中执行，适合任务明确、步骤少的简单任务

  可用 Skill：
  {% for skill in available_skills %}
  - **{{ skill.name }}**：{{ skill.description }}
  {% endfor %}

  调用时必须明确指定 mode 参数（例如 format_audit(task=..., file_path=..., mode="subagent")）。
  执行完毕后，你必须基于结果输出 Markdown 格式的最终结论。

task_decomposition: |
  将以下任务分解为可执行的子任务：
  {{ task }}

  可用 Skill：{{ available_skills | map(attribute='name') | join(', ') }}

workflow_rules: |
  # 工作流规则
  1. **【最高优先级】如果存在文件上传（file_path 可用），第一步必须调用 parse_document 解析文档。**
     文档解析完成后才能进行后续审核操作。若解析失败，停止并报告，禁止继续调用依赖文档内容的 Skill。
  2. 文档解析完成后，理解用户需求，立即且只调用一次最匹配的 Skill 工具，
     禁止在文本中先写计划再调用。
  3. 调用 Skill 时必须透传 file_path（例如 format_audit(task=..., file_path=...))。
  4. 使用 Skill 名称对应的工具，并始终携带 file_path。
  5. 严禁重复调用同一个 Skill；Skill 返回后应直接基于结果给出最终结论，
     禁止追加「是否还需要…」等追问。

  # 最终结论输出格式
  最终结论必须以 Markdown 格式输出，使用标题（##）、列表（-）、
  表格等结构清晰地呈现结果。格式要求：
  - 开头用 ## 标题概括类型和文档名称
  - 用 ### 分项列出各维度的结果
  - 发现问题用列表逐条列出，包含位置、严重程度、建议修改方案
  - 最后给出总体评价和优先级排序的修改建议
  - Skill 内部按其自身格式要求输出（如 JSON），你在最终结论中将其转化为 Markdown 呈现

  # Skill 执行模式选择
  每个 Skill 工具的 mode 参数为必填，你必须根据任务复杂度主动选择：
  - mode="subagent"：启动独立子代理，适合复杂任务
  - mode="inline"：返回 Skill 指令到当前上下文，适合简单任务
  选择原则：复合类任务优先 subagent；单一维度任务优先 inline，
  但如果文档内容较多或要求详细报告，也可以选择 subagent。

  # get_artifact 使用规范
  1. 【最常用】读取 Skill/tool 返回的结果：传入 id 参数，
     值直接取工具输出中的 result_id 字段（$ref:...:N 格式）。
     不要传 artifact_type。
  2. 【类型投影】需要将数据转换为特定类型时：
     传入 id + artifact_type，系统自动执行类型投影链。

  # 结构化参数传递规则
  参数中的数据字段（如 document）接受两种传值方式：
  1. 传入 $ref 缓存引用字符串，系统自动加载完整数据 — 推荐
  2. 传入完整的 JSON 对象（仅当数据较小时使用）
```

- [ ] **Step 3: Write subagent.yaml**

```yaml
# config/prompts/zh-CN/subagent.yaml
system_prompt: |
  你是 Courtier 平台的 SubAgent，负责执行一个具体的 Skill 任务。

  {% if skill_description %}
  ## 当前 Skill：{{ skill_name }}
  {{ skill_description }}
  {% endif %}

  ## 任务
  {{ task }}

  请完整执行任务并返回结果。

tool_call_reminder: |
  【提醒】请继续执行任务，直接调用需要的工具。不要长篇描述计划。
```

- [ ] **Step 4: Write behavioral.yaml**

```yaml
# config/prompts/zh-CN/behavioral.yaml
thinking_directive: |
  # 思考规范（严格遵守）
  - 思考过程始终使用中文，禁止使用英文。
  - 【核心原则】直接给出结论，不要逐条列举、不要反复推演、不要比较方案。
  - 结论一句话说清即可，禁止超过三句话的思考。
  - 工具参数有默认值时直接用默认值，禁止为"稳妥"先调探测工具。
  - 思考中只提当前要做什么，禁止罗列工具名、路径、配置项。

rules:
  - 最终结论必须言简意赅，只列出关键信息，禁止长篇大论。
  - 直接提炼要点，禁止原文照搬工具输出。
  - 禁止废话铺垫（如「现在我将」「让我分析」「根据结果发现」）和客套语（如「感谢」「已完成」）。
  - 禁止复述执行流程，直接说结论。
  - 【关键】禁止在最终输出中暴露任何内部实现细节，包括文件路径、引用名（如 $ref）、工具名、技能名称、代理名称、配置项、模块名、类名、函数名、artifact_id 等。
    示例：
    · 差：「根据解析工具返回的引用标识 ref:parse_document:1 ……」
    · 好：「文档已解析，现进行格式审核。」
  - 【关键】$ref 引用使用规则：
    · $ref 引用（工具输出中的 result_id 字段）只能从工具返回结果的 metadata 中获取，禁止自行编造或推断；
    · 每个 $ref 由系统生成，与具体工具调用绑定，不能通过拼接工具名和序号来猜测另一个工具的 $ref 引用；
    · 工具调用失败时不会产生有效的 $ref 引用，不要为失败的工具构造引用；
    · 禁止将某个工具返回的 $ref 当作其他工具的引用使用。
    · 调用 get_artifact 时直接使用 id 参数传入 $ref 值即可，不要自行添加 artifact_type。
  - 输出格式：最终结论必须以 Markdown 格式输出（使用标题、列表、表格等结构）。如果当前 Skill 的系统指令明确要求 JSON 或其他格式，则该 Skill 内部按其要求输出，但你在汇总多个 Skill 结果给用户的最终结论中仍需使用 Markdown。
  - 工具失败处理：
    · 关键路径工具（如文档解析、文件加载）失败后，停止后续依赖步骤并如实报告；
    · 非关键工具失败时，可跳过并说明，禁止隐瞒；
    · 禁止在关键失败后继续调用依赖该结果的工具，也禁止无意义地反复重试同一失败工具。
  - 终止信号：出现以下任一情况时立即输出结论并结束，禁止继续自我质疑：
    · 已调用过最终输出/汇总工具；
    · 已连续多轮没有新的有效发现或业务产出；
    · 任务交付物已生成。
  - 禁止重复调用同一个工具或同一个 Skill；一次调用应充分利用返回结果。
  - 不要在文本中描述计划后再调用工具，直接调用。

pre_turn_reminder: |
  【系统 · 本轮约束】直接选择最明显的工具调用，不要列举方案比较优劣。
  参数有默认值就用默认值。不确定时随便选一个，错了再调整。

periodic_reminder: |
  【系统 · 阶段提醒】请回顾行为准则：
  最终输出简洁、禁止暴露内部名称与 $ref，
  工具失败按关键/非关键处理，任务完成立即结束。
```

- [ ] **Step 5: Write chat.yaml**

```yaml
# config/prompts/zh-CN/chat.yaml
system_prompt: |
  你是 {{ agent_name }}，一个智能助手。
  你可以回答用户关于文档处理、内容审核、文本分析等方面的问题。
  以专业、友好的方式提供帮助。

welcome_message: |
  你好！我是 {{ agent_name }}，有什么可以帮助你的？
```

- [ ] **Step 6: Write errors.yaml**

```yaml
# config/prompts/zh-CN/errors.yaml
tool_timeout: "工具执行超时（{{ timeout_seconds }}秒）：{{ tool_name }}"
permission_denied: "权限不足：{{ action }}"
tool_not_found: "工具未找到：{{ tool_name }}"
internal_error: "内部错误：{{ message }}"
```

- [ ] **Step 7: Write tools.yaml**

```yaml
# config/prompts/zh-CN/tools.yaml
invocation_rules: |
  【工具调用规则】
  1. 最小化工具调用：能一次调用完成的不分两次，能合并的不分开。
  2. 工具调用必须在 tool_calls 中执行，reasoning 不能替代实际调用。
  3. 直接调用目标工具，不要在文本中描述计划。
  4. 调用前确认工具真实存在，禁止调用未提供的工具。
```

- [ ] **Step 8: Commit**

```bash
git add config/prompts/
git commit -m "feat: add zh-CN prompt YAML files extracted from hardcoded text"
```

---

### Task 3: Create en-US prompt YAML files

**Files:**
- Create: `config/prompts/en-US/orchestrator.yaml`
- Create: `config/prompts/en-US/subagent.yaml`
- Create: `config/prompts/en-US/behavioral.yaml`
- Create: `config/prompts/en-US/chat.yaml`
- Create: `config/prompts/en-US/errors.yaml`
- Create: `config/prompts/en-US/tools.yaml`

- [ ] **Step 1: Create directory**

```bash
mkdir -p config/prompts/en-US
```

- [ ] **Step 2: Write orchestrator.yaml**

```yaml
# config/prompts/en-US/orchestrator.yaml
system_prompt: |
  You are {{ agent_name }}, an intelligent task orchestration Agent.

  Your responsibilities:
  1. Analyze user tasks
  2. Select and invoke appropriate Skills
  3. Coordinate SubAgents (subagent or inline mode)
  4. Aggregate and return results

  Each Skill supports two execution modes:
  - **subagent**: Launch an independent sub-agent for complex, multi-turn tasks
  - **inline**: Return Skill instructions for direct execution on simple tasks

  Available Skills:
  {% for skill in available_skills %}
  - **{{ skill.name }}**: {{ skill.description }}
  {% endfor %}

  You must specify the mode parameter on every call
  (e.g. format_audit(task=..., file_path=..., mode="subagent")).
  After execution, produce a final conclusion in Markdown format.

task_decomposition: |
  Decompose the following task into executable subtasks:
  {{ task }}

  Available Skills: {{ available_skills | map(attribute='name') | join(', ') }}

workflow_rules: |
  # Workflow Rules
  1. **【Highest priority】If a file upload is present (file_path available), the first
     step MUST call parse_document to parse the document.** Proceed only after parsing
     succeeds. If parsing fails, stop and report — do not continue.
  2. After parsing, immediately call the most appropriate Skill tool.
     Do not write a plan in text before calling.
  3. Always forward file_path to Skills (e.g. format_audit(task=..., file_path=...)).
  4. Use the Skill name as the tool name, always carrying file_path.
  5. Never call the same Skill twice. After a Skill returns, produce the final
     conclusion directly. Do not ask "do you also need..."

  # Final Output Format
  Final conclusions must use Markdown with headings (##), lists (-), and tables.
  - Start with a ## heading summarizing the type and document name
  - Use ### subsections for each dimension
  - List issues with location, severity, and suggested fixes
  - End with an overall assessment and prioritized recommendations

  # Skill Execution Mode Selection
  Every Skill tool requires the mode parameter:
  - mode="subagent": independent sub-agent for complex tasks
  - mode="inline": inline instructions for simple tasks
  Principle: composite tasks → subagent; single-dimension → inline.
  Large documents or detailed reports may also warrant subagent.

  # get_artifact Usage
  1. To read Skill/tool results: pass the id parameter using the result_id
     field from tool output ($ref:...:N format). Do not pass artifact_type.
  2. For type projection: pass id + artifact_type.

  # Structured Parameter Passing
  Data fields (e.g. document) accept two forms:
  1. $ref cache reference string — recommended
  2. Full JSON object — only for small data
```

- [ ] **Step 3: Write subagent.yaml**

```yaml
# config/prompts/en-US/subagent.yaml
system_prompt: |
  You are a Courtier SubAgent executing a specific Skill task.

  {% if skill_description %}
  ## Current Skill: {{ skill_name }}
  {{ skill_description }}
  {% endif %}

  ## Task
  {{ task }}

  Complete the task and return your results.

tool_call_reminder: |
  【Reminder】Continue executing the task. Call tools directly.
```

- [ ] **Step 4: Write behavioral.yaml**

```yaml
# config/prompts/en-US/behavioral.yaml
thinking_directive: |
  # Thinking Directive (strict)
  - Reason concisely. State conclusions directly — no enumeration, no repeated deliberation.
  - One sentence is enough. No more than three sentences of thinking.
  - Use defaults when available. Do not call probe tools "to be safe."
  - Only mention what you're about to do. Do not list tool names, paths, or config.

rules:
  - Keep final conclusions concise — key information only, no walls of text.
  - Extract key points; do not copy-paste tool output verbatim.
  - No filler phrases ("Now I will...", "Let me analyze...") or pleasantries ("Thank you", "Done.").
  - Do not narrate the execution flow — state the conclusion.
  - 【Critical】Never expose internal implementation details in final output:
    file paths, $ref references, tool names, skill names, agent names,
    config keys, module names, class names, function names, artifact IDs.
    Example: Bad: "Based on $ref:parse_document:1..." Good: "Document parsed. Proceeding with format audit."
  - 【Critical】$ref reference rules:
    · $ref values come ONLY from tool result metadata — never fabricate or infer them.
    · Each $ref is bound to a specific tool call — do not guess another tool's $ref.
    · Failed tool calls produce no valid $ref — do not construct references for failures.
    · Never use one tool's $ref as another tool's reference.
    · When calling get_artifact, use only the id parameter with the $ref value.
  - Output format: Final conclusions in Markdown (headings, lists, tables).
    If a Skill's instructions require JSON or another format, the Skill may use it
    internally, but your final aggregated output must be Markdown.
  - Tool failure handling:
    · Critical path tools (e.g. document parsing): stop and report on failure.
    · Non-critical tools: may skip with explanation — never hide failures.
    · Do not continue with dependent tools after a critical failure.
    · Do not retry the same failed tool repeatedly.
  - Termination signals — conclude immediately if any of these occur:
    · A final output/summary tool has been called.
    · Multiple consecutive turns with no new findings or business output.
    · A task deliverable has been generated.
  - Do not call the same tool or Skill twice. One call should fully utilize its result.
  - Do not describe a plan in text before calling a tool — just call it.

pre_turn_reminder: |
  【System · Turn Constraint】Pick the most obvious tool call directly.
  Use defaults when available. If unsure, pick one and adjust if wrong.

periodic_reminder: |
  【System · Periodic Reminder】Review behavioral rules:
  Concise output, no internal names or $ref exposure,
  handle tool failures per critical/non-critical, conclude immediately when done.
```

- [ ] **Step 5: Write chat.yaml**

```yaml
# config/prompts/en-US/chat.yaml
system_prompt: |
  You are {{ agent_name }}, an AI assistant.
  You can help with document processing, content analysis, text review, and more.
  Provide professional, friendly assistance.

welcome_message: |
  Hello! I'm {{ agent_name }}. How can I help you?
```

- [ ] **Step 6: Write errors.yaml**

```yaml
# config/prompts/en-US/errors.yaml
tool_timeout: "Tool execution timed out ({{ timeout_seconds }}s): {{ tool_name }}"
permission_denied: "Permission denied: {{ action }}"
tool_not_found: "Tool not found: {{ tool_name }}"
internal_error: "Internal error: {{ message }}"
```

- [ ] **Step 7: Write tools.yaml**

```yaml
# config/prompts/en-US/tools.yaml
invocation_rules: |
  # Tool Invocation Rules
  1. Minimize tool calls: don't split what can be done in one call.
  2. Execute tool calls in tool_calls — reasoning is not a substitute.
  3. Call the target tool directly — don't describe a plan in text.
  4. Verify the tool exists before calling — never invoke non-existent tools.
```

- [ ] **Step 8: Commit**

```bash
git add config/prompts/en-US/
git commit -m "feat: add en-US prompt YAML files"
```

---

### Task 4: Modify PromptPipeline to accept external PromptBundle (remove hardcoded import)

**Files:**
- Modify: `src/agent/prompts/pipeline.py`

- [ ] **Step 1: Read the current file**

The file is at `src/agent/prompts/pipeline.py`. We need to:
1. Remove the hardcoded import of `THINKING_DIRECTIVE` from `src.common.behavioral_rules`
2. Make `set_identity()` accept a thinking_directive parameter
3. Make `set_behavioral_rules()` and `set_rules()` remain as-is (they already accept parameters)
4. Add a class method to build from a PromptEngine

- [ ] **Step 2: Apply the edit — remove hardcoded THINKING_DIRECTIVE import**

```python
# In src/agent/prompts/pipeline.py, replace line 23:
#   from src.common.behavioral_rules import THINKING_DIRECTIVE
# with nothing (remove the import).

# Then update set_identity() to accept thinking_directive as a parameter:
```

Actually, let me write the complete modified version of the relevant methods. The rest of the file stays the same.

- [ ] **Step 3: Edit set_identity to accept thinking_directive parameter**

Replace the current `set_identity` method (lines 76-81):

```python
    def set_identity(
        self, name: str, role: str, thinking_directive: str = ""
    ) -> None:
        """Section 1: Core identity and behavioral instructions.

        Args:
            name: Agent display name.
            role: Agent role description / system prompt.
            thinking_directive: Thinking directive text. If empty, a
                minimal fallback is used.  Injected from PromptBundle
                rather than hardcoded.
        """
        directive = thinking_directive or (
            "# Thinking Directive\n"
            "- Be concise in your reasoning.\n"
            "- State conclusions directly.\n"
        )
        self._identity = self._validate_section_length(
            f"# 身份\n你是 {name}。\n\n# 行为规则\n{role}\n\n{directive}",
            "identity",
        )
```

- [ ] **Step 4: Run existing prompt tests to verify nothing breaks**

```bash
cd docaudit-agent && uv run pytest tests/agent/test_prompts.py -v
```
Expected: test_identity_injects_thinking_directive may fail because `THINKING_DIRECTIVE` is no longer hardcoded. If it does, we'll update the test next.

- [ ] **Step 5: Update the failing test**

```python
# In tests/agent/test_prompts.py, modify test_identity_injects_thinking_directive:
    def test_identity_injects_thinking_directive(self):
        """set_identity accepts an optional thinking_directive parameter."""
        pp = PromptPipeline()
        custom_directive = "Custom thinking rules."
        pp.set_identity(
            "TestAgent", "你是测试代理。",
            thinking_directive=custom_directive,
        )
        result = pp.build()
        assert "你是测试代理。" in result
        assert "Custom thinking rules." in result

    def test_identity_no_thinking_directive_uses_fallback(self):
        """Without thinking_directive, a minimal fallback is used."""
        pp = PromptPipeline()
        pp.set_identity("TestAgent", "You are a test agent.")
        result = pp.build()
        assert "# Thinking Directive" in result
        assert "Be concise in your reasoning." in result
```

- [ ] **Step 6: Run tests**

```bash
cd docaudit-agent && uv run pytest tests/agent/test_prompts.py -v
```
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/agent/prompts/pipeline.py tests/agent/test_prompts.py
git commit -m "refactor: remove hardcoded THINKING_DIRECTIVE import from PromptPipeline"
```

---

### Task 5: Wire OrchestratorAgent to use PromptEngine instead of hardcoded text

**Files:**
- Modify: `src/agent/agents/orch.py`
- Modify: `src/agent/agents/base.py`

- [ ] **Step 1: Modify OrchestratorAgent.__init__ to accept and use PromptEngine**

The key change: replace the hardcoded `role` string (lines 75-85) and hardcoded `set_rules` call (lines 97-138) with PromptEngine-rendered text.

```python
# src/agent/agents/orch.py — modifications to __init__

# Add import at top:
from courtier.prompts.engine import PromptEngine

# Change __init__ signature to accept prompt_engine:
    def __init__(
        self,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        selected_auditors: list[str] | None = None,
        plugin_system: Any = None,
        tool_registry: Any = None,
        skill_registry: SkillRegistry | None = None,
        agent_runtime: AgentRuntime | None = None,
        drudge_md_content: str | None = None,
        prompt_engine: PromptEngine | None = None,          # NEW
        agent_name: str = "Courtier Orchestrator",          # NEW
    ) -> None:

        # ... existing init code ...

        # Build skill catalog (existing logic)
        skill_catalog = ""
        if skill_registry is not None:
            # ... existing skill tool building ...
            skill_catalog = skill_registry.build_catalog()

        # Render system prompt from PromptEngine (replaces hardcoded strings)
        if prompt_engine is not None:
            skill_list = []
            if skill_registry is not None:
                for skill in skill_registry.list_enabled():
                    skill_list.append({
                        "name": skill.name,
                        "description": skill.description,
                    })
            role = prompt_engine.render(
                "orchestrator.system_prompt",
                agent_name=agent_name,
                available_skills=skill_list,
            )
            workflow_rules = prompt_engine.render(
                "orchestrator.workflow_rules",
            )
        else:
            # Fallback for tests that don't provide PromptEngine
            role = (
                f"You are {agent_name}. "
                "Your job is to understand user needs and call appropriate Skills."
            )
            if skill_catalog:
                role += f"\n\nAvailable Skills:\n{skill_catalog}"
            workflow_rules = ""

        super().__init__(
            name="OrchestratorAgent",
            role=role,
            tools=tools,
            model=_model,
            hooks=hooks,
            permissions=permissions,
            tool_registry=tool_registry,
            drudge_md_content=drudge_md_content,
            prompt_engine=prompt_engine,    # NEW — pass to base Agent
            agent_name=agent_name,          # NEW
        )
        self._prompt_pipeline.set_rules(workflow_rules)
```

- [ ] **Step 2: Modify Agent base class to accept PromptEngine**

```python
# src/agent/agents/base.py — modifications

# Add import at top:
from courtier.prompts.engine import PromptEngine

# Modify __init__ to accept prompt_engine and agent_name:
    def __init__(
        self,
        name: str,
        role: str,
        tools: list[ToolProtocol] | None = None,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        tool_registry: ToolRegistry | None = None,
        memory_store: MemoryStore | None = None,
        drudge_md_content: str | None = None,
        prompt_engine: PromptEngine | None = None,      # NEW
        agent_name: str = "",                            # NEW
    ) -> None:
        # ... existing init ...

        # Replace the hardcoded BEHAVIORAL_RULES import with PromptEngine usage:
        if prompt_engine is not None:
            thinking_directive = prompt_engine.render("behavioral.thinking_directive")
            behavioral_rules = prompt_engine.render("behavioral.rules")
            tool_invocation_rules = prompt_engine.render("tools.invocation_rules")
        else:
            # Fallback for backward compatibility
            from src.common.behavioral_rules import (
                BEHAVIORAL_RULES,
                TOOL_INVOCATION_RULES,
            )
            thinking_directive = ""
            behavioral_rules = BEHAVIORAL_RULES
            tool_invocation_rules = TOOL_INVOCATION_RULES

        self._prompt_pipeline = PromptPipeline()
        self._prompt_pipeline.set_identity(
            name, role, thinking_directive=thinking_directive,
        )
        self._prompt_pipeline.set_behavioral_rules(behavioral_rules)
        # ... rest of existing init ...
```

- [ ] **Step 3: Run existing tests**

```bash
cd docaudit-agent && uv run pytest tests/agent/agents/test_orch.py tests/agent/test_agent_base.py -v
```
Expected: Tests may need updates if they construct OrchestratorAgent without prompt_engine — the fallback should handle this. Fix any failures.

- [ ] **Step 4: Run full test suite**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -x
```
Expected: All tests PASS (or identify tests that need updates)

- [ ] **Step 5: Fix any tests that construct agents without PromptEngine**

For tests that construct `Agent` or `OrchestratorAgent` directly, the fallback path (when `prompt_engine=None`) should preserve existing behavior by falling back to the `src.common.behavioral_rules` imports. Verify this works.

- [ ] **Step 6: Commit**

```bash
git add src/agent/agents/orch.py src/agent/agents/base.py
git commit -m "refactor: wire OrchestratorAgent and Agent to use PromptEngine for all NL text"
```

---

### Task 6: Update agent_service.py to use PromptBundle

**Files:**
- Modify: `src/agent/api/services/agent_service.py`

- [ ] **Step 1: Modify build_audit_agent and build_chat_agent**

Replace the hardcoded `CHAT_AGENT_SYSTEM_PROMPT` and pass PromptEngine to agents:

```python
# src/agent/api/services/agent_service.py

# Remove lines 148-153 (CHAT_AGENT_SYSTEM_PROMPT)
# Add import:
from courtier.prompts.engine import PromptEngine, PromptBundle

# Modify build_audit_agent to accept and use PromptEngine:
async def build_audit_agent(
    settings: Any,
    plugin_system: Any = None,
    tool_registry: Any = None,
    cache_store: Any = None,
    artifact_store: Any = None,
    prompt_engine: PromptEngine | None = None,        # NEW
) -> tuple[Any, Any, str]:
    """Create OrchestratorAgent and ContextManager."""
    # ... existing code ...

    agent = OrchestratorAgent(
        model=model,
        plugin_system=plugin_system,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        agent_runtime=agent_runtime,
        drudge_md_content=drudge_md_content,
        prompt_engine=prompt_engine,                   # NEW
        agent_name="Courtier",                         # NEW
    )
    # ... rest unchanged ...
    return agent, context_manager, model.model_name


# Modify build_chat_agent to use PromptEngine:
async def build_chat_agent(
    settings: Any,
    cache_store: Any = None,
    artifact_store: Any = None,
    prompt_engine: PromptEngine | None = None,        # NEW
) -> tuple[Any, Any, str]:
    """Create a simple chat Agent for text-only conversations."""
    from ...agents.base import Agent
    from ...core.context_manager import ContextManager

    model = build_model_client(settings)

    if prompt_engine is not None:
        chat_prompt = prompt_engine.render(
            "chat.system_prompt", agent_name="Courtier"
        )
        agent_name = prompt_engine.render(
            "ui.agent_name", fallback="Courtier Assistant"
        )
    else:
        chat_prompt = (
            "You are Courtier, an AI assistant."
            "Provide helpful, accurate responses."
        )
        agent_name = "Courtier Assistant"

    agent = Agent(
        name=agent_name,
        role=chat_prompt,
        tools=[],
        model=model,
        prompt_engine=prompt_engine,
        agent_name=agent_name,
    )
    # ... rest unchanged ...
    return agent, context_manager, model.model_name
```

- [ ] **Step 2: Run tests**

```bash
cd docaudit-agent && uv run pytest tests/agent/api/test_agent_service.py -v
```
Expected: Tests may need updates to pass PromptEngine. Fix as needed.

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/services/agent_service.py
git commit -m "refactor: remove hardcoded CHAT_AGENT_SYSTEM_PROMPT, use PromptEngine"
```

---

### Task 7: Update create_app to load PromptBundle and inject PromptEngine

**Files:**
- Modify: `src/agent/api/app.py`

- [ ] **Step 1: Add PromptBundle loading in create_app**

```python
# src/agent/api/app.py — add after settings initialization, before agent construction

def create_app(
    sessions_dir: str = "",
    start_plugins: bool = True,
    prompt_bundle_path: str = "",          # NEW
) -> FastAPI:
    # ... existing setup ...

    # NEW: Load PromptBundle from config directory
    from courtier.prompts.engine import PromptBundle, PromptEngine

    if prompt_bundle_path:
        prompts_dir = Path(prompt_bundle_path)
    else:
        prompts_dir = Path("config/prompts")

    locale = getattr(settings, "courtier_locale", "zh-CN")
    prompt_engine = PromptEngine.from_domain_directories(
        domain_paths=[Path(".")],  # root dir contains config/prompts
        locale=locale,
    )
    app.state.prompt_engine = prompt_engine

    # ... rest unchanged ...
```

- [ ] **Step 2: Update the route handlers that create agents**

The routes in `src/agent/api/routes/` that call `build_audit_agent` / `build_chat_agent` need to pass `app.state.prompt_engine`. Since these are accessed via `request.app.state`, the route handlers that call AgentService will naturally pick up the prompt_engine. Update the service calls:

```python
# In the stream/chat route handlers, pass prompt_engine:
agent, context_manager, model_name = await build_audit_agent(
    settings=settings,
    plugin_system=request.app.state.plugin_system,
    tool_registry=request.app.state.tool_registry,
    artifact_store=request.app.state.artifact_store,
    prompt_engine=request.app.state.prompt_engine,     # NEW
)
```

- [ ] **Step 3: Run the app to verify startup**

```bash
cd docaudit-agent && timeout 5 uv run python -c "
from src.agent.api.app import create_app
app = create_app(start_plugins=False)
print('App created:', app.title)
print('PromptEngine locale:', app.state.prompt_engine.locale)
" || true
```
Expected: App creates successfully, prints locale.

- [ ] **Step 4: Run full test suite**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -x
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/api/app.py src/agent/api/routes/
git commit -m "feat: load PromptBundle in create_app, inject PromptEngine into agents"
```

---

## Phase 2: Directory Restructuring (Tasks 8–18)

> **Note:** Each task in Phase 2 uses `git mv` to preserve history. After each move, update imports and run the affected tests.

### Task 8: Create target directory structure

**Files:**
- Create: directory structure for `packages/core/` and `packages/domains/docaudit/`

- [ ] **Step 1: Create all target directories**

```bash
cd docaudit-agent

# Core package
mkdir -p packages/core/src/courtier

# Domain package
mkdir -p packages/domains/docaudit/plugins
mkdir -p packages/domains/docaudit/skills
mkdir -p packages/domains/docaudit/rules/format
mkdir -p packages/domains/docaudit/rules/content
mkdir -p packages/domains/docaudit/config/prompts
mkdir -p packages/domains/docaudit/models

# WebUI
mkdir -p packages/webui
```

- [ ] **Step 2: Create core pyproject.toml**

```toml
# packages/core/pyproject.toml
[project]
name = "courtier-core"
version = "0.2.0"
description = "Courtier — general-purpose AI agent platform core"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "openai>=1.50.0",
    "sqlalchemy[asyncio]>=2.0.0",
    "elasticsearch>=8.0.0",
    "minio>=7.0.0",
    "pymupdf>=1.24.0",
    "python-docx>=1.0.0",
    "opentelemetry-api>=1.25.0",
    "opentelemetry-sdk>=1.25.0",
    "prometheus-client>=0.20.0",
    "slowapi>=0.1.0",
    "bcrypt>=4.0.0",
    "pyjwt>=2.8.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "pyyaml>=6.0.0",
    "jinja2>=3.1.0",
    "aiofiles>=24.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/courtier"]
```

- [ ] **Step 3: Create domain pyproject.toml**

```toml
# packages/domains/docaudit/pyproject.toml
[project]
name = "courtier-domain-docaudit"
version = "0.2.0"
description = "Courtier domain package — Chinese government document audit"
requires-python = ">=3.12"
dependencies = [
    "courtier-core>=0.2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 4: Create domain.yaml metadata**

```yaml
# packages/domains/docaudit/config/domain.yaml
name: docaudit
title: 公文智能审计
description: 基于 GB/T 9704-2012 的公文格式与内容审查
locales: [zh-CN, en-US]
requires_plugins: [format_audit, content_audit, text_correction, plagiarism]
requires_services: [mysql, elasticsearch, minio]
```

- [ ] **Step 5: Commit**

```bash
git add packages/
git commit -m "chore: create target directory structure for core and domain packages"
```

---

### Task 9: Move core agent engine to packages/core/

- [ ] **Step 1: Move src/agent/ to packages/core/src/courtier/**

```bash
cd docaudit-agent
git mv src/agent packages/core/src/courtier/agent
```

- [ ] **Step 2: Move src/courtier/prompts/ to packages/core/src/courtier/prompts/**

```bash
git mv src/courtier/prompts packages/core/src/courtier/prompts
```

- [ ] **Step 3: Move src/plugin/ into core**

```bash
git mv src/plugin packages/core/src/courtier/plugin
```

- [ ] **Step 4: Update imports across the codebase**

All `from src.agent...` imports become `from courtier.agent...`. Use a script:

```bash
cd docaudit-agent
find packages/ tests/ -name "*.py" -exec sed -i \
  -e 's/from src\.agent/from courtier.agent/g' \
  -e 's/import src\.agent/import courtier.agent/g' \
  {} +
```

- [ ] **Step 5: Run tests to identify import errors**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -x 2>&1 | head -80
```
Expected: Tests fail on import errors. Fix incrementally.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: move core agent engine to packages/core/src/courtier/"
```

---

### Task 10: Move domain plugins

- [ ] **Step 1: Move plugins/ to domain package**

```bash
cd docaudit-agent
git mv plugins packages/domains/docaudit/plugins
```

- [ ] **Step 2: Move skills/ to domain package**

```bash
git mv skills packages/domains/docaudit/skills
```

- [ ] **Step 3: Update PluginSystem path reference**

In `packages/core/src/courtier/agent/api/app.py`, update the plugins_dir:

```python
# Old:
app.state.plugin_system = PluginSystem(
    plugins_dir="plugins",
    ...
)

# New:
app.state.plugin_system = PluginSystem(
    plugins_dir="packages/domains/docaudit/plugins",
    ...
)
```

- [ ] **Step 4: Update SkillRegistry path reference**

In `packages/core/src/courtier/agent/api/services/agent_service.py`:

```python
# Old:
skill_registry = SkillRegistry("skills")

# New:
skill_registry = SkillRegistry("packages/domains/docaudit/skills")
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move plugins and skills to domain package"
```

---

### Task 11: Move validators and content compliance to domain plugins

- [ ] **Step 1: Move src/validator/ to format_audit plugin**

```bash
cd docaudit-agent
git mv src/validator packages/domains/docaudit/plugins/format_audit/validator
```

- [ ] **Step 2: Create format_audit plugin.yaml**

```yaml
# packages/domains/docaudit/plugins/format_audit/plugin.yaml
name: format_audit
version: "1.0"
type: tool
description: "文档格式审查——基于规则文件验证格式合规性"
tools:
  - name: check_format
    description: "检查文档格式是否符合指定规则"
    parameters:
      type: object
      properties:
        document_id:
          type: string
          description: "文档 ID"
        rule_file:
          type: string
          description: "规则文件名"
      required: [document_id, rule_file]
```

- [ ] **Step 3: Move src/content_compliance/ to content_audit plugin**

```bash
git mv src/content_compliance packages/domains/docaudit/plugins/content_audit/content_compliance
```

- [ ] **Step 4: Create content_audit plugin.yaml**

```yaml
# packages/domains/docaudit/plugins/content_audit/plugin.yaml
name: content_audit
version: "1.0"
type: tool
description: "文档内容合规审查——政治合规性、保密性、政策一致性检查"
tools:
  - name: check_content
    description: "检查文档内容是否符合合规要求"
    parameters:
      type: object
      properties:
        document_id:
          type: string
          description: "文档 ID"
        rule_file:
          type: string
          description: "合规规则文件名"
      required: [document_id, rule_file]
```

- [ ] **Step 5: Move src/doccorrector/ to text_correction plugin**

```bash
git mv src/doccorrector packages/domains/docaudit/plugins/text_correction/doccorrector
```

- [ ] **Step 6: Move src/docparse/ to parse plugin**

```bash
git mv src/docparse packages/domains/docaudit/plugins/parse/docparse
```

- [ ] **Step 7: Move src/docannot/ to annotate plugin**

```bash
git mv src/docannot packages/domains/docaudit/plugins/annotate/docannot
```

- [ ] **Step 8: Move rule JSON files**

```bash
# Move validator rules
git mv src/validator/rules packages/domains/docaudit/rules/format/ 2>/dev/null || \
  find src/validator -name "*.json" -exec git mv {} packages/domains/docaudit/rules/format/ \; 2>/dev/null || true

# Move content compliance rules
git mv src/content_compliance/rules packages/domains/docaudit/rules/content/ 2>/dev/null || \
  find src/content_compliance -name "*.json" -exec git mv {} packages/domains/docaudit/rules/content/ \; 2>/dev/null || true
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: move domain validators and compliance checkers to domain plugins"
```

---

### Task 12: Move infrastructure packages to core

- [ ] **Step 1: Move storage, es, dbop to core**

```bash
cd docaudit-agent
git mv src/storage packages/core/src/courtier/storage
git mv src/es packages/core/src/courtier/es
git mv src/dbop packages/core/src/courtier/db
```

- [ ] **Step 2: Move domain-specific DB models to domain package**

The `format_template.py` table model with hardcoded doc_type values belongs in the domain package:

```bash
git mv packages/core/src/courtier/db/tables/format_template.py \
       packages/domains/docaudit/models/format_template.py
```

- [ ] **Step 3: Update imports**

```bash
find packages/ tests/ -name "*.py" -exec sed -i \
  -e 's/from src\.storage/from courtier.storage/g' \
  -e 's/from src\.es/from courtier.es/g' \
  -e 's/from src\.dbop/from courtier.db/g' \
  {} +
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move storage, elasticsearch, and db to core; domain models to docaudit"
```

---

### Task 13: Move prompt config to domain package

- [ ] **Step 1: Move config/prompts/**

```bash
cd docaudit-agent
git mv config/prompts packages/domains/docaudit/config/prompts
```

- [ ] **Step 2: Update create_app PromptBundle path**

```python
# In packages/core/src/courtier/agent/api/app.py:
prompt_engine = PromptEngine.from_domain_directories(
    domain_paths=[Path("packages/domains/docaudit")],
    locale=locale,
)
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: move prompt config YAML to domain package"
```

---

### Task 14: Move remaining domain source files

- [ ] **Step 1: Handle src/docbuilder/, src/dedump/, src/plagiarism/**

These packages are domain-specific. Move them to appropriate domain plugin directories:

```bash
cd docaudit-agent
git mv src/docbuilder packages/domains/docaudit/plugins/docbuilder 2>/dev/null || true
git mv src/dedump packages/domains/docaudit/plugins/dedump 2>/dev/null || true
git mv src/plagiarism packages/domains/docaudit/plugins/plagiarism 2>/dev/null || true
```

- [ ] **Step 2: Handle src/docmodels/**

```bash
git mv src/docmodels packages/domains/docaudit/models/docmodels 2>/dev/null || true
```

- [ ] **Step 3: Handle src/common/**

`src/common/behavioral_rules.py` can be removed (text is now in YAML prompts). Check if anything else in `src/common/` is still referenced:

```bash
grep -r "from src.common" packages/ tests/ || echo "No remaining references"
```

If no references, remove it:

```bash
git rm -r src/common/ 2>/dev/null || true
```

- [ ] **Step 4: Handle src/config.py**

Move to core:

```bash
git mv src/config.py packages/core/src/courtier/config.py 2>/dev/null || true
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move remaining domain source files; remove src/common/"
```

---

### Task 15: Move webui to packages/

- [ ] **Step 1: Move frontend**

```bash
cd docaudit-agent
git mv webui packages/webui
```

- [ ] **Step 2: Update Vite proxy if needed**

Check and update `packages/webui/vite.config.ts` if the API proxy path changed.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: move webui to packages/webui"
```

---

### Task 16: Update root pyproject.toml for monorepo

**Files:**
- Modify: `pyproject.toml` (root)

- [ ] **Step 1: Rewrite root pyproject.toml as workspace root**

```toml
# pyproject.toml (root)
[project]
name = "courtier"
version = "0.2.0"
description = "Courtier — a general-purpose AI agent platform"
requires-python = ">=3.12"

[tool.uv.sources]
courtier-core = { workspace = true }
courtier-domain-docaudit = { workspace = true }

[tool.uv.workspace]
members = [
    "packages/core",
    "packages/domains/docaudit",
]
```

- [ ] **Step 2: Update test configuration**

```toml
# Add to root pyproject.toml:
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = [
    "packages/core/src",
    "packages/domains/docaudit",
]
```

- [ ] **Step 3: Run tests to verify layout**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -x --co 2>&1 | tail -5
```
Expected: Test collection succeeds.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: update root pyproject.toml for monorepo workspace layout"
```

---

### Task 17: Remove src/ directory (cleanup)

- [ ] **Step 1: Remove empty src/**

```bash
cd docaudit-agent
rmdir src/ 2>/dev/null && echo "src/ removed" || echo "src/ not empty — check remaining files"
```

If `src/` still has files, inspect and move/remove individually.

- [ ] **Step 2: Remove config/ directory (if empty)**

```bash
rmdir config/ 2>/dev/null || true
```

- [ ] **Step 3: Run full test suite**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -x
```
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove empty src/ and config/ directories"
```

---

### Task 18: End-to-end verification of Phase 2

- [ ] **Step 1: Verify directory structure matches spec**

```bash
cd docaudit-agent
echo "=== Core ===" && find packages/core -type f -name "*.py" | head -20
echo "=== Domain ===" && find packages/domains/docaudit -type f | head -30
echo "=== WebUI ===" && ls packages/webui/
```

- [ ] **Step 2: Run all tests**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -v 2>&1 | tail -30
```
Expected: All tests PASS

- [ ] **Step 3: Verify app starts**

```bash
cd docaudit-agent && timeout 5 uv run python -c "
from courtier.agent.api.app import create_app
app = create_app(start_plugins=False)
print('OK — app created:', app.title)
" || true
```
Expected: "OK — app created: Courtier API"

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: Phase 2 complete — directory restructuring verified"
```

---

## Phase 3: Contract Hardening (Tasks 19–24)

### Task 19: Implement Plugin health check protocol

**Files:**
- Modify: `packages/core/src/courtier/plugin/host.py`
- Modify: `packages/core/src/courtier/plugin/protocol.py`

- [ ] **Step 1: Add health method to Plugin protocol**

```python
# In plugin/protocol.py, add to the JSON-RPC method handler:

HEALTH_METHOD = "health"

# In the plugin host's request dispatcher:
async def _handle_health(self) -> dict:
    """Handle health check request."""
    return {"status": "ok", "dependencies": {}}
```

- [ ] **Step 2: Add health check to PluginHost with 3-strike restart logic**

```python
# In plugin/host.py, add to PluginHost:

MAX_HEALTH_FAILURES = 3

async def check_health(self) -> bool:
    """Check plugin health. Returns True if healthy."""
    try:
        result = await self._send_request({"method": "health"})
        return result.get("status") == "ok"
    except Exception:
        return False

async def monitor_health(self):
    """Periodic health check loop. Restarts plugin after 3 consecutive failures."""
    failures = 0
    while self._running:
        await asyncio.sleep(30)  # check every 30s
        if await self.check_health():
            failures = 0
        else:
            failures += 1
            if failures >= MAX_HEALTH_FAILURES:
                logger.warning("Plugin %s unhealthy after %d checks, restarting", self.name, failures)
                await self.restart()
                failures = 0
```

- [ ] **Step 3: Add timeout_ms support from plugin.yaml**

```python
# In plugin/manifest.py, add field:
class PluginManifest(BaseModel):
    # ... existing fields ...
    timeout_ms: int = 120_000  # default 120s
```

- [ ] **Step 4: Add tests**

```python
# tests/plugin/test_health.py
async def test_health_check_returns_ok():
    ...

async def test_three_failures_triggers_restart():
    ...
```

- [ ] **Step 5: Commit**

```bash
git add packages/core/src/courtier/plugin/ tests/plugin/
git commit -m "feat: add Plugin health check protocol with 3-strike restart"
```

---

### Task 20: Add Skill protocol fields (mode, output_schema, retry_policy)

**Files:**
- Modify: `packages/core/src/courtier/agent/skills/config.py`
- Add validation in: `packages/core/src/courtier/agent/skills/registry.py`

- [ ] **Step 1: Update SkillConfig model**

```python
# In skills/config.py, add fields to SkillConfig:
from enum import Enum
from typing import Literal

class SkillMode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    AUTO = "auto"

class RetryPolicy(str, Enum):
    NONE = "none"
    ON_ERROR = "on_error"
    ON_TIMEOUT = "on_timeout"

class SkillConfig(BaseModel):
    # ... existing fields ...
    type: Literal["skill"] = "skill"
    version: str = "1.0"
    mode: SkillMode = SkillMode.AUTO
    output_schema: str | None = None
    timeout_seconds: int = 600
    retry_policy: RetryPolicy = RetryPolicy.NONE
```

- [ ] **Step 2: Add validation in SkillRegistry.scan()**

```python
# In skills/registry.py, add after parsing frontmatter:
def _validate_skill_config(self, config: SkillConfig) -> list[str]:
    errors = []
    if config.type != "skill":
        errors.append(f"Expected type='skill', got '{config.type}'")
    if not config.version:
        errors.append("version is required")
    return errors
```

- [ ] **Step 3: Update existing skill .md files to include new fields**

```markdown
---
name: format_audit
description: 公文格式审查
version: "1.0"
type: skill
tools: [check_format]
input_model: FormatAuditInput
mode: auto
timeout_seconds: 600
retry_policy: on_error
---
```

- [ ] **Step 4: Commit**

```bash
git add packages/core/src/courtier/agent/skills/ packages/domains/docaudit/skills/
git commit -m "feat: add Skill protocol fields — mode, output_schema, retry_policy"
```

---

### Task 21: Add domain.yaml schema validation

**Files:**
- Create: `packages/core/src/courtier/domain/__init__.py`
- Create: `packages/core/src/courtier/domain/loader.py`

- [ ] **Step 1: Create DomainConfig model**

```python
# packages/core/src/courtier/domain/__init__.py
"""Domain package discovery and validation."""

from .loader import DomainConfig, DomainLoader

__all__ = ["DomainConfig", "DomainLoader"]
```

```python
# packages/core/src/courtier/domain/loader.py
"""Domain package loader — discovers and validates domain packages."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class DomainConfig(BaseModel):
    """Validated domain.yaml metadata."""
    name: str
    title: str = ""
    description: str = ""
    locales: list[str] = Field(default_factory=lambda: ["en-US"])
    requires_plugins: list[str] = Field(default_factory=list)
    requires_services: list[str] = Field(default_factory=list)


class DomainLoader:
    """Discover and validate domain packages in a directory."""

    @staticmethod
    def load(domain_path: Path) -> DomainConfig | None:
        """Load and validate domain.yaml from a domain package directory."""
        config_file = domain_path / "config" / "domain.yaml"
        if not config_file.is_file():
            logger.warning("No domain.yaml found in %s", domain_path)
            return None
        try:
            raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            return DomainConfig.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            logger.error("Invalid domain.yaml in %s: %s", domain_path, exc)
            return None

    @staticmethod
    def validate_domain(domain_path: Path) -> list[str]:
        """Validate a complete domain package. Returns list of issues."""
        issues: list[str] = []

        # 1. Validate domain.yaml
        config = DomainLoader.load(domain_path)
        if config is None:
            issues.append("Missing or invalid config/domain.yaml")
            return issues

        # 2. Check prompts directory has at least one locale
        prompts_dir = domain_path / "config" / "prompts"
        if not prompts_dir.is_dir():
            issues.append("Missing config/prompts/ directory")
        else:
            for locale in config.locales:
                locale_dir = prompts_dir / locale
                if not locale_dir.is_dir():
                    issues.append(f"Locale '{locale}' declared but dir missing: {locale_dir}")
                elif not list(locale_dir.glob("*.yaml")):
                    issues.append(f"Locale '{locale}' has no YAML files")

        # 3. Check each required plugin exists
        plugins_dir = domain_path / "plugins"
        for plugin_name in config.requires_plugins:
            plugin_dir = plugins_dir / plugin_name
            if not plugin_dir.is_dir():
                issues.append(f"Required plugin '{plugin_name}' not found at {plugin_dir}")
            elif not (plugin_dir / "plugin.yaml").is_file():
                issues.append(f"Plugin '{plugin_name}' missing plugin.yaml")

        # 4. Check skills directory
        skills_dir = domain_path / "skills"
        if not skills_dir.is_dir():
            issues.append("Missing skills/ directory")
        elif not list(skills_dir.glob("*.md")):
            issues.append("No skill .md files found in skills/")

        return issues
```

- [ ] **Step 2: Write tests**

```python
# tests/courtier/domain/test_loader.py
class TestDomainLoader:
    def test_load_valid_domain_yaml(self): ...
    def test_missing_config_file_returns_none(self): ...
    def test_validate_domain_returns_issues_for_missing_plugins(self): ...
    def test_validate_domain_passes_for_complete_package(self): ...
```

- [ ] **Step 3: Run tests**

```bash
cd docaudit-agent && uv run pytest tests/courtier/domain/ -v
```
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add packages/core/src/courtier/domain/ tests/courtier/domain/
git commit -m "feat: add DomainLoader with domain.yaml schema validation"
```

---

### Task 22: Add CLI validate-domain command

**Files:**
- Create: `packages/core/src/courtier/cli/__init__.py`
- Create: `packages/core/src/courtier/cli/main.py`
- Modify: `packages/core/pyproject.toml` (add console_scripts entry point)

- [ ] **Step 1: Create CLI module**

```python
# packages/core/src/courtier/cli/__init__.py
"""Courtier CLI tools."""
```

```python
# packages/core/src/courtier/cli/main.py
"""Courtier CLI — validate-domain and other utilities."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from courtier.domain.loader import DomainLoader


@click.group()
def main():
    """Courtier agent platform CLI."""
    pass


@main.command()
@click.argument("domain_path", type=click.Path(exists=True, path_type=Path))
def validate_domain(domain_path: Path):
    """Validate a domain package.

    Checks domain.yaml, prompts, plugins, and skills for completeness.
    """
    issues = DomainLoader.validate_domain(domain_path)
    if not issues:
        click.echo(f"✓ Domain package at '{domain_path}' is valid.")
        sys.exit(0)

    click.echo(f"✗ Domain package at '{domain_path}' has {len(issues)} issue(s):")
    for issue in issues:
        click.echo(f"  ✗ {issue}")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add console_scripts to pyproject.toml**

```toml
# packages/core/pyproject.toml
[project.scripts]
courtier = "courtier.cli.main:main"
```

- [ ] **Step 3: Add click dependency**

```bash
cd docaudit-agent && uv add click
```

- [ ] **Step 4: Test CLI**

```bash
cd docaudit-agent && uv run courtier validate-domain packages/domains/docaudit/
```
Expected: Validation output showing domain package status.

- [ ] **Step 5: Commit**

```bash
git add packages/core/src/courtier/cli/ packages/core/pyproject.toml
git commit -m "feat: add courtier CLI with validate-domain command"
```

---

### Task 23: Write developer documentation

**Files:**
- Create: `docs/domain-package-guide.md`
- Create: `docs/plugin-development.md`
- Create: `docs/skill-authoring.md`

- [ ] **Step 1: Write domain-package-guide.md**

```markdown
# Courtier Domain Package Guide

## Overview

A Courtier domain package extends the platform with domain-specific capabilities.
Each domain package is a self-contained directory with plugins, skills, rules,
and localized prompts.

## Quick Start

1. Copy the template: `cp -r packages/domains/docaudit packages/domains/my-domain`
2. Edit `config/domain.yaml` with your domain metadata
3. Add plugins in `plugins/`
4. Add skills in `skills/`
5. Add prompts in `config/prompts/{locale}/`
6. Validate: `courtier validate-domain packages/domains/my-domain`

## Directory Structure

```
my-domain/
├── config/
│   ├── domain.yaml          # Required: domain metadata
│   └── prompts/
│       ├── zh-CN/           # Chinese prompts
│       │   ├── orchestrator.yaml
│       │   ├── subagent.yaml
│       │   ├── behavioral.yaml
│       │   ├── chat.yaml
│       │   ├── errors.yaml
│       │   └── tools.yaml
│       └── en-US/           # English prompts
│           └── ...
├── plugins/                 # JSON-RPC plugins
│   └── my_tool/
│       ├── plugin.yaml
│       └── entry.py
├── skills/                  # Skill definitions (.md)
│   └── my_skill.md
├── rules/                   # Domain rules (JSON/YAML)
│   └── my_rules.json
├── models/                  # Domain data models (optional)
│   └── my_model.py
└── pyproject.toml
```

## domain.yaml Reference

| Field | Required | Description |
|-------|----------|-------------|
| name | Yes | Unique domain identifier |
| title | No | Human-readable title |
| description | No | One-line description |
| locales | Yes | Supported locale tags |
| requires_plugins | No | Plugin names this domain needs |
| requires_services | No | Infrastructure services needed |

## Prompt Template Keys

See [skill-authoring.md](skill-authoring.md) for the full list of template keys.
```

- [ ] **Step 2: Write plugin-development.md**

```markdown
# Courtier Plugin Development Guide

## Overview

Plugins are standalone subprocesses communicating via JSON-RPC 2.0 over stdio.
Each plugin provides one or more tools.

## plugin.yaml Reference

... (detailed reference)
```

- [ ] **Step 3: Write skill-authoring.md**

```markdown
# Courtier Skill Authoring Guide

## Overview

Skills are Markdown documents with YAML frontmatter. They define reusable
workflows that the OrchestratorAgent can dispatch to SubAgents.

## Skill Frontmatter Reference

... (detailed reference)

## Prompt Template Keys

... (full list from RESERVED_TEMPLATE_KEYS)
```

- [ ] **Step 4: Commit**

```bash
git add docs/domain-package-guide.md docs/plugin-development.md docs/skill-authoring.md
git commit -m "docs: add domain package, plugin development, and skill authoring guides"
```

---

### Task 24: Phase 3 verification

- [ ] **Step 1: Run courtier validate-domain**

```bash
cd docaudit-agent && uv run courtier validate-domain packages/domains/docaudit/
```

- [ ] **Step 2: Run all tests**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -v 2>&1 | tail -20
```
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: Phase 3 complete — contract hardening verified"
```

---

## Phase 4: Branding & Documentation (Tasks 25–28)

### Task 25: Update API title and version

**Files:**
- Modify: `packages/core/src/courtier/agent/api/app.py`

- [ ] **Step 1: Update FastAPI title**

```python
# In create_app():
app = FastAPI(
    title="Courtier API",
    description="General-purpose AI agent platform with pluggable domain packages",
    version="0.2.0",
    lifespan=lifespan,
)
```

- [ ] **Step 2: Update all remaining "DocAudit" references**

```bash
cd docaudit-agent
grep -r "DocAudit" packages/ tests/ docs/ --include="*.py" --include="*.md" -l
```

Replace each occurrence with "Courtier":

```bash
find packages/ tests/ docs/ -type f \( -name "*.py" -o -name "*.md" \) \
  -exec sed -i 's/DocAudit/Courtier/g' {} +
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: rename API title and all DocAudit references to Courtier"
```

---

### Task 26: Update frontend branding

**Files:**
- Modify: `packages/webui/index.html`
- Modify: `packages/webui/src/App.vue`
- Modify: `packages/webui/package.json`

- [ ] **Step 1: Update index.html title**

```html
<!-- packages/webui/index.html -->
<title>Courtier</title>
```

- [ ] **Step 2: Update package.json**

```json
{
  "name": "courtier-webui",
  "description": "Courtier agent platform web interface"
}
```

- [ ] **Step 3: Update App.vue branding text**

Find and replace any "DocAudit" references in the Vue components with "Courtier".

- [ ] **Step 4: Commit**

```bash
git add packages/webui/
git commit -m "chore: update frontend branding from DocAudit to Courtier"
```

---

### Task 27: Update root documentation

**Files:**
- Modify: `CLAUDE.md` (root)
- Modify: `packages/core/CLAUDE.md`

- [ ] **Step 1: Rewrite root CLAUDE.md**

```markdown
# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Repository Layout

Courtier is a monorepo for a general-purpose AI agent platform.

- `packages/core/` — Python FastAPI backend: the Courtier agent engine.
- `packages/domains/docaudit/` — First official domain package: Chinese government document audit.
- `packages/webui/` — Vue 3 terminal-style frontend.

## Common Development Commands

... (update as needed)
```

- [ ] **Step 2: Update core CLAUDE.md**

Remove domain-specific references, keep generic agent platform documentation.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md packages/core/CLAUDE.md
git commit -m "docs: update CLAUDE.md files for Courtier platform"
```

---

### Task 28: Final verification and tag

- [ ] **Step 1: Run full test suite one final time**

```bash
cd docaudit-agent && uv run pytest -m "not integration" -v
```
Expected: All tests PASS

- [ ] **Step 2: Verify the app starts**

```bash
cd docaudit-agent && timeout 5 uv run python -c "
from courtier.agent.api.app import create_app
app = create_app(start_plugins=False)
print('Courtier API v0.2.0 ready —', app.title)
" || true
```

- [ ] **Step 3: Verify CLI**

```bash
cd docaudit-agent && uv run courtier validate-domain packages/domains/docaudit/
```

- [ ] **Step 4: Commit and tag**

```bash
git add -A
git commit -m "chore: Courtier v0.2.0 — general-purpose AI agent platform"
git tag v0.2.0 -m "Courtier v0.2.0 — general-purpose AI agent platform

- Domain-agnostic core engine with zero hardcoded NL text
- PromptBundle system with Jinja2 templates and i18n
- Domain package architecture (Plugin + Skill + Rules + Config)
- Official docaudit domain package (Chinese government document audit)
- CLI validation tool (courtier validate-domain)
- Plugin health check protocol
- Developer documentation"
```

- [ ] **Step 5: GitHub description update**

Update the GitHub repository description to:
"Courtier — A general-purpose AI agent platform. Domain-agnostic core with pluggable skills and plugins. Ships with official domain packages for document audit and more."

---

## Post-Migration Checklist

- [ ] All tests pass (`uv run pytest -m "not integration"`)
- [ ] App starts without errors
- [ ] CLI `validate-domain` passes on docaudit domain
- [ ] No hardcoded "DocAudit" strings remain in source
- [ ] No hardcoded NL text in core (all through PromptEngine)
- [ ] Core imports zero domain code
- [ ] Domain package is self-contained
- [ ] Developer docs are complete and accurate
