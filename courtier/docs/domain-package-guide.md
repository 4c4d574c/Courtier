# Courtier 域包开发指南

## 概述

域包（domain package）是向 Courtier 平台注入领域能力的自包含目录。核心引擎是域无关的——它不理解"公文"，也不理解任何具体业务；领域能力（技能、插件、提示词、规则守卫）全部由域包提供，在运行时按会话自我激活。

仓库内置的第一个域包是 `domains/docaudit/`（基于 GB/T 9704-2012 的公文审计）。新建域包时它就是最好的参照。

## 目录结构

```
domains/my-domain/
├── config/
│   ├── domain.yaml          # 必需：域元数据
│   └── prompts/
│       └── zh-CN/           # 域特定提示词模板（YAML，每个 locale 一个目录）
│           ├── orchestrator.yaml
│           └── chat.yaml
├── skills/                  # 技能定义（Markdown + YAML frontmatter，平铺扫描）
│   ├── my_skill.md
│   └── schemas/             # 技能结构化输入模型（Pydantic）
│       └── my_skill.py
└── domain_artifacts.py      # 可选：产物档案（artifact 注册/物化/投影）
```

注意几个与直觉不同的点：

- **插件不在域包里。** 插件是独立 TCP 服务，统一放在仓库顶层 `plugins/` 下：跨域插件放 `plugins/shared/<name>/`，域专属插件放 `plugins/<domain>/<name>/`。宿主正是用这个目录归属来做插件→域的映射和可见性门控。插件开发见 `docs/plugin-development.md`。
- **没有 rules/、models/、pyproject.toml。** 规则 JSON 随消费它们的库走（如 `libs/docaudit/validator/`），共享数据模型放 `libs/shared/docmodels/`。域包是纯数据 + 配置 + 技能 Markdown，不是 Python 发行包。
- **`skills/schemas/` 是唯一惯例上的 Python 代码。** 技能输入模型经点分路径（如 `skills.schemas.my_skill.MyInput`）延迟导入，因此域包目录必须可导入——见下文「技能输入模型」。

## 发现与加载

启动时 `CourtierConfig.discover()`（`courtier/config.py`）扫描仓库顶层 `domains/` 目录：

1. **域名解析**：`COURTIER_DOMAIN_PACKAGES` 环境变量（逗号分隔）是显式白名单，未设置时扫描目录下全部域包。显式配置的域包不存在则启动报错（`FileNotFoundError`/`ValueError`）——配置错误必须响亮地失败，而不是静默少加载。
2. **停用文件**：未设置 `COURTIER_DOMAIN_PACKAGES` 时，`domains/.disabled`（纯文本，每行一个域名，支持 `#` 注释）里列出的域被跳过。管理后台的域开关读写的就是这个文件；它有意不用 `.env`，因为不含机密、可安全被 admin API 读写。显式白名单永远优先于 `.disabled`。
3. **逐包加载**：读 `config/domain.yaml`（Pydantic 校验，失败即抛错），并用 `PromptEngine.from_domain_directories` 装载该域的提示词包（PromptBundle）。

区域设置由 `COURTIER_LOCALE` 控制（默认 `zh-CN`），决定渲染哪套模板。

## domain.yaml 参考

| 字段 | 必需 | 类型 | 说明 |
|------|------|------|------|
| `name` | 是 | string | 域唯一标识（kebab-case），需与目录名一致 |
| `title` | 否 | string | 人类可读的展示名 |
| `description` | 否 | string | 一句话描述；同时出现在 `activate_domain` 目录里供模型选域 |
| `locales` | 否 | string[] | 声明支持的语言（默认 `["en-US"]`）；每个声明的 locale 必须有对应的提示词目录 |
| `requires_plugins` | 否 | string[] | 依赖的插件名清单；校验时会到 `plugins/` 全树下核对 |
| `requires_services` | 否 | string[] | 依赖的基础设施（`mysql`、`elasticsearch`、`minio`） |
| `guards` | 否 | list | 域自带的进程内守卫声明，激活时注册进会话 GuardrailSystem（见下文） |

示例（即 docaudit）：

```yaml
name: docaudit
title: 公文智能审计
description: 基于 GB/T 9704-2012 的公文格式与内容审查，以及文本纠错、查重检测、涉密信息判别
locales: [zh-CN, en-US]
requires_plugins: [parse, template, search, annotate, detect_plagiarism, check_format, check_content]
requires_services: [mysql, elasticsearch, minio]
```

### guards 声明

`guards` 的每一项有两种形式：

- **字符串**（类路径，如 `"docaudit.guards.FormatGuard"`）：会话级（session scope）守卫，激活时无参实例化，随会话重建重放；
- **对象**：`{name, class_path, scope, enabled}`，其中 `scope` 可为 `session` 或 `run`（`run` 级守卫每次 `agent_loop` 全新实例化）。

激活时守卫注册到会话 GuardrailSystem（owner 为 `domain:<name>`），加载失败只记日志、不阻断激活。声明在 `validate-domain` 时就会被检查（可导入、形似守卫、scope 合法）。守卫协议与统一管线见 `docs/guardrails.md`。

## 提示词模板

域包只携带**域特定**的模板，放在 `config/prompts/{locale}/*.yaml`。核心默认模板（行为规则、错误文案、工具调用规则、上下文压缩等）随核心发布在 `courtier/prompts/defaults/{locale}/`，域包**可以按 key 覆盖任意核心默认**——渲染时域模板优先。

docaudit 当前提供的键（供参照）：

| 键 | 用途 |
|----|------|
| `orchestrator.system_prompt` | 编排器身份与领域指令 |
| `orchestrator.task_decomposition` | 任务拆解提示 |
| `orchestrator.workflow_rules` | 领域工作流规则（**激活载荷**，见下） |
| `chat.system_prompt` | 聊天代理身份 |

模板是 Jinja2 语法，渲染时注入变量。核心保证域包缺失的 key 回落到核心默认，最后才是引擎内置的英文兜底。

### orchestrator.workflow_rules 的特殊语义

这不是普通的模板：它是**激活载荷**。`DomainActivator.activate()` 是它唯一的写入者——域激活时，用该域自己的 PromptBundle 渲染 `orchestrator.workflow_rules` 并叠加到提示词管线的 rules 段（owner `domain:<name>`），同时标记系统提示词脏位，使同一次运行内的激活从下一轮起生效。域包没有这个 key 时保持沉默（不会注入任何兜底规则文案）。

## 运行时激活（域门控）

域门控是**可见性过滤器**，不是进程开关——插件子进程照常启动，只是编排器看不见未激活域的工具：

- 编排器启动时只看得见 `plugins/shared/` 的插件工具和内建工具；
- 模型按需调用 `activate_domain` 元工具自我激活某个域：注册该域全部技能的 SkillTool、注入该域插件代理、叠加 `orchestrator.workflow_rules`、注册域守卫、加载域产物档案；
- 激活是幂等、可加的，激活集合持久化在 `SessionRecord.active_domains`，每次请求重建代理时重放；
- 域插件工具的可见性由 `plugin_domain()` 映射决定——即插件在 `plugins/` 下的目录归属。

可选的 `domain_artifacts.py` 是域的**产物档案**：提供模块级 `register_domain_artifacts(default_registry, materializer_registry, projector_registry)` 钩子，注册本域的产物 schema、物化器与投影器。激活时从包目录直接导入（不依赖 sys.path 顺序），重复激活幂等，档案损坏只告警不阻断。

## 技能与输入模型

- 技能是 `skills/*.md`（平铺，不递归），YAML frontmatter 定义元数据，正文是子代理的系统提示词。写作详见 `docs/skill-authoring.md`。
- 结构化输入模型放 `skills/schemas/<skill>.py`，在 frontmatter 里用点分路径引用，如 `input_model: skills.schemas.format_audit.FormatAuditorInput`。

**导入路径注意**：`skills.schemas.*` 之所以能导入，是因为 `main.py` 的 `_inject_monorepo_paths()` 把域包目录放上了 `sys.path`（当前硬编码注入 `domains/docaudit`）。新建域包若要使用 typed input，需要在该列表里补上 `domains/<name>`，或把域包做成已安装的包。

## 校验

```bash
cd courtier
uv run courtier validate-domain domains/my-domain/
```

检查项（任一失败以非零码退出并列出问题）：

1. `config/domain.yaml` 存在且能通过 Pydantic 校验；
2. 每个声明的 locale 有提示词目录且目录里有至少一个 YAML 文件；
3. `requires_plugins` 里每个插件名都能在 `plugins/` 全树的 `plugin.yaml` 中找到（按 manifest 的 `name` 匹配，与目录层级无关）；
4. `skills/` 目录存在且至少有一个 `.md` 文件；
5. `guards` 每条声明可导入、形似守卫类、scope 合法。

`plugins_root` 默认从域包路径推断（域包必须位于 `domains/` 之下，插件目录取同级 `plugins/`）；域包不在 `domains/` 下时需显式传 `plugins_root`。

## 新建域包清单

1. `domains/<name>/config/domain.yaml`：填元数据、声明 locale、列出依赖插件；
2. `domains/<name>/config/prompts/zh-CN/`：写 `orchestrator.system_prompt`（域身份）与 `orchestrator.workflow_rules`（激活后叠加的工作流规则），按需加 `chat.system_prompt`；
3. 把域专属插件放到 `plugins/<name>/`（每个插件独立 venv 与 manifest，见插件开发指南），跨域插件放 `plugins/shared/`；
4. `domains/<name>/skills/`：写技能 Markdown，需要结构化输入时加 `skills/schemas/` 并把域包目录加进 `main.py` 的 sys.path 注入列表；
5. 有进程内守卫或产物需求时，在 `domain.yaml` 声明 `guards` / 提供 `domain_artifacts.py`；
6. `uv run courtier validate-domain domains/<name>/` 通过后，在 `COURTIER_DOMAIN_PACKAGES` 加入域名（或留空让它自动发现），重启后端；
7. 在会话里让模型调用 `activate_domain` 验证：技能目录出现、域工具可见、系统提示词叠加了工作流规则。
