# Plugin、Tool、SubAgent 与 Skill 概念边界

> 本文档说明 DocAudit 能力扩展层中的核心概念及其关系。
> 架构在 2026-06 重新组织为 **Tool / SubAgent / Skill** 三层抽象，业务审核流程由 `skills/*.md` 文档定义，
> 通过 `AgentRuntime` 以第一离子代理方式调起执行。设计依据见
> `docs/superpowers/specs/2026-06-29-skill-subagent-redesign-design.md`。

## 三层抽象速览

```text
┌─────────────────────────────────────────────┐
│           OrchestratorAgent                  │
│  SYSTEM prompt 内嵌可用 Skill 目录            │
│  工具列表：插件工具 + Skill 工具 + 内置工具   │
└──────────────────┬───────────────────────────┘
                   │ format_audit(task=...)
                   ▼
┌─────────────────────────────────────────────┐
│  SkillTool（src/agent/tools/builtin/skill.py）│
│  取 SkillConfig → AgentRuntime.spawn()       │
│  → delegate() 运行子代理 → ExecutionResult   │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  AgentRuntime                                │
│  ├─ 预算/深度/循环检测                        │
│  ├─ 创建 Agent 实例（SubAgent）               │
│  └─ 桥接子代理事件到 SSE（scope: subagent）   │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  SubAgent（通用 Agent 实例）                  │
│  role = Skill.md 正文；tools = frontmatter 声明│
│  执行标准 agent_loop，自主调用工具            │
└──────────────────┬───────────────────────────┘
                   │ 调用工具
                   ▼
┌─────────────────────────────────────────────┐
│  插件工具（plugins/common、plugins/audit）    │
│  + 内置工具（list_artifacts 等）              │
└─────────────────────────────────────────────┘
```

## 核心术语

| 术语 | 含义 | 示例 |
|------|------|------|
| **Tool（工具）** | 单次调用的原子能力，直接注册到 `ToolRegistry` | `parse_document`、`audit_format` |
| **Plugin（插件）** | 独立运行的 TCP 服务、通过换行分隔 JSON-RPC 提供 Tool 的能力包 | `plugins/docaudit/audit/check_format/` |
| **SubAgent（子代理）** | 执行某个 Skill 的通用 `Agent` 实例，拥有自己的 Think-Act-Observe 循环 | `AgentRuntime` 创建的 `Agent` |
| **Skill（技能）** | 用 Markdown 文档定义的 SubAgent 配置与任务流程 | `skills/format_audit.md` |
| **SkillConfig** | 启动时由 Skill 文档预编译出的不可变配置对象 | `SkillConfig(name="format_audit", ...)` |
| **SkillRegistry** | 扫描 `skills/` 并维护全部 `SkillConfig` 的注册表 | 启动时调用 `scan()` 预编译 |
| **SkillTool** | 把"执行 Skill"包装成的 `ToolProtocol`，LLM 通过它调用 Skill | 工具名 `format_audit` |
| **AgentRuntime** | 管理 SubAgent 生命周期、预算与事件桥接的运行时 | `spawn()` / `delegate()` / `terminate()` |
| **skill（UI 字段）** | `ToolInfo.skill` / `StepRecord.skill`，标识工具/步骤的展示分类，**与运行时 Skill 无关** | 前端分组标签 |

## 工具（Tool）

**定位**：原子性、单次完成的能力。

**来源**：由插件提供。插件目录在 2026-06 重组为两类：

```text
plugins/
├── common/      # 通用能力：parse、search、annotate、template
└── audit/       # 业务能力：format_audit、content_audit、style_audit、
                 #           text_correction、plagiarism
```

**插件 manifest 只声明工具**（不再声明 `type: agent`）：

```yaml
# plugins/audit/format_audit/plugin.yaml
name: format_audit
version: "1.0.0"
api: "1.0"
capabilities:
  tools:
    - name: detect_document_type
      display_name: 检测文档类型
      description: "Detect Chinese government document type ..."
    - name: audit_format
      display_name: 格式审计
      description: "Audit document formatting compliance ..."
```

**注册与调用路径**：

```text
plugin.yaml (capabilities.tools)
        ↓  PluginScanner 扫描（支持 plugins/X/ 与 plugins/{common,audit}/X/ 两级布局）
ProxyTool
        ↓
ToolRegistry.register(proxy)
        ↓
LLM → audit_format
  └── ProxyTool.execute()
        └── JSON-RPC tool.execute → plugin 服务 handler → ToolResult
```

**适用场景**：输入输出明确、无需多轮推理的任务，例如解析文档、检测文种、加载模板、查重计算。

## 子代理（SubAgent）

**定位**：执行一个 Skill 的通用 `Agent`。

**关键特征**：SubAgent 没有专门的子类，就是 `src/agent/agents/base.py` 中的通用 `Agent`。`AgentRuntime`
按 `SkillConfig` 动态组装它：

- `role` = Skill 文档正文（system prompt）
- `tools` = Skill frontmatter `tools:` 中声明、并已在 `ToolRegistry` 注册的工具
- 执行标准 `agent_loop`，自主决定调用哪些工具、如何汇总结果

**嵌套深度与预算**：`AgentRuntime.spawn()` 通过 `AgentHandle` 与 `AgentRuntimeBudget` 跟踪嵌套层数、
累计调用次数和运行时间，防止 Skill 互相递归调用导致失控。

## 技能（Skill）

**定位**：业务专家可读写的流程定义，让"新增/调整审核流程"无需改 Python 代码。

**文件结构**：

```text
skills/
├── format_audit.md          # name、description、tools、input_model 等在 YAML frontmatter
├── content_audit.md         # 内容合规审核与文本纠错（text_correction 已并入）
├── style_audit.md
├── plagiarism.md
├── full_government_audit.md  # 编排型 Skill，可调用其它 Skill 工具完成完整审核
└── schemas/                  # 各 Skill 的 Pydantic 输入模型
    ├── format_audit.py        (FormatAuditorInput)
    ├── content_audit.py       (ContentAuditorInput)
    ├── style_audit.py         (StyleAuditorInput)
    └── plagiarism.py          (PlagiarismAuditorInput)
```

**Skill 文档格式**（frontmatter + 正文）：

```markdown
---
name: format_audit
description: 政府公文格式审核
tools:
  - parse_document
  - detect_document_type
  - audit_format
input_model: skills.schemas.format_audit.FormatAuditorInput
enabled: true
---

# 目标
你是政府公文格式审核专家。收到任务后按以下流程执行……

# 执行流程
1. 调用 parse_document 解析输入文档
2. 调用 detect_document_type 确定文种
3. 调用 audit_format 检查格式
4. 汇总违规项，返回 JSON 结果
```

**生命周期**：

```text
启动：SkillRegistry.scan() 读取 skills/*.md → 解析 frontmatter → 预编译为 SkillConfig
      （frontmatter 错误或缺失的文件被记录到 registry.errors，不影响其它 Skill）
        ↓
编排：SkillRegistry.build_catalog() 生成 "- name: description" 目录，注入 OrchestratorAgent system prompt
        ↓
注册：OrchestratorAgent 为每个启用的 Skill 创建 SkillTool 并注册到 ToolRegistry
        ↓
调用：LLM → format_audit(task="...")
        └── SkillTool.execute()
              ├── 取 SkillConfig（未找到或 disabled → 失败）
              ├── AgentRuntime.spawn() 校验预算/深度/循环
              ├── 从 ToolRegistry 解析声明的工具（缺失 → 失败）
              ├── 若有 input_model：校验/构建结构化输入
              ├── AgentRuntime.delegate() 运行 SubAgent
              └── 返回 ExecutionResult（metadata 含 is_subagent_result、skill 名）
```

## Tool / SubAgent / Skill 对比

| 维度 | Tool | SubAgent | Skill |
|------|------|----------|-------|
| 定义位置 | 插件 `plugin.yaml` | 无独立定义（由 Skill 动态生成的 `Agent`） | `skills/*.md` |
| 创建方 | `PluginScanner` → `ProxyTool` | `AgentRuntime` 运行时创建 | 启动时 `SkillRegistry` 预编译 `SkillConfig` |
| LLM 看到的名字 | 工具本名（`audit_format`） | 不直接可见（被 SkillTool 包裹） | 通过 `format_audit(task=...)` 调用 |
| 内部实现 | 单次 RPC/函数调用 | 完整 `agent_loop` | = 一个 SubAgent 的配置来源 |
| 可由业务专家编辑 | 否（需开发） | 否 | **是**（改 `.md` 即可） |
| 输出 | `ExecutionResult` | `AgentResult` → 统一为 `ExecutionResult` | 同 SubAgent |

## `skill` 命名歧义澄清

代码里有两处都叫 `skill`，但分属不同层，切勿混淆：

1. **运行时 Skill**（本文主题）：`skills/*.md` + `SkillRegistry` + `AgentRuntime` + `SkillTool`，是真正的能力系统。
2. **UI 展示字段 `skill`**：`ToolInfo.skill` / `StepRecord.skill`（`src/agent/api/models.py`）以及
   `sse_adapter._build_tool_skill_map()`，仅用于前端把工具/步骤按能力类别分组展示，是字符串标签，
   与运行时 Skill 没有关系。

## 当前能力清单

**插件工具**（按目录分组）：

| 目录 | 插件 | 提供的 Tool |
|------|------|------------|
| `common` | `parse` | `parse_document` |
| `common` | `search` | `search_documents` |
| `common` | `annotate` | `annotate_document` |
| `common` | `template` | `load_template` |
| `audit` | `format_audit` | `detect_document_type`、`audit_format`、`list_format_rule_types` |
| `audit` | `content_audit` | `audit_content` |
| `audit` | `style_audit` | `audit_writing_style`、`list_writing_style_types` |
| `audit` | `text_correction` | `correct_text` |
| `audit` | `plagiarism` | `detect_plagiarism` |

**Skill**（`skills/*.md`）：`format_audit`、`content_audit`（内容合规审核与文本纠错，原 `text_correction` 已并入）、
`style_audit`、`plagiarism`、`full_government_audit`（编排型，串联前述审核）。

## 何时声明 Tool，何时定义 Skill

**声明为 Tool（插件）**：
- 功能确定性强、无需 LLM 推理
- 输入输出可用单一函数描述、执行时间短

**定义为 Skill（`.md`）**：
- 需要 LLM 多步规划、组合多个工具
- 任务边界较宽（如"审核整篇文档格式"）
- 希望由业务专家维护流程，而非改代码

## 相关源码

- `src/agent/skills/config.py`：`SkillConfig`
- `src/agent/skills/registry.py`：`SkillRegistry`、`build_catalog()`
- `src/agent/tools/builtin/skill.py`：`SkillTool`
- `src/agent/runtime/`：`AgentRuntime`、子代理预算与生命周期管理
- `skills/*.md`、`skills/schemas/*.py`：Skill 定义与输入模型
- `src/plugin/scanner.py`：`PluginScanner`（支持嵌套插件目录）
- `src/agent/agents/orch.py`：`OrchestratorAgent` 如何注入 Skill 目录并组装 `SkillTool`
