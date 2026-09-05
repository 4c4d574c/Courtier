# Courtier — 通用 AI Agent 平台 / General-Purpose AI Agent Platform

A domain-agnostic AI agent platform with a pluggable domain-package architecture. The core engine provides an agent runtime, tool orchestration, a plugin system, and a skill framework — with zero domain knowledge baked in. Domain capabilities ship as self-contained packages (plugins + skills + rules + locale prompts).

**项目简介**：Courtier 是一个域无关的通用 AI Agent 平台。核心引擎只提供 Agent 运行时（Think → Act → Observe 循环）、工具编排、插件系统与技能框架，不包含任何业务知识；领域能力以自包含「域包」形式插入。首个官方域包 **docaudit** 提供基于 GB/T 9704-2012 的中文公文智能审计（排版审查、内容合规、查重、批注导出）。

## 功能特性

- **Agent 运行时** — Think → Act → Observe 循环，SSE 流式输出（fetch 传输，Last-Event-ID 水印断线续传），思考/工具/子代理树形展示
- **分级事件总线** — token 流与关键事件（工具结果、状态迁移）分队列投递，洪峰下关键事件不丢失
- **插件系统** — 插件为独立进程，宿主经 TCP 发起 JSON-RPC 2.0 连接，双向 token 握手；按会话取消在途请求；插件容器以非 root 用户运行
- **技能编排** — 技能 = Markdown + YAML frontmatter，编排器派发给子代理执行，支持类型化输入（Pydantic schema 校验）
- **PromptEngine** — Jinja2 + YAML PromptBundle，核心零硬编码自然语言，域包携带 zh-CN / en-US 模板
- **Guardrails** — 统一运行管线（适配 → 执行 → 记录），声明式注册（设置表可管理），路径策略、工具确认交互、工具停用内建
- **分层记忆** — 全局层 + 用户层 × 领域包，DB 真相源，hash 审计链，memory 插件工具
- **模型池** — 两级（端点 → 模型）管理，每次运行可指定模型；embedding 与聊天端点解耦
- **Web 终端前端** — Vue 3 + Vite，fetch SSE 传输，流式 Markdown、工具卡片、子代理树、确认交互卡片
- **可观测** — OpenTelemetry → Langfuse、Prometheus + Grafana、结构化审计日志（含注销隔离与保留期清理）

## 架构总览

```
浏览器 (Vue 3)
   │  fetch SSE (POST /api/sessions/run, Last-Event-ID 续传)
   ▼
FastAPI (courtier/agent/api)  ── RunManager：会话运行=后台任务，SSE=可重放事件日志
   │
   ├─ Agent 循环 (courtier/agent/core)  ── Think → Act → Observe + Guardrails
   │      ├─ 模型池 (多端点/多模型，每次运行可选)
   │      ├─ 分层记忆 (MySQL，全局层 + 用户层 × 领域)
   │      └─ 技能派发 → 子代理
   │
   ├─ 插件系统 (courtier/plugin)  ── TCP JSON-RPC 2.0，双向 token，受限 MinIO 账号
   │
   └─ 域包 (domains/docaudit)  ── 技能 / 规则 / 提示词 / 工件 profile

基础设施数据面：MySQL 8（会话/用户/记忆/设置）· Elasticsearch 8（切片检索）
                · MinIO（原文与插件传输桶，24h 生命周期）
```

## 仓库结构

```
courtier/                 # 后端与前端根目录
├── courtier/             # 域无关引擎
│   ├── agent/            # 循环、编排器、子代理、API、事件总线
│   ├── plugin/           # TCP JSON-RPC 插件系统
│   ├── skills/           # 技能注册表
│   ├── prompts/          # PromptEngine（Jinja2 + YAML）
│   ├── domain/           # 域包发现与校验
│   ├── artifacts/        # 工件系统（schema registry、投影器）
│   └── db/ storage/ es/  # MySQL / MinIO / Elasticsearch 客户端
├── domains/docaudit/     # docaudit 域包（技能/规则/提示词/工件 profile）
├── libs/                 # 可安装库（docparse、validator、plugin_sdk …）
├── plugins/              # 独立插件进程（每插件独立 venv 与 lockfile）
├── webui/                # Vue 3 终端风格前端
├── docs/                 # 架构与开发文档
├── alembic/              # 数据库迁移
└── tests/                # pytest 套件（2300+ 用例）

pi/                       # Pi agent harness（参考项目，独立仓库）
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | FastAPI（Python 3.12+，uv 管理） |
| Agent 运行时 | Think → Act → Observe + SSE 流式（fetch 传输） |
| 插件协议 | JSON-RPC 2.0 over TCP，双向 token 握手 |
| 数据库 | MySQL 8.x（SQLAlchemy 2 异步 ORM + Alembic） |
| 检索 | Elasticsearch 8.x（别名 + 写索引切换） |
| 对象存储 | MinIO（S3 兼容；插件传输桶 24h 生命周期） |
| 可观测 | OpenTelemetry → Langfuse · Prometheus · Grafana |
| 前端 | Vue 3 + Vite + TypeScript（fetch SSE 传输） |

## 快速开始

### 前置要求

- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)
- Node.js 18+（前端）
- MySQL 8.x、Elasticsearch 8.x、MinIO（`docker compose up -d` 一键起全套基础设施）
- 可选：PaddleOCR 服务（扫描件 OCR）、LibreOffice（PDF→DOCX 转换）

### 后端

```bash
cd courtier

# 安装依赖
uv sync

# 配置环境（Tier-0 必填项）
cp .env.example .env
#   MYSQL_URL                —— 数据库连接
#   COURTIER_SETTINGS_KEY    —— 设置表秘钥加密（丢失将无法解密已存秘钥）
#   DEPLOYMENT_ENVIRONMENT   —— development / staging / production
#   COURTIER_PLUGIN_TOKEN    —— 插件通道鉴权 token
# 其余全部应用设置存数据库，首次启动后经「管理后台 → 系统设置」修改

# 启动（main.py 先自动执行 Alembic 迁移，再起 uvicorn）
uv run main.py
# 自定义端口 / 热重载
uv run main.py --host 127.0.0.1 --port 8080 --reload
```

首次启动后访问 `http://localhost:8000` 进入初始化向导创建第一个管理员（生产环境需 `COURTIER_SETUP_KEY`）。API 文档：`http://localhost:8000/docs`。

### 插件

```bash
# 复制插件环境样例并填入 token / MinIO 账号
cp plugins/plugin.env.example plugins/plugin.env

# 以独立进程启动全部开发插件（宿主经 COURTIER_PLUGIN_ENDPOINTS 连接）
uv run python scripts/dev-plugins.py
```

插件与宿主通过双向 token 握手认证；文件交换走 MinIO 专用传输桶（24h 生命周期，受限 IAM）。

### 前端

```bash
cd courtier/webui
npm install
npm run dev        # http://localhost:5173，/api 代理到 8000
```

### Docker

```bash
docker compose build          # 应用与插件镜像（插件以非 root 用户运行）
docker compose up -d          # 基础设施 + 全套服务
```

## 测试

```bash
# 后端全套（不含需要真实 ES/MinIO 的 integration 用例）
uv run pytest -m "not integration"

# 前端（eslint + 22 个断言脚本）
cd webui && npm test
```

## 文档

- [域包开发指南](docs/domain-package-guide.md)
- [插件开发](docs/plugin-development.md)
- [技能编写](docs/skill-authoring.md)
- [Guardrails 参考与扩展](docs/guardrails.md)
- [设置与秘钥](docs/operations/settings-and-secrets.md)
- 审查与设计记录：`docs/reviews/`（分轮审查报告）、`docs/architecture/`（设计方案）

## docaudit 域包

内置的 docaudit 域包提供中文公文审计能力：

- **文档解析** — PDF / DOCX / 扫描件（OCR 管线，页数上限与请求级缓存）
- **排版审查** — GB/T 9704-2012 合规检查（红头、字号、边距、成文日期等规则）
- **内容审查** — 政治合规、涉密信息、政策一致性
- **文本纠错** — 错别字、标点、术语规范
- **查重检测** — 段落级相似度（动态 IQR 阈值）
- **批注导出** — 审查结果导出为带批注的 DOCX / PDF

## License

Internal project — contact the maintainers for usage terms.
