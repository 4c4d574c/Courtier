# Courtier Platform — 项目规则

## Monorepo 布局

```
courtier/                          # 项目根目录
├── courtier/                      # Courtier 通用引擎（领域无关）
│   ├── agent/                    # Agent 运行时（loop, tools, skills, api, artifacts）
│   ├── config.py                 # Settings + CourtierConfig
│   ├── db/                       # 数据库层（CRUD, 表定义）
│   ├── domain/                   # DomainPackage loader
│   ├── plugin/                   # JSON-RPC 插件系统（manager, registry, manifest; SDK 在 libs/shared/plugin_sdk）
│   └── prompts/                  # Jinja2 PromptEngine + PromptBundle + defaults/{locale}/（Core 默认模板）
├── domains/
│   └── docaudit/                 # 公文审计领域包
│       ├── config/               # domain.yaml + prompts/{locale}/（仅领域专属模板与覆盖项）
│       └── skills/               # Skill 文档（Markdown + YAML frontmatter + schemas/ 类型化输入模型）
├── libs/
│   ├── shared/                   # 跨领域共享库（均为带 pyproject.toml 的可安装包）
│   │   ├── docannot/             # 文档标注
│   │   ├── docmodels/            # 共享数据模型（Document/FormatSpec）
│   │   └── plugin_sdk/           # 插件 SDK（courtier_plugin_sdk）
│   └── docaudit/                 # docaudit 领域专属库
│       ├── docparse/             # 文档解析（PDF/DOCX/扫描件 → GB/T 9704 Document 模型）
│       ├── validator/            # 格式校验
│       ├── content_compliance/   # 内容合规
├── plugins/
│   ├── shared/                   # 跨领域共享插件
│   │   ├── anydoc/               # 通用文档转换工具（convert_document → Markdown）
│   │   ├── search/               # 搜索工具
│   │   ├── annotate/             # 标注工具
│   │   └── template/             # 模板工具
│   └── docaudit/                 # docaudit 领域插件
│       ├── parse/                # 公文文档解析工具（parse_document → GB/T 9704 Document）
│       └── audit/                # 审计包装插件
│           ├── format_audit/
│           ├── content_audit/
│           └── plagiarism/
├── webui/                        # Vue 3 + Vite 前端（Terminal-style UI）
├── tests/                        # 测试套件
│   ├── agent/                    # 核心引擎/API 测试
│   ├── courtier/                 # Courtier 通用组件测试
│   ├── domains/docaudit/         # 公文审计领域测试
│   ├── plugin/                   # JSON-RPC 插件测试
│   └── telemetry/                # 可观测性测试
├── docker-compose.yml            # 可观测性栈（Grafana, Prometheus, Langfuse, OTLP Collector）
├── Dockerfile                    # 生产镜像构建
├── pyproject.toml                # 根项目配置（uv workspace）
└── .env                          # 环境配置
```

## 常用命令

### 后端

Python 3.12+ 必需。使用 `uv` 管理依赖和虚拟环境。

```bash
# 安装依赖
uv sync

# 运行 API 服务
uv run main.py

# 自定义监听地址或启用热重载
uv run main.py --host 127.0.0.1 --port 8080 --reload

# 运行所有测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/agent/api/test_routes.py -v

# 排除集成测试
uv run pytest -m "not integration"

# 数据库迁移
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"

# ES chunks 索引运维（版本化索引 + alias，详见 docs/superpowers/plans/2026-08-13-search-rag-retrieval-implementation.md）
uv run python scripts/reindex_chunks.py --check-only        # MinIO 原件覆盖检查
uv run python scripts/reindex_chunks.py --target-version v4 # 重建到新版本索引
uv run python scripts/reindex_chunks.py --swap-to v4        # 原子切换 alias
uv run python scripts/reindex_chunks.py --rollback          # 回滚 alias

# 检索评测（recall@k / MRR / NDCG@k）
uv run python scripts/retrieval_eval.py
```

### 前端

```bash
cd webui

# 安装依赖
npm ci

# 开发服务器（Vite, 端口 5173, 代理 /api → localhost:8000）
npm run dev

# 类型检查 + 构建
npm run build

# 运行测试
npm test
```

### Docker Compose（可观测性栈）

```bash
docker-compose up -d
# Grafana: localhost:3001
# Langfuse: localhost:3000
# Prometheus: localhost:9090
```

## 架构概览

### 四层架构

1. **API 层** (`courtier/agent/api/`): HTTP handlers (`routes/`), SSE 流 (`sse_adapter.py`), 文件/会话存储, 可观测性中间件
2. **服务层** (`courtier/agent/api/services/`): AgentService（统一 `build_agent`，chat/文档/续聊三形态共用一个编排器构建器）, StreamService, FileService, SessionService
3. **核心引擎层** (`courtier/agent/`): Agent loop, OrchestratorAgent, AgentRuntime + DomainActivator（域门控自激活）, PluginSystem, ToolRegistry, Artifact 系统, Context 管理, Prompt pipeline, Hooks, Permissions, Telemetry
4. **领域/业务层**:
   - `libs/shared/`: docannot, docmodels, plugin_sdk 等跨领域共享库（可安装包），被共享插件调用
   - `libs/docaudit/`: docparse, validator, content_compliance 等领域专属库（可安装包），被 docaudit 插件调用
   - `plugins/shared/`: anydoc, search, annotate, template 等跨领域共享插件（独立 TCP 服务）
   - `plugins/docaudit/`: parse（`plugins/docaudit/parse/`）与 check_format, check_content, detect_plagiarism（`plugins/docaudit/audit/`）等领域专属插件（独立 TCP 服务）

### 会话模式（统一编排器 + 域门控）

已废除 chat/audit 双模式：所有会话（纯聊天 / 上传文档 / 多轮续聊）统一走 `build_agent` 构建的 `OrchestratorAgent`。

- **可见性门控**：编排器初始只可见共享插件工具（`plugins/shared/`，见 `app.py` 的 `shared_plugin_names`）；领域插件进程照常启动，但其工具不可见。
- **自激活**：模型按意图调用 `activate_domain` meta-tool（`courtier/agent/tools/builtin/activate_domain.py`）→ `DomainActivator.activate()`（`courtier/agent/runtime/activation.py`）：注册领域 SkillTool、注入领域插件代理、把领域 `orchestrator.workflow_rules` 覆盖进 PromptPipeline 的 rules 段（激活载荷）。"疑似即激活"，幂等。
- **持久化与重放**：激活集随每次 run 结束写入 `SessionRecord.active_domains`（`stream_service.py`），按请求重建 agent 时静默重放（上下文压缩会丢证据，不能靠历史推导）。
- **取数策略**：格式审核 → `parse_document`（Document 模型）；内容类技能与阅读问答 → 优先复用会话中已有文本（convert_document 的 Markdown 或 parse_document 的文本投影），否则 `convert_document`（扫描件自动 OCR 兜底，`ANYDOC_OCR_API_URL`）。`core.document_markdown → core.plain_text` 投影链（`artifacts/projectors.py`）支撑内容类插件的纯文本入参。
- `orchestrator.system_prompt` 由 core 默认提供（`prompts/defaults/{locale}/orchestrator.yaml`）；领域包只提供 `orchestrator.workflow_rules` 作为激活载荷。
- `activate_domain` 工具描述中的领域目录实时包含各域技能清单（`skills/` 目录按 mtime 缓存，`runtime/activation.py` 的 `build_domain_catalog`）——前端新建技能后无需重启即可被模型感知。

### 插件系统（JSON-RPC 2.0 over TCP，独立服务）

插件是**独立运行的 TCP 服务**，主进程按 `COURTIER_PLUGIN_ENDPOINTS`（`name=host:port` 映射）拨号连接，换行分隔 JSON-RPC 通信；协议与 stdio 时代完全一致（正 id 主进程→插件、负 id 插件→主进程反向 host services）：

- 共享插件位于 `plugins/shared/`（anydoc, search, annotate, template）
- 领域插件位于 `plugins/<domain>/`（如 `plugins/docaudit/parse/`、`plugins/docaudit/audit/`）
- 每个插件有独立 `.venv`、`pyproject.toml`、`plugin.yaml`（`runtime.port` 声明默认监听端口）
- 插件声明 `entry.py` 入口；`PluginRuntime` SDK 提供 `register_capabilities()`, `register_tool()`, `run()`（TCP serve 模式）
- 鉴权：共享 `COURTIER_PLUGIN_TOKEN` 双向校验——插件 `plugin.register` 携带 token 供主进程验证，主进程以 `plugin.auth` 自证；鉴权失败插件置 `BLOCKED`
- 主进程 `ProcessManager` 是连接管理器：非阻塞启动、健康检查（30s，三连败断连）、断线无限指数退避重连（1s~30s）；状态集 `SCANNED/CONNECTING/REGISTERING/ACTIVE/DISCONNECTED/BLOCKED/STOPPING/STOPPED`
- 文件传输：主进程在 ProxyTool 派发边界把 `file-ref` 标记参数（upload 目录内路径，fail-closed）改写为 `minio://` 引用（PUT 至 transfer bucket）；插件用自有受限 MinIO 账号经 SDK `resolve_file()` 下载；输出经 SDK `put_file()` 直传 + `storage.presign_get` 换下载链接
- 本地开发用 `scripts/dev-plugins.py` 拉起全部插件（读 `plugins/plugin.env`）；生产用 `Dockerfile.plugins` 多 target 镜像 + compose `plugin-*` 服务
- 工具代理 (`ProxyTool`) 将 JSON-RPC 调用暴露为 `Tool` 对象

**插件 vs Skill：** 插件提供原子工具（`type: tool`）。Skill 是 Markdown + frontmatter 定义的任务工作流，经 `SkillTool` + `AgentRuntime` 以子代理（subagent）/内联（inline）模式编排执行（见上文「会话模式」）。

**插件 vs 库：** 插件是可独立部署的服务（独立 TCP 进程），库是代码依赖（构建时打包）。库位于 `libs/`，插件位于 `plugins/`。

### CourtierConfig

`CourtierConfig` 是顶层平台配置，负责发现和加载领域包：

- `from_env()` 读取 `COURTIER_DOMAIN_PACKAGES`（默认 `"docaudit"`）和 `COURTIER_LOCALE`（默认 `"zh-CN"`）
- `discover()` 验证所有领域包，加载 `domain.yaml`、PromptBundle，构建 `DomainPackage`
- `build_prompt_engine()` 创建 PromptEngine：以 Core 默认模板（`prompts/defaults/{locale}/`，覆盖全部领域无关 key）为基底，再逐领域合并其专属模板与覆盖项

### DomainPackage

运行时领域表示，组合：
- `DomainConfig`（从 `config/domain.yaml` 验证的元数据）
- `plugins_path` / `skills_path` — 文件系统路径（`plugins_path` 指向仓库根目录的 `plugins/`）
- `PromptBundle` — 由 `PromptEngine` 加载的 YAML 模板集合：Core 默认（`courtier/prompts/defaults/{locale}/`，领域无关 key 的全量本地化文本）+ 领域包 `config/prompts/{locale}/`（领域专属与覆盖项）

## Git 提交规则

**每次修改代码后，必须使用 git 进行提交。**

- 修改完代码并确认无误后，立即执行 `git add` 和 `git commit`
- 遵循约定式提交消息格式（feat, fix, refactor, docs, test, chore, perf, ci）
- 不应等到积攒多个修改后才提交
- 如果用 worktrees 进行开发，完成工作后必须将 worktree 对应的分支合并到 main，并清理 worktree

## Python 环境管理

**使用 uv 管理 Python 包和虚拟环境。**

- 包管理：`uv add <package>` / `uv remove <package>`
- 运行脚本：`uv run <script>`
- 同步依赖：`uv sync`
- 升级包：`uv lock --upgrade-package <package>`

## 已知技术债务

- `src/` 下遗留模块已基本清理；`docmodels` 已从领域包上移至 `libs/shared/docmodels/`（可安装包，导入名不变）
- `courtier/agent/api/app.py` 中直接导入 `content_compliance.init_checkers()` 的泄漏已移除；初始化由 `content_audit` 插件延迟完成
- 可观测性配置（docker-compose、Prometheus、Grafana）已从 `docaudit` 品牌统一重命名为 `courtier`
- Dockerfile 引用路径已更新为 monorepo 结构（包含 `libs/` 和 `plugins/`），但尚未在 CI 中验证完整构建与前端静态文件服务
