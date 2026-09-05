# Courtier — 通用 AI Agent 平台

**General-Purpose AI Agent Platform / 通用 AI Agent 平台（内网部署优先）**

一个域无关的通用 AI Agent 平台：核心引擎只提供 Agent 运行时、工具编排、插件系统与技能框架，不包含任何业务知识；领域能力以自包含「域包」形式插入，不修改核心代码即可扩展新领域。**为组织内网环境设计，安全是第一设计目标。**

平台自带 **docaudit** 域包作为参考实现——以文档审计为例展示域包机制（技能、规则、插件、工件如何组织），它只是一个普通域包，不是平台身份。

> 配套文档：[面向 AI 代理的仓库说明](AGENTS.md)、[分轮审查报告与设计方案](docs/reviews/)

---

## 为什么内网场景需要特别设计

组织内网与公网 SaaS 的威胁模型完全不同：数据一旦出网即不可接受、所有组件必须可自包含部署、操作必须可审计可追溯、供应链必须可控。Courtier 从第一天起按这些约束设计：

- **全栈自包含** — `docker compose up -d` 在内网起完整基础设施（MySQL / Elasticsearch / MinIO / Langfuse / Prometheus / Grafana），不依赖任何公网 SaaS
- **数据不出网** — LLM 端点由部署方指定（可指向内网私有化模型），embedding / 重排 / 工具全部走内网
- **一切可审计** — LLM prompts、工具调用、记忆变更、设置变更全量落结构化审计日志，可按操作者追溯
- **供应链可控** — 依赖经 uv lockfile 锁定；插件独立 venv + 独立 lockfile；镜像钉版本、非 root 运行

## 安全设计（内网场景）

### 部署边界

- 全部基础设施端口（MySQL / ES / MinIO / Langfuse / Prometheus / Grafana）默认只绑定 `127.0.0.1`，服务间经 Docker 内网通信
- 生产环境**缺关键配置即拒绝启动**（fail-fast）：`MYSQL_URL` / `COURTIER_SETTINGS_KEY` / `DEPLOYMENT_ENVIRONMENT` 不满足时 lifespan 直接中止，杜绝「健康但裸奔」的实例
- 初始化向导在生产/预发布环境强制要求一次性 `COURTIER_SETUP_KEY`；生产拒绝从环境变量降级登录
- 插件容器以非 root 用户运行；compose 中 `COURTIER_PLUGIN_TOKEN` 为必填项

### 身份认证与会话

- JWT（HS256）+ bcrypt 凭据存储；access token 短时效，refresh token 为 httpOnly / SameSite=Strict cookie
- **Refresh token 轮换 + 重用检测**：重放已轮换的 refresh token 会触发整个 token 家族吊销，攻击者会话立即失效
- 登录失败锁定（10 次失败锁 15 分钟）；RBAC（管理员 / 审计员），管理员不可自行改角色
- 改密 / 管理员重置密码**同步吊销全部在用 refresh token**——被窃凭据立即失效
- SSE 传输基于 fetch + Bearer 头，任务全文走 POST JSON body：prompt 不进访问日志、浏览器历史与 URL

### 插件边界（半信任执行体）

- 每个插件是独立进程 + 独立 venv + 独立 lockfile：崩溃不波及宿主，依赖互不污染
- 通道为**双向 token 握手**（fail-closed）：未认证连接的一切通知与请求被丢弃
- 每连接并发上限，超限直接 busy 拒绝——防止单插件 OOM 或拖垮宿主
- **按会话取消**：停止会话只取消该会话的在途插件请求，其他会话不受影响
- **宿主校验插件上报的 session_id**：artifact / cache / memory / 模板等会话级数据服务拒绝越会话访问
- 插件不持有数据库凭据、不持有全权 MinIO 账号、不持有 LLM 端点：文件交换仅经专用传输桶（受限 IAM，24h 生命周期）
- 跨不可信网段部署插件时，文档建议以 WireGuard / stunnel 加固通道

### 秘钥与配置

- 三层配置：Tier-0 环境变量（启动必需）→ 数据库设置表（管理后台热更）→ 快照合并
- 全部秘钥（LLM 端点 key、MinIO 账号、ES 凭据）Fernet 加密落库；审计表只记录 sha256
- 生产模式下秘钥写入 fail-closed：加密不可用时拒绝保存，绝不落明文
- CORS 锁定救援通道：被锁死时可由环境变量覆盖，且 UI 保存会收到明确的 409 提示

### 数据安全与审计

- **结构化审计日志**：逐 run 记录 LLM prompts / reasoning / 工具调用 / 用量，可按操作者追溯
- **注销九步级联**：会话、上传文件、个人资源、记忆、ES 结果、审计行全部清除或匿名化（actor → `deleted-user:<id>`，明文标题清空）；审计日志文件移入隔离区，按保留期（默认 30 天）自动清除
- 分层记忆带 hash 审计链（逐行 old/new hash）；重复声明、越会话访问、路径穿越（符号链接 resolve + fail-closed）均被拒绝并留痕
- 工件 / 记忆 / 会话数据按会话与所有者强隔离：插件越会话访问、模型伪造身份参数（host-injected 参数剥离）、路径逃逸均被拒绝并留痕

### 运行时防护（Guardrails）

- 统一运行管线：每层（输入 / 输出 / 工具调用 / 工具后）按「适配 → 执行 → 记录」顺序执行守卫；tool_call 层 fail-closed
- 声明式注册：守卫以 `guardrail_guards` 设置管理（管理后台可启用 / 停用，import 校验在保存路径），域包经 `domain.yaml` 贡献额外守卫
- 会话工作区路径沙箱：工具文件访问限定在会话工作区根内，符号链接解析后仍越界即拒绝
- 敏感工具确认交互：高危工具调用挂起等待管理员批准（approve / approve_session / deny）

## 功能特性

- **Agent 运行时** — Think → Act → Observe 循环，SSE 流式输出（fetch 传输，Last-Event-ID 水印断线续传），思考 / 工具 / 子代理树形展示
- **分级事件总线** — token 流与关键事件（工具结果、状态迁移）分队列投递，洪峰下关键事件不丢失
- **技能编排** — 技能 = Markdown + YAML frontmatter，编排器派发给子代理执行，类型化输入（Pydantic schema 校验）
- **PromptEngine** — Jinja2 + YAML PromptBundle，核心零硬编码自然语言，域包携带 zh-CN / en-US 模板
- **分层记忆** — 全局层 + 用户层 × 领域包，DB 真相源，hash 审计链，memory 插件工具
- **模型池** — 两级（端点 → 模型）管理，每次运行可指定模型；embedding 与聊天端点解耦
- **Web 终端前端** — Vue 3 + Vite，fetch SSE 传输，流式 Markdown、工具卡片、子代理树、确认交互卡片
- **可观测** — OpenTelemetry → Langfuse、Prometheus + Grafana

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
├── AGENTS.md             # 面向 AI 代理的仓库说明
├── README.md             # 本文件
├── LICENSE               # GPL-2.0
├── docs/                 # 审查报告与设计方案（docs/reviews、docs/architecture）
├── courtier/             # 平台主目录（后端 + 前端 + 迁移 + 测试 + 运维文件）
│   ├── courtier/         # 域无关引擎（循环、API、插件系统、事件总线…）
│   ├── domains/docaudit/ # docaudit 域包（技能 / 规则 / 提示词 / 工件 profile）
│   ├── libs/             # 可安装库（docparse、validator、plugin_sdk …）
│   ├── plugins/          # 独立插件进程（每插件独立 venv 与 lockfile）
│   ├── webui/            # Vue 3 终端风格前端
│   ├── alembic/          # 数据库迁移
│   ├── tests/            # pytest 套件（2300+ 用例）
│   └── docs/             # 开发指南（域包 / 插件 / 技能 / guardrails）
└── pi/                   # Pi agent harness（参考项目，独立仓库）
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

首次启动后进入初始化向导创建第一个管理员（生产环境需 `COURTIER_SETUP_KEY`）。API 文档：`http://localhost:8000/docs`。

### 插件

```bash
# 复制插件环境样例并填入 token / MinIO 账号
cp plugins/plugin.env.example plugins/plugin.env

# 以独立进程启动全部开发插件（宿主经 COURTIER_PLUGIN_ENDPOINTS 连接）
uv run python scripts/dev-plugins.py
```

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

- [域包开发指南](courtier/docs/domain-package-guide.md)
- [插件开发](courtier/docs/plugin-development.md)
- [技能编写](courtier/docs/skill-authoring.md)
- [Guardrails 参考与扩展](courtier/docs/guardrails.md)
- [设置与秘钥](courtier/docs/operations/settings-and-secrets.md)
- 审查与设计记录：[docs/reviews](docs/reviews/)、[docs/architecture](docs/architecture/)

## 示例域包：docaudit

docaudit 是平台自带的参考域包，演示技能、规则、插件与工件 profile 的完整打包方式。当前提供：

- **文档解析** — PDF / DOCX / 扫描件（OCR 管线，页数上限与请求级缓存）
- **排版审查** — 内置 GB/T 9704-2012 公文格式规则集（红头、字号、边距、成文日期），规则文件可替换、可扩展
- **内容审查** — 政治合规、涉密信息、政策一致性
- **文本纠错** — 错别字、标点、术语规范
- **查重检测** — 段落级相似度（动态 IQR 阈值）
- **批注导出** — 审查结果导出为带批注的 DOCX / PDF

## License

GPL-2.0 — see [LICENSE](LICENSE).
