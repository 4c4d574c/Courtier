# Skill / SubAgent / Tool 重新组织设计文档

**日期**: 2026-06-29  
**状态**: 待实现  
**优先级**: A（人的可写性）> D（LLM 可解析）> B（代码清晰）> C（热更新）

---

## 1. 背景与动机

当前 DocAudit 的能力扩展层存在以下问题：

1. **概念混杂**：`src/agent/skills/` 已移除，但代码中仍有 `ToolInfo.skill` 等遗留字段；插件中同时存在 `type: tool` 和 `type: agent`，两者关系不清晰。
2. **子代理发现不直观**：插件声明的 `format_auditor` 到 OrchestratorAgent 中变成 `run_format_auditor`，这个 `run_` 前缀是隐式约定。
3. **插件职责过重**：插件既提供原子工具，又定义子代理，导致插件目录和业务逻辑耦合。
4. **业务专家无法参与**：新增或调整审核流程需要修改 Python 代码和插件 manifest，非开发人员难以参与。

本设计重新组织三层抽象：

- **Tool**：最底层原子能力
- **SubAgent**：通用 Agent 形式，根据配置自主编排工具
- **Skill**：通过 `.md` 文档定义 SubAgent 的配置和任务流程

---

## 2. 核心目标

按优先级排序：

1. **A. 业务专家可通过 `.md` 定义流程**（最高优先级）
2. **D. LLM 能按 `.md` 自动编排工具**
3. **B. 代码结构清晰**
4. **C. 支持动态热更新**（最低优先级，本阶段不接受热更新）

---

## 3. 术语定义

| 术语 | 含义 | 示例 |
|------|------|------|
| **Tool** | 单次调用的原子能力 | `parse_document`、`audit_format` |
| **Skill** | 定义 SubAgent 的 Markdown 文档 | `skills/format_audit.md` |
| **SubAgent** | 执行 Skill 的通用 Agent | 由 `load_skill` 创建并运行的 `Agent` 实例 |
| **SkillConfig** | 启动时从 Skill 文档预编译出的配置对象 | `SkillConfig(name="format_audit", ...)` |
| **SkillRegistry** | 维护所有 `SkillConfig` 的注册表 | 启动时扫描 `skills/` 生成 |
| **Plugin** | 提供原子工具的子进程 | `plugins/common/parse/`、`plugins/audit/format_audit/` |

---

## 4. 总体架构

```text
┌─────────────────────────────────────────────┐
│           OrchestratorAgent                 │
│  SYSTEM prompt 包含可用 Skill 目录           │
│  工具列表：插件工具 + load_skill + 内置工具  │
└──────────────────┬──────────────────────────┘
                   │ 调用 load_skill(skill, task, ...)
                   ▼
┌─────────────────────────────────────────────┐
│           LoadSkillTool                     │
│  1. 从 SkillRegistry 取 SkillConfig         │
│  2. 检查嵌套深度（最大 3 层）               │
│  3. 从 ToolRegistry 取声明的工具            │
│  4. 校验/构建 input_model                   │
│  5. 创建通用 Agent（SubAgent）              │
│  6. 执行并返回 ToolResult                   │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│           SubAgent (通用 Agent)             │
│  role = Skill.md 正文                        │
│  tools = Skill frontmatter 声明的工具        │
│  执行标准 agent_loop                         │
└──────────────────┬──────────────────────────┘
                   │ 调用工具
                   ▼
┌─────────────────────────────────────────────┐
│           插件工具 / 内置工具                │
└─────────────────────────────────────────────┘
```

---

## 5. 目录结构

### 5.1 后端目录

```text
docaudit-agent/
├── skills/                              # Skill 定义目录
│   ├── __init__.py
│   ├── format_audit.md
│   ├── content_audit.md
│   ├── style_audit.md
│   ├── text_correction.md
│   ├── plagiarism.md
│   ├── full_government_audit.md
│   └── schemas/                         # Skill 输入模型
│       ├── __init__.py
│       ├── format_audit.py
│       ├── content_audit.py
│       ├── style_audit.py
│       ├── text_correction.py
│       └── plagiarism.py
│
├── plugins/                             # 原子工具目录
│   ├── common/                          # 通用能力
│   │   ├── parse/
│   │   ├── search/
│   │   ├── annotate/
│   │   └── template/
│   └── audit/                           # 业务能力
│       ├── format_audit/
│       ├── content_audit/
│       ├── style_audit/
│       ├── text_correction/
│       └── plagiarism/
│
└── src/agent/
    ├── skills/                          # 已移除，不再使用
    │   ...
    └── ...
```

### 5.2 Skill 文件示例

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
tags: [audit, government]
enabled: true
---

# 目标
你是政府公文格式审核专家。收到任务后，按以下流程执行审核。

# 适用场景
- 文种为通知、报告、请示、函
- 文件类型为 DOCX 或 PDF

# 工具说明
- `parse_document`: 解析文档，提取段落、标题、表格等结构
- `detect_document_type`: 根据标题和正文判断公文文种
- `audit_format`: 按 GB/T 9704-2012 检查格式合规性

# 执行流程
1. 调用 `parse_document` 解析输入文档
2. 调用 `detect_document_type` 确定文种
3. 调用 `audit_format` 检查格式
4. 汇总所有违规项，返回 JSON 结果

# 输出格式
```json
{
  "violations": [
    {"rule": "标题字号不符", "position": "...", "message": "..."}
  ],
  "summary": "..."
}
```
```

---

## 6. 数据模型

### 6.1 SkillConfig

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class SkillConfig:
    """预编译后的 Skill 配置。"""

    name: str                          # 调用名
    description: str                   # 目录描述
    source_path: Path                  # 源文件路径
    system_prompt: str                 # Markdown 正文
    tools: tuple[str, ...]             # 可用工具名列表
    input_model: type[BaseModel] | None
    output_artifact_type: str | None
    tags: tuple[str, ...]
    enabled: bool
    raw_frontmatter: dict[str, Any]    # 保留原始 frontmatter
```

### 6.2 SkillRegistry

```python
class SkillRegistry:
    """启动时扫描 skills/ 目录并维护预编译的 Skill 配置。"""

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir
        self._configs: dict[str, SkillConfig] = {}
        self._errors: list[str] = []

    def scan(self) -> None:
        """扫描 skills/ 目录，解析所有 .md 文件。"""

    def get(self, name: str) -> SkillConfig | None:
        """按名称获取 SkillConfig。"""

    def list_enabled(self) -> list[SkillConfig]:
        """返回所有启用的 Skill。"""

    def build_catalog(self) -> str:
        """生成 SYSTEM prompt 中使用的技能目录文本。"""

    @property
    def errors(self) -> list[str]: ...

    @property
    def has_errors(self) -> bool: ...
```

---

## 7. 运行时行为

### 7.1 启动流程

1. 创建共享 `ToolRegistry`
2. 启动 `PluginSystem`，注册所有原子工具
3. 创建 `SkillRegistry`，扫描 `skills/`
4. 解析每个 `.md` 文件，生成 `SkillConfig`
5. 校验 `input_model` 可导入、`name` 唯一等
6. 把 `SkillRegistry` 注入 `OrchestratorAgent`
7. `OrchestratorAgent` 在 system prompt 中展示可用 Skill 目录

### 7.2 `load_skill` 工具

```python
class LoadSkillTool:
    name = "load_skill"
    description = (
        "加载并执行一个 Skill。Skill 是预定义的子代理配置，"
        "会根据 SKILL.md 中的说明自主调用工具完成任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill 名称"},
            "task": {"type": "string", "description": "任务描述"}
        },
        "required": ["skill"],
        "additionalProperties": True   # 允许传入 input_model 字段
    }
```
> 注：参数名为 `skill` 而非 `name`，以避免与 `ToolRegistry.execute()` 的 `name` 参数冲突。

### 7.3 执行流程

```text
LLM 调用 load_skill(skill="format_audit", task="审核这份通知", document="...")
        ↓
LoadSkillTool.execute()
        ↓
SkillRegistry.get("format_audit") → SkillConfig
        ↓
从 ToolRegistry 取 tools: [parse_document, detect_document_type, audit_format]
        ↓
如果 input_model 存在：
    input_obj = FormatAuditorInput(task=task, document=document)
否则：
    使用 task 字符串
        ↓
创建 Agent(name="format_audit", role=system_prompt, tools=tools)
        ↓
Agent.run(input=input_obj) 或 Agent.run(task=task)
        ↓
返回 ToolResult(success, data, error, metadata)
```

### 7.4 结果提取

`LoadSkillTool` 返回的结果与当前 `_SubAgentTool` 行为一致：

```python
ToolResult(
    success=result.status == "completed",
    data=_extract_subagent_data(result),   # 提取最后一个成功 tool result 的数据
    error=result.termination_reason if result.status != "completed" else None,
    metadata={"is_subagent_result": True, "skill": name},
)
```

---

## 8. 与现有系统的集成

### 8.1 OrchestratorAgent 的变化

**当前**：
- 通过 `SubAgentRunner` 把插件子代理包装成 `run_xxx` 工具
- system prompt 中写死具体工作流规则

**新设计**：
- 不再包装插件子代理
- 工具列表：插件原子工具 + `load_skill` + 内置工具
- system prompt 只保留通用编排描述：

```text
你是 DocAudit 文档审核编排代理。
你的任务是理解用户需求，选择合适的 Skill 并调用 load_skill 执行。
每个 Skill 都是独立的子代理，会自主完成其负责的部分。

可用 Skill：
- format_audit: 政府公文格式审核
- content_audit: 内容审核
- style_audit: 行文风格审查
- text_correction: 文本纠错
- plagiarism: 文档查重
- full_government_audit: 政府公文完整审核流程
```

### 8.2 插件的变化

- 插件只声明 `type: tool`，不再声明 `type: agent`
- 插件目录重组为 `plugins/common/` 和 `plugins/audit/`
- 现有插件子代理的 `role` 和流程迁移到 `skills/*.md`

### 8.3 默认流程 Skill

当前 OrchestratorAgent 的硬编码工作流规则迁移到 `skills/full_government_audit.md`：

```markdown
---
name: full_government_audit
description: 政府公文完整审核流程
tools:
  - parse_document
  - load_skill
---

# 流程
收到政府公文审核任务后：
1. 调用 parse_document 解析文档
2. 调用 load_skill(skill="format_audit", ...)
3. 调用 load_skill(skill="content_audit", ...)
4. 调用 load_skill(skill="style_audit", ...)
5. 调用 load_skill(skill="text_correction", ...)
6. 调用 load_skill(skill="plagiarism", ...)
7. 汇总结果
```

---

## 9. 错误处理

### 9.1 启动时

| 场景 | 行为 |
|------|------|
| YAML frontmatter 解析失败 | 记录错误，跳过该 Skill |
| `input_model` 导入失败 | 该 Skill `enabled=false` |
| `tools` 中的工具未注册 | 记录警告，不阻塞启动 |
| `name` 重复 | 后加载覆盖先加载，记录警告 |
| `skills/` 目录不存在 | 记录 info，空注册表运行 |

### 9.2 运行时

| 场景 | 行为 |
|------|------|
| Skill 不存在 | `ToolResult(success=False, error="Skill not found")` |
| 工具未注册 | `ToolResult(success=False, error="Skill requires tool ...")` |
| 输入校验失败 | `ToolResult(success=False, error="Invalid input: ...")` |
| 子代理执行失败 | 返回终止原因 |
| 嵌套过深 | 超过最大深度（默认 3）直接返回错误 |

---

## 10. 测试策略

### 10.1 单元测试

- `SkillRegistry.scan()` 解析正确性
- `SkillConfig` 构建
- 启动校验逻辑
- `LoadSkillTool.execute()` 各种成功/失败路径

### 10.2 集成测试

- 完整 Skill 执行流程
- Skill 嵌套调用
- 插件工具 + Skill 协同
- OrchestratorAgent 调度 Skill

### 10.3 测试夹具

```text
tests/agent/skills/
├── test_skill_registry.py
├── test_load_skill_tool.py
└── fixtures/
    ├── valid_skill.md
    ├── missing_frontmatter.md
    ├── bad_yaml.md
    └── nested_skill.md
```

---

## 11. 迁移计划

### Phase 1：新增 Skill 基础设施

- [ ] 创建 `skills/` 目录结构
- [ ] 实现 `SkillConfig` 和 `SkillRegistry`
- [ ] 实现 `LoadSkillTool`
- [ ] 在 `OrchestratorAgent` 中注册 `load_skill` 并注入 Skill 目录
- [ ] 新旧机制并存

### Phase 2：创建核心 Skill 文档

- [ ] 迁移现有插件子代理到 `skills/*.md`
- [ ] 移动 input_model 到 `skills/schemas/`
- [ ] 创建 `skills/full_government_audit.md`

### Phase 3：清理插件 agent 声明

- [ ] 从插件 `plugin.yaml` 中移除 `type: agent`
- [ ] 清理 `SubAgentRunner` 中包装插件子代理的代码
- [ ] 移除 `ExtensionRegistry` 中不再使用的 agent 注册逻辑

### Phase 4：简化 OrchestratorAgent

- [ ] 移除硬编码工作流规则
- [ ] 简化 system prompt 为通用编排描述
- [ ] 更新测试和文档

---

## 12. 未解决问题与未来工作

1. **热更新**：本阶段 Skill 启动后固定。未来如需热更新，需设计文件监听和注册表刷新机制。
2. **Skill 版本控制**：是否需要版本字段？当前未设计。
3. **远程 Skill**：当前 Skill 在本地执行。未来如需插件隔离，可扩展 `context: fork` 模式。
4. **Skill 组合**：高层 Skill 通过 `load_skill` 调用低层 Skill，当前通过 LLM 编排，未来可考虑结构化工作流。
5. **权限控制**：SubAgent 可以访问声明的工具，但没有更细粒度的权限限制，未来可扩展。

---

## 13. 相关源码

- `src/plugin/proxies.py`: `ProxyTool`
- `src/agent/agents/base.py`: `Agent`
- `src/agent/core/loop.py`: `agent_loop`
- `src/agent/tools/registry.py`: `ToolRegistry`
- `src/agent/agents/orch.py`: `OrchestratorAgent`
- `src/plugin/scanner.py`: 插件扫描参考
