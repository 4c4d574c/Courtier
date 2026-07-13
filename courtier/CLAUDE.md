# Courtier Platform — 项目规则

## Monorepo 布局

```
courtier/                          # 项目根目录
├── courtier/                      # Courtier 通用引擎（领域无关）
│   ├── agent/                    # Agent 运行时（loop, tools, skills, api, artifacts）
│   ├── config.py                 # Settings + CourtierConfig
│   ├── db/                       # 数据库层（CRUD, 表定义）
│   ├── domain/                   # DomainPackage loader
│   ├── plugin/                   # JSON-RPC 插件系统（SDK, registry, types）
│   └── prompts/                  # Jinja2 PromptEngine + PromptBundle
├── domains/
│   └── docaudit/                 # 公文审计领域包
│       ├── config/               # domain.yaml + prompts/{locale}/
│       ├── docmodels/            # 领域数据模型
│       └── skills/               # Skill 文档（Markdown + YAML frontmatter）
├── libs/
│   ├── shared/                   # 跨领域共享库
│   │   ├── docparse/             # 文档解析
│   │   └── docannot/             # 文档标注
│   └── docaudit/                 # docaudit 领域专属库
│       ├── validator/            # 格式校验
│       ├── content_compliance/   # 内容合规
│       └── doccorrector/         # 文本纠错
├── plugins/
│   ├── shared/                   # 跨领域共享插件
│   │   ├── parse/                # 文档解析工具
│   │   ├── search/               # 搜索工具
│   │   ├── annotate/             # 标注工具
│   │   └── template/             # 模板工具
│   └── docaudit/                 # docaudit 领域插件
│       └── audit/                # 审计包装插件
│           ├── format_audit/
│           ├── content_audit/
│           ├── text_correction/
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
PYTHONPATH=courtier:libs/shared:libs/docaudit \
  uv run python -m uvicorn courtier.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000

# 运行所有测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/agent/api/test_routes.py -v

# 排除集成测试
uv run pytest -m "not integration"

# 数据库迁移
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
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
2. **服务层** (`courtier/agent/api/services/`): AgentService, StreamService, FileService, SessionService
3. **核心引擎层** (`courtier/agent/`): Agent loop, PluginSystem, ToolRegistry, Artifact 系统, Context 管理, Prompt pipeline, Hooks, Permissions, Telemetry
4. **领域/业务层**:
   - `libs/shared/`: docparse, docannot 等跨领域共享库，被共享插件调用
   - `libs/docaudit/`: validator, content_compliance, doccorrector 等领域专属库，被 docaudit 插件调用
   - `plugins/shared/`: parse, search, annotate, template 等跨领域共享插件（JSON-RPC 子进程）
   - `plugins/docaudit/audit/`: format_audit, content_audit, text_correction, plagiarism 等领域专属插件（JSON-RPC 子进程）

### 插件系统（JSON-RPC 2.0）

插件是独立的子进程，通过 stdio 上的 JSON-RPC 2.0 通信：

- 共享插件位于 `plugins/shared/`（parse, search, annotate, template）
- 领域插件位于 `plugins/<domain>/`（如 `plugins/docaudit/audit/`）
- 每个插件有独立 `.venv`、`pyproject.toml`、`plugin.yaml`
- 插件声明 `entry.py:main` 作为入口点
- `PluginRuntime` SDK 提供 `register_capabilities()`, `register_tool()`, `run()`
- 宿主进程通过 `PluginProcess` 管理子进程生命周期
- 工具代理 (`ToolProxy`) 将 JSON-RPC 调用暴露为 `Tool` 对象

**插件 vs Skill：** 插件提供原子工具（`type: tool`）。Skill 定义通过 `load_skill` 编排工具的任务工作流。

**插件 vs 库：** 插件是可独立部署的服务（JSON-RPC 子进程），库是代码依赖（构建时打包）。库位于 `libs/`，插件位于 `plugins/`。

### CourtierConfig

`CourtierConfig` 是顶层平台配置，负责发现和加载领域包：

- `from_env()` 读取 `COURTIER_DOMAIN_PACKAGES`（默认 `"docaudit"`）和 `COURTIER_LOCALE`（默认 `"zh-CN"`）
- `discover()` 验证所有领域包，加载 `domain.yaml`、PromptBundle，构建 `DomainPackage`
- `build_prompt_engine()` 创建合并所有领域模板的 PromptEngine

### DomainPackage

运行时领域表示，组合：
- `DomainConfig`（从 `config/domain.yaml` 验证的元数据）
- `plugins_path` / `skills_path` — 文件系统路径（`plugins_path` 指向仓库根目录的 `plugins/`）
- `PromptBundle` — 由 `PromptEngine` 从 `config/prompts/{locale}/` 加载的 YAML 模板

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

- `src/` 下遗留模块已基本清理；`docmodels` 已迁移至 `domains/docaudit/docmodels/`
- `courtier/agent/api/app.py` 中直接导入 `content_compliance.init_checkers()` 的泄漏已移除；初始化由 `content_audit` 插件延迟完成
- 可观测性配置（docker-compose、Prometheus、Grafana）已从 `docaudit` 品牌统一重命名为 `courtier`
- Dockerfile 引用路径已更新为 monorepo 结构（包含 `libs/` 和 `plugins/`），但尚未在 CI 中验证完整构建与前端静态文件服务
