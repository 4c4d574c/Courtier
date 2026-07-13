# Courtier — 通用 AI Agent 平台设计

**日期:** 2026-07-10
**状态:** 设计已批准，待实现
**版本:** 0.1

---

## 1. 概述

### 1.1 背景

DocAudit / SDTAgent 当前定位为"中文政府公文审计系统"。核心 Agent 基础设施（Agent Loop、ToolRegistry、Plugin System、Skill Framework、Hooks、Permissions 等）占代码量约 60-70%，已经是领域无关的。领域耦合主要在系统提示词、Skills/Plugins 内容、领域验证包和数据库表结构。

### 1.2 目标

将项目重构为 **Courtier** — 一个通用 AI Agent 平台。领域无关的核心引擎 + 可插拔的领域包（Plugin + Skill + Config）。公文审计成为平台的第一个官方领域包。

### 1.3 设计原则

- **核心零领域知识** — Core 引擎不 import 任何领域代码
- **核心零硬编码文本** — 所有自然语言文本由 PromptBundle 注入
- **领域包纯声明式** — Plugin(s) + Skill(s) + Rules + Config + i18n
- **接口硬约束** — Core 和 Domain 之间只通过 Plugin/Skill 协议通信
- **渐进迁移** — 四阶段迁移，每阶段可独立验证

---

## 2. 命名与身份

**Courtier** — 朝臣/侍臣，暗喻"为决策者提供建议的智能助手"。

| 属性 | 值 |
|------|-----|
| 平台名称 | Courtier |
| Python 包名 | `courtier-core` |
| 领域包命名 | `courtier-domain-{name}` |
| 仓库名 | `courtier`（从 `docaudit-agent` 改名） |

---

## 3. 架构

### 3.1 仓库结构

```
courtier/
├── packages/
│   ├── core/                   # 通用 Agent 引擎
│   │   ├── src/courtier/
│   │   │   ├── engine/         # loop, state, context
│   │   │   ├── tools/          # registry, protocol, builtin
│   │   │   ├── plugins/        # JSON-RPC host, protocol
│   │   │   ├── skills/         # registry, loader
│   │   │   ├── agents/         # base, orchestrator, subagent
│   │   │   ├── prompts/        # engine, PromptBundle
│   │   │   ├── artifacts/      # store, projection
│   │   │   ├── hooks/          # hook chain
│   │   │   ├── permissions/    # permission gate
│   │   │   ├── memory/         # memory store
│   │   │   ├── telemetry/      # OpenTelemetry
│   │   │   ├── storage/        # MinIO client
│   │   │   ├── es/             # Elasticsearch client
│   │   │   ├── db/             # DB operations (generic)
│   │   │   └── api/            # FastAPI app, routes, SSE, middleware
│   │   └── pyproject.toml
│   │
│   ├── domains/
│   │   └── docaudit/           # 公文审计（第一个官方 domain）
│   │       ├── plugins/        # format_audit, content_audit, text_correction, plagiarism, parse, annotate
│   │       ├── skills/         # format_audit.md, content_audit.md, full_audit.md, plagiarism.md
│   │       ├── rules/          # format rules, content compliance rules (JSON)
│   │       ├── config/
│   │       │   ├── prompts/zh-CN/  # orchestrator.yaml, subagent.yaml, behavioral.yaml, errors.yaml
│   │       │   ├── prompts/en-US/  # 同上，英文版
│   │       │   └── domain.yaml     # 领域元数据
│   │       ├── models/         # 领域数据模型（可选）
│   │       └── pyproject.toml
│   │
│   └── webui/                  # Vue 3 前端
│
└── docs/                       # 架构文档、领域包开发指南
```

### 3.2 核心接口协议

#### Plugin 协议（JSON-RPC 2.0 on stdio）

- 每个 Plugin 是独立子进程，通过 stdin/stdout 通信
- `plugin.yaml` 声明：name, version, type: tool, tools[]
- 支持方法：`tools/list`（发现）、`tools/call`（调用）、`health`（健康检查）
- 超时默认 120s，可在 plugin.yaml 声明 `timeout_ms`
- 错误格式统一：`{"error": {"code": int, "message": str, "data": any}}`
- 连续 3 次 health 失败则重启插件

#### Skill 协议（Markdown + YAML frontmatter）

```yaml
---
name: skill_name
description: ...
version: "1.0"
type: skill
tools: [tool_a, tool_b]
input_model: InputClassName
mode: sequential | parallel | auto     # 编排模式
output_schema: OutputClassName         # 输出结构
timeout_seconds: 600
retry_policy: none | on_error | on_timeout
---
# Skill Body (Markdown — SubAgent system prompt)
```

#### PromptBundle 协议

```python
class PromptBundle(BaseModel):
    """领域包注入的完整提示词集合。Core 不包含任何硬编码自然语言文本。"""
    locale: str
    templates: dict[str, str]  # key 使用 dot-notation 命名约定

    # 模板 key 约定:
    #   orchestrator.system_prompt
    #   orchestrator.task_decomposition
    #   subagent.system_prompt
    #   subagent.tool_call_reminder
    #   chat.welcome_message
    #   chat.system_prompt
    #   errors.tool_timeout
    #   errors.permission_denied
    #   ui.session_title
    #   ui.step_label
    #   behavioral.thinking_directive
    #   behavioral.rules[]
```

#### Domain 包注册协议

```python
class DomainPackage(Protocol):
    name: str
    plugins: list[Path]
    skills: list[Path]
    prompt_bundle: PromptBundle
```

#### 领域配置元数据（domain.yaml）

```yaml
name: docaudit
title: 公文智能审计
description: 基于 GB/T 9704-2012 的公文格式与内容审查
locales: [zh-CN]
requires_plugins: [format_audit, content_audit, text_correction, plagiarism]
requires_services: [mysql, elasticsearch, minio]
```

### 3.3 加载流程

```
CourtierConfig.domain_packages = ["docaudit"]
  │
  ├─> 1. 扫描 plugins/*/plugin.yaml     → PluginRegistry
  ├─> 2. 扫描 skills/*.md               → SkillRegistry
  ├─> 3. 加载 config/prompts/{locale}/  → PromptEngine (PromptBundle)
  └─> 4. create_app(config, prompt_bundle) → FastAPI app
```

### 3.4 SubAgent 隔离模型

```python
class SubAgentConfig(BaseModel):
    skill: str
    task: str
    isolation: Literal["shared", "context_window", "process"]
    # shared: 共享上下文（默认）
    # context_window: 独立上下文窗口，通过 artifact 传递数据
    # process: 独立进程（预留，与 Plugin 模式重复，暂不实现）
```

---

## 4. i18n 与 Prompt 系统

### 4.1 Prompt 模板引擎

```python
class PromptEngine:
    """零硬编码 — 所有文本来自 PromptBundle，Jinja2 渲染"""

    def __init__(self, bundle: PromptBundle, locale: str = "en-US"): ...
    def render(self, template_name: str, **variables) -> str: ...
```

模板使用 Jinja2 语法，变量通过 `render()` 的 kwargs 传入。

### 4.2 PromptBundle 加载与回退

```
启动时，对每个 domain package:
  1. 加载 config/prompts/{requested_locale}/
  2. 如果缺失 → 回退到 domain 声明的第一个 locale
  3. 如果仍缺失 → 回退到 Core 内置 en-US 最小模板
  4. 多个 domain 包的 PromptBundle merge，后者覆盖同名 template
```

### 4.3 迁移映射

| 现状（硬编码） | 目标（PromptBundle 模板） |
|---|---|
| `orch.py: "你是 DocAudit 文档审核编排代理"` | `orchestrator.system_prompt` |
| `agent_service.py: CHAT_AGENT_SYSTEM_PROMPT` | `chat.system_prompt` |
| `behavioral_rules.py: THINKING_DIRECTIVE` | `behavioral.thinking_directive` |
| `behavioral_rules.py: BEHAVIORAL_RULES` | `behavioral.rules` |

---

## 5. 迁移计划

### Phase 1: Core 净化（2-3天）

不改目录结构，只抽离硬编码文本。

1. 新建 `src/courtier/prompts/engine.py` — PromptEngine + PromptBundle
2. 建 `config/prompts/zh-CN/` — 搬入所有硬编码文本为 YAML
3. 建 `config/prompts/en-US/` — 英文翻译
4. `create_app()` 接受 `PromptBundle` 参数
5. `OrchestratorAgent` 通过 `PromptEngine` 获取 system prompt
6. 删除 `src/common/behavioral_rules.py` 中的硬编码文本

**验证：** 现有测试全部通过，API 行为不变。

### Phase 2: 目录重组（3-5天）

建立 packages 分层，改写领域代码为 Plugin。

1. `src/agent/` → `packages/courtier-core/src/courtier/`
2. `plugins/` → `packages/domains/docaudit/plugins/`
3. `skills/` → `packages/domains/docaudit/skills/`
4. `src/validator/` → `packages/domains/docaudit/plugins/format_audit/`（改写为 Plugin）
5. `src/content_compliance/` → `packages/domains/docaudit/plugins/content_audit/`（改写为 Plugin）
6. `src/doccorrector/` → `packages/domains/docaudit/plugins/text_correction/`（改写为 Plugin）
7. `src/docparse/` → `packages/domains/docaudit/plugins/parse/`（改写为 Plugin）
8. `src/docannot/` → `packages/domains/docaudit/plugins/annotate/`（改写为 Plugin）
9. `src/storage/`, `src/es/` → core
10. `src/dbop/` → core（通用部分）+ docaudit/models（公文表）
11. 更新 import 路径
12. 更新 pyproject.toml

**验证：** 所有测试通过。

### Phase 3: 契约强化（2-3天）

接口协议做成硬约束，写文档。

1. Plugin 协议：超时、健康检查、错误格式
2. Skill 协议：新增字段，schema 验证
3. Domain 包协议：domain.yaml schema 验证
4. 写 `docs/domain-package-guide.md`
5. 写 `docs/plugin-development.md`
6. 写 `docs/skill-authoring.md`
7. CLI 验证命令：`courtier validate-domain <path>`

**验证：** `courtier validate-domain` 通过。

### Phase 4: 前端重命名 + 文档（1天）

1. 仓库重命名 `docaudit-agent` → `courtier`
2. 更新 CLAUDE.md, README.md, docs/
3. 前端 webui/ 更新品牌名称
4. GitHub description 更新
5. Tag: `v0.2.0`

**总计：8-12 天。**

---

## 6. 添加新领域（示例）

以"合同审查"为例，新领域包只需：

```
packages/domains/contract-review/
├── plugins/
│   ├── clause_check/plugin.yaml
│   └── risk_analysis/plugin.yaml
├── skills/
│   ├── clause_review.md
│   └── full_contract_review.md
├── rules/
│   └── risk_patterns.json
├── config/
│   ├── prompts/zh-CN/...
│   └── domain.yaml
└── pyproject.toml
```

不需要修改 Core 一行代码。用户在 `.env` 中配置：

```bash
COURTIER_DOMAIN_PACKAGES=docaudit,contract-review
```

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Phase 2 目录重组导致大量 import 路径变更 | 每一小步执行并验证测试，不批量移动 |
| Plugin 改写可能引入性能开销（子进程 vs 直接调用） | 保留 in-process tool 选项；Plugin 用于隔离/独立依赖场景 |
| 重命名后现有用户/文档链接失效 | GitHub 自动重定向；保留旧 tag `v0.1.0` 作为归档 |

---

## 9. 迁移后补强记录（2026-07-13）

在 Courtier 平台主体迁移完成后，进行第六轮 gap analysis 并修复以下运行时/部署/品牌一致性问题：

| 问题 | 修复 |
|------|------|
| Docker 生产镜像无法自动定位仓库根目录 | 引入 `COURTIER_REPO_ROOT` 环境变量，Dockerfile 设置为 `/app` |
| Elasticsearch 健康检查未携带认证 | `docker-compose.yml` 健康检查使用 `-u elastic:${ES_PASSWORD}` |
| OTel Collector 向 Langfuse 使用错误 Bearer auth | 改为 Basic auth，并在 `.env.example` 中说明 `LANGFUSE_AUTH_HEADER` |
| Wheel 包缺少领域运行时资源 | 将 `config/` 转为 Python package（`__init__.py`），Hatch 随 `packages/domains/docaudit` 自动纳入 `config/` 与 `skills/` |
| Dockerfile 以 root 运行且无健康检查 | 新增非 root `courtier` 用户、`COURTIER_REPO_ROOT`、`HEALTHCHECK` |
| `BCRYPT_ROUNDS` 设置未被使用 | `hash_password()` 与 admin bootstrap 均使用 `settings.bcrypt_rounds` |
| 插件环境变量仍使用 `DOCAUDIT_` 前缀 | 主推 `COURTIER_PROJECT_ROOT` / `COURTIER_UPLOAD_DIR`，保留旧名做兼容回退 |
| README/CLAUDE.md 启动示例 PYTHONPATH 不完整 | README 示例已补齐 `packages/domains/docaudit` |
| Prometheus 抓取已注释的 `app` 服务 | 注释掉 `courtier` scrape job，避免服务未启动时告警 |
| 缺少 Alembic 迁移健康检查 | 新增 `tests/test_alembic_smoke.py`，离线验证 revision graph |
| 开发依赖缺少格式化/类型工具 | `pyproject.toml` dev 组新增 `black`、`isort`、`ruff`、`mypy` 及对应配置 |

以上修复保持测试套件 813 passed / 6 skipped、wheel 构建、前端构建、插件扫描 VALID:8 / BLOCKED:0 的基线不变。
