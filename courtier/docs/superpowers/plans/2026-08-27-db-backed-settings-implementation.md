# 配置中心化（去 .env：DB 设置存储 + 前端系统设置页）实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 容器启动不再依赖 `.env`：应用读取的全部配置迁入数据库，经前端"系统设置"管理页（admin 权限）查看、校验、变更、测试连通性；不可消除的自举集（Tier 0，2~3 个容器 env）压到最小。配置变更按三档生效模型落地（热生效 / 保存后热重建 / 需重启，前端逐项标注）。

**Architecture:** 现有 pydantic `Settings` 继续作为唯一配置 schema；新增 `SettingsStore`（settings 表 + Fernet 加密 + 审计）与 `ConfigService`（快照 + 版本号 + EventBus 广播 + env-only 降级），替换 import 时单例与 8 处散装 `Settings()` 重读（顺带根治现存的配置多脑分裂）。读取优先级：默认值 < 部署层 env < DB 显式值（CORS 例外：env 永远覆盖 DB，作锁死逃生舱）。前端表单由后端导出的字段元数据（`SETTINGS_META`）驱动，secret 只写不读。首启无 `.env`：JWT secret 自动生成加密落库，管理员经 `/setup` 向导创建（取代 `ADMIN_USER/ADMIN_PASSWORD` 启动 bootstrap）。

**Tech Stack:** pydantic-settings（沿用）、SQLAlchemy 2 async + Alembic、cryptography（Fernet）、FastAPI、Vue 3。

**关联文档:** `2026-08-17-plugin-standalone-deployment-implementation.md`（"插件 env 不纳入"边界来源）与 `2026-08-27-plugin-de-llm-implementation.md`（**已实施**：插件侧 `LLM_*` 全部移除，检索向量/重排收归 host——本计划 B 类清单据此缩小，LLM 配置成为 host 单点）。

---

## 决策记录（已与用户确认，2026-08-27）

| # | 决策点 | 结论 | 被否方案 |
|---|--------|------|----------|
| 1 | 自举集 | **保留 Tier 0 容器 env（2~3 个 + 可选项）**：`MYSQL_URL`、`COURTIER_SETTINGS_KEY`（新增，DB 内 secret 加密密钥）、`DEPLOYMENT_ENVIRONMENT`；可选 `CORS_ORIGINS`（逃生舱）、`COURIER_SETUP_KEY`（生产首启向导令牌） | 向导把 DB URL 写入容器内本地文件（.env 换名复活，且容器重建即丢） |
| 2 | infra 容器密码 | **留部署层**：compose 给开发默认值（`${VAR:-dev默认}`），生产用 docker secrets/部署平台注入；应用侧连接凭据（ES/MinIO）在前端配，与 infra 的匹配关系写入运维文档 | 向导自动轮换 MySQL/MinIO/ES 密码（轮换中途失败的恢复逻辑复杂脆弱） |
| 3 | 插件自身 env | **不纳入前端管理**（与插件独立化决策 6 一致）：插件用 LLM key、OCR 地址、受限 MinIO 凭据等仍走 `plugins/plugin.env`/compose `environment`；仅 host 侧 `COURTIER_PLUGIN_ENDPOINTS/TOKEN` 入前端，改 token 时提示插件侧同步 | host→plugin 反向 RPC 配置下发（技术可行但推翻已锁定决策，跨机密钥分发需重设计） |
| 4 | 生效模型 | **三档分级**：热生效 / 保存后热重建（失败回滚旧快照）/ 需重启（前端标灰明示） | 全部热生效（CORS middleware、OTel provider、目录路径运行时可变化引入新故障面） |

自举矛盾（决策 1 的根据，讨论已对齐）：配置存 DB → 连 DB 的 URL 本身是配置，不能存 DB；加密密钥不能存进被加密的库；`main.py` 在 uvicorn 之前跑 Alembic，那一刻没有 HTTP/前端。故 Tier 0 在逻辑上不可消除，只能最小化。

---

## 目标设计

### 1. 配置分层与变量归属

现状 `.env` 变量分四类消费者（探索结论，2026-08-27）：

| 类 | 消费者 | 处置 |
|----|--------|------|
| A. 应用进程（`Settings`，约 90 字段） | host | **迁入 DB + 前端**（Tier 1），个别除外（见下） |
| B. 插件进程 | 独立 TCP 进程自读 env | 不动（决策 3）；host `.env.example` 中的纯插件变量删除，`plugins/plugin.env.example` 已承载 |
| C. infra 容器自身 | compose 启动时刻 | 留部署层（决策 2）：compose 加开发默认值 |
| D. 部署拓扑/文件系统 | 容器路径 | 留部署层（Tier 0/只读展示） |

**Tier 0 最终清单**（容器 env，部署时注入；`.env.example` 瘦身后仅含这些）：

| 变量 | 说明 |
|------|------|
| `MYSQL_URL` | 迁移与 DB 引擎自举 |
| `COURTIER_SETTINGS_KEY`（新增） | DB 内 secret 的 Fernet 密钥（`openssl rand -hex 32`）；丢失 = DB 内 secret 不可恢复，必须纳入部署备份 |
| `DEPLOYMENT_ENVIRONMENT` | development/staging/production，安全语义开关（Secure cookie、setup 门禁），属部署声明 |
| `COURTIER_REPO_ROOT` | Dockerfile 已固定 `/app`，不算 .env 项 |
| `CORS_ORIGINS`（可选，逃生舱） | **设置时永远覆盖 DB 值**——管理员把 CORS 改坏锁死时的救援通道 |
| `COURIER_SETUP_KEY`（可选） | 生产首启向导一次性令牌，见 §7 |

**Tier 1 归属（组级；字段级以 `SETTINGS_META` 注册表为准）**：

| 组 | 字段 | 生效档 |
|----|------|--------|
| 模型与上下文 | `llm_*`（base_url/api_key/model/采样参数/timeout/extra_body）、`llm_embedding_*`（host 侧）、`llm_context_window_tokens`、`context_*` | 热生效（下次 `build_agent` 自然读新快照） |
| 检索与存储 | `es_*`、`minio_*`（应用凭据与 bucket 名） | 热重建（client invalidate + 重建；`es_index_*`/embedding 维度变更需确认对话框） |
| 插件 | `courtier_plugin_endpoints`、`courtier_plugin_token` | 热重建（触发断开重连；token 是双侧共享，保存时弹"插件侧 env 需同步"警告 + 一致性探测按钮） |
| 运行守卫与预算 | `loop_*`、`subagent_*`、`run_log_*`、`run_grace_seconds`、`max_runs_per_user`、`max_total_runs` | 热生效（RunManager 订阅 `config.changed` 刷新限流字段） |
| 可观测性 | `otel_*`、`logger_level`、`audit_log_enabled` | logger_level/audit 开关热生效；`otel_*` 重启档（provider 进程级初始化） |
| Web 与安全 | `cors_*`、`jwt_algorithm`、`jwt_expire_seconds`、`jwt_access_expire_seconds`、`bcrypt_rounds` | JWT/bcrypt 热生效（每请求读快照）；CORS 重启档 + 逃生舱 |
| 安全凭据 | `jwt_secret` | 不暴露编辑框：首启自动生成 + "轮换"按钮（全员掉线重登，确认对话框） |
| 平台 | `courtier_locale`、`courtier_domain_packages` | 重启档（PromptEngine/领域发现启动时构建；领域启停已有 `setDomainEnabled` 通道） |
| 部署层只读展示 | `mysql_url`、`upload_dir`、`cache_dir`、`audit_log_dir`、`deployment_env`、`repo_root` | 前端只读展示"部署层配置"，不可编辑（路径运行中迁移危险） |
| 删除 | `cec_*` 四字段（探索确认无任何消费者）。~~embedding env 名 description 修正~~（已随去 LLM 化完成） | — |

不迁入 host 前端的插件变量（B 类，从 host `.env.example` 删除、保留在 `plugins/plugin.env.example`）：`DOCPARSE_OCR_*`、`ANYDOC_OCR_API_URL`、`FONT_MODEL_*`、search 插件的 `ES_*` 与检索行为 `SEARCH_*`（KNN_K/MAX_WINDOW/NEIGHBOR_WINDOW/CACHE_*）、插件受限 `MINIO_*`、`COURTIER_PLUGIN_LISTEN/WORKDIR`。**修订（去 LLM 化已实施）**：插件 `LLM_*`/`LLM_EMBEDDING_*`/`SEARCH_RERANK_FETCH`/`SEARCH_CANDIDATE_BUDGET_CHARS` 已不存在——后两者成为 host settings（`search_rerank_fetch`/`search_rerank_candidate_budget_chars`，热生效组），embedding 索引/查询两侧已统一 host 单点。

**infra（C 类）compose 改造**：`MYSQL_ROOT_PASSWORD`、`MINIO_ROOT_*`、`ES_PASSWORD`、`LANGFUSE_*`、`GRAFANA_PASSWORD` 全部加开发默认值（`${VAR:-courtier-dev-...}`）；生产推荐 docker secrets 注入，写入运维文档。app 服务 env 缩减为 Tier 0。

### 2. 存储模型（settings 表 + 加密 + 审计）

```sql
CREATE TABLE settings (
  `key`        VARCHAR(190) PRIMARY KEY,   -- Settings 字段名（如 llm_api_key）
  `value`      JSON NOT NULL,              -- 标量包 JSON；is_secret=true 时为 {"enc": "<fernet密文>"}
  `is_secret`  BOOLEAN NOT NULL DEFAULT FALSE,
  `category`   VARCHAR(50) NOT NULL,
  `updated_by` VARCHAR(64) NOT NULL,
  `updated_at` DATETIME NOT NULL,
  `seq`        BIGINT NOT NULL AUTO_INCREMENT, UNIQUE KEY (`seq`)  -- 全局版本号
);

CREATE TABLE settings_changes (             -- 审计：谁、何时、改了哪个 key（不记明文）
  `id`          BIGINT AUTO_INCREMENT PRIMARY KEY,
  `key_name`    VARCHAR(190) NOT NULL,
  `old_hash`    CHAR(64) NULL,              -- sha256；secret 亦只记哈希
  `new_hash`    CHAR(64) NULL,
  `actor`       VARCHAR(64) NOT NULL,
  `created_at`  DATETIME NOT NULL
);
```

- **加密**：Fernet（密钥 = `COURTIER_SETTINGS_KEY`，hex 32B 派生）。**密钥未设置 → secret 字段保存直接 422 拒绝**（fail-closed，非 secret 配置不受影响）；读取遇到解不开的密文 → 该值视为"已配置但不可读"，日志告警，不回退明文。
- **回显脱敏**：GET 永不返回明文，secret 字段只返回 `{"set": true, "tail": "abcd"}`；PUT 语义：省略 = 不改，字符串/null = 设置/清除。
- 写路径：分组 partial update → 应用到当前快照 → **整模型 pydantic 校验**（复用现有 validators，含 `plugin_endpoints()` 格式严格校验）→ 差异落库（仅变更 key）→ 审计行 → 新快照发布。

### 3. ConfigService（统一配置源，根治配置多脑）

现状三脑：import 时单例（`config.py:690`）、`app.state.settings`、8 处临时 `Settings()`（`db.py`、`es/client.py`、`storage/client.py`、`alembic/env.py`、脚本等）。目标：**进程内唯一 ConfigService**，其余全部消费它。

- `snapshot: Settings`（当前生效配置）+ `version: int`（= settings 表最大 seq）；`get_settings()` 保留为兼容入口、内部委托 ConfigService（调用点渐进迁移，Phase 0 完成）。
- 快照合成：默认值 < 部署层 env（含 `.env` 文件——向后兼容）< DB 显式值；CORS_ORIGINS 反转（env 覆盖 DB）。
- **env-only 降级**：`MYSQL_URL` 为空或 DB 不可达 → 快照仅由 env/默认值合成，配置面只读，settings API 返回降级标记。这同时保证测试体系不变（conftest 即 `MYSQL_URL=""`）。
- **变更发布**：保存 → 新快照 → 进程内 EventBus 广播 `config.changed{version}` → 订阅者（RunManager 限流字段、ES/MinIO client 失效标记、PluginSystem 重连）各自刷新。
- **`.env` 种子导入**：首次检测 settings 表为空且能从 env/.env 读到非默认值 → 全量写入 DB（secret 加密）→ 记 `seed_imported_at`。此后 `.env` 仍可存在（未覆盖项作为兜底），但设置页对每项显示**来源标记**（DB/env/默认/已被 DB 覆盖），杜绝"改了 .env 怎么不生效"的困惑。
- **JWT secret 自举**：DB 模式下快照合成发现 `jwt_secret` 空且 DB 无记录 → `secrets.token_urlsafe(48)` 生成、加密落库、继续启动（生产不再要求 env 提供 JWT_SECRET）。

### 4. 生效模型（三档，前端逐项标注）

| 档 | 机制 | 失败处理 |
|----|------|----------|
| 热生效 | 下次请求/下次 `build_agent`/订阅者刷新自然读到新快照 | pydantic 校验前置，无运行时失败面 |
| 热重建 | 保存 → client invalidate → 用新快照重建（ES client、MinIO client、PluginSystem 断开重连） | 重建探活失败（list_buckets/ping/拨号）→ **自动回滚上一快照**并报错，前端红条提示 |
| 需重启 | 前端标灰 + 保存后顶部横幅"N 项待重启生效"；不改运行时行为 | — |

每项的档位在 `SETTINGS_META` 注册（组级默认 + 字段级覆盖）。

### 5. 管理 API（`/api/admin/settings`，全部 `require_admin`）

| 端点 | 作用 |
|------|------|
| `GET /api/admin/settings` | 返回分组 schema + 当前值（secret 脱敏）+ 来源标记 + 生效档 + 降级/待重启状态 |
| `PUT /api/admin/settings/{category}` | 分组 partial 更新（语义见 §2）；返回 `{applied, rebuilt, restart_required: [...]}` |
| `POST /api/admin/settings/test/{target}` | 连通性测试（**后端发起**，避免浏览器 CORS/内网泄露）：`llm`（1 token 补全）、`minio`（list_buckets）、`es`（ping）、`plugins`（逐端点 TCP 拨号探测） |
| `POST /api/admin/settings/reload` | 手动触发热重建（等价于保存后自动流程，排障用） |
| `GET /api/admin/settings/audit` | 变更历史分页 |
| `POST /api/admin/settings/jwt/rotate` | JWT secret 轮换（确认语义：全部会话失效） |
| `GET /health` 扩展 | 增加 `config: {mode: db|env_only, setup_required, seed_imported_at, version}` |
| `POST /api/setup/*` | 首启向导（仅 DB 无 admin 时可用，见 §7） |

### 6. 前端设计

- **系统设置页**：`AdminLayout` 侧栏新增"系统设置"（`/admin/settings`，`SystemSettings.vue`）；左侧分组导航对应 §1 的组；表单由 `GET` 返回的字段元数据驱动渲染（轻量 SchemaForm 组件：string/int/float/bool/enum/json/list 六种控件，**不做完整 JSON Schema 标准**）；secret 掩码输入（placeholder "已配置（••••abcd）"，留空不改）；每项徽章：生效档 + 来源；组级"保存"+"测试连接"；保存后全局横幅汇总待重启项。
- **首启向导**：DB 无 admin 时全局路由守卫重定向 `/setup`（`SetupView.vue`）：步骤 1 创建管理员（用户名/密码，取代 env bootstrap）→ 步骤 2 关键配置引导（LLM key 等必填检查，可跳过）→ 完成。后端 `POST /api/setup/admin` + `POST /api/setup/complete`，完成即永久关闭（有 admin 后 404）。
- **插件 token 修改**：保存前弹警告"token 为 host/插件双侧共享，插件侧 env 需同步修改，保存后插件将断连重试"；提供"测试全部插件连接"。

### 7. 安全设计

- **生产首启防抢占**：`/api/setup` 在 development 下直接可用；staging/production 下需满足其一——请求来自 localhost/内网网段、或携带 `COURIER_SETUP_KEY`（Tier 0 可选 env，首次成功使用后置已用标志）。DB 已有 admin 时一律 404。
- **守门迁移**：现有 production validator（`config.py:475-487` 在 `Settings()` 构造时 raise，要求 env 提供 JWT_SECRET/ADMIN_PASSWORD）**删除**，改为运行时状态机：`unconfigured`（无 admin）状态下除 `/api/setup`、`/health`、静态资源外全部 403 `setup_required`；否则正常。这样无 `.env` 的生产首启才能进得去向导。
- **bootstrap 处置**：删 `bootstrap_admin_user()`（`db.py:42`）与 env `ADMIN_USER/ADMIN_PASSWORD`；`_fallback_login`（无 DB 开发模式登录）保留但仅限 development + env-only 模式，读快照同名字段。
- **CORS 防锁死**：Tier 0 `CORS_ORIGINS` 覆盖语义（决策 1）+ 保存 CORS 变更强制确认对话框 + 后端永久放行同源与回环。
- **审计**：§2 的 settings_changes 表；`GET .../audit` 仅 admin。

### 8. 兼容与迁移

- **Alembic**：`env.py` 读 Tier 0 `MYSQL_URL`（现状即经 Settings，保持 env-only 读取路径）；settings 两张表用新迁移建，先于种子导入。
- **测试**：env-only 降级保证现有 conftest（`MYSQL_URL=""`）零改动；插件测试的 env 用法不受影响（B 类不动）。新增：SettingsStore 加密/脱敏/审计、ConfigService 优先级与降级、守门状态机、API、前端 SchemaForm。
- **脚本**：`scripts/retrieval_eval.py`、`scripts/minio_plugin_io.py` 等直读 env 的脚本在 env-only 语义下照常工作（不动；DB 模式下脚本如何读 DB 配置留待实际需要时再议，记入"明确不做"）。
- **运维文档**：`COURTIER_SETTINGS_KEY` 备份告警（丢失=secret 不可恢复）；infra 密码与应用侧 DB 配置的匹配说明（决策 2 的配套）；compose 生产 secrets 注入示例。

---

## 文件职责

| 对象 | 操作 | 阶段 |
|------|------|------|
| `courtier/config.py` | 删 import 单例与 `_validate_production_secrets`；`cec_*` 删除；embedding env 名 description 修正；新增 `SETTINGS_META`（category/is_secret/effect/label 注册表）；`get_settings()` 委托 ConfigService | 0/1 |
| `courtier/settings_store.py`（新） | 表模型、Fernet 编解码、读写、种子导入、审计写入、密钥缺失 fail-closed | 1 |
| `courtier/agent/api/services/config_service.py`（新） | 快照合成/版本/EventBus 发布订阅/降级/热重建编排/连通性测试执行器/JWT 自举 | 0（env-only 版）→ 1（DB 版） |
| `courtier/agent/telemetry/tracer.py` | 直读 `os.getenv` 收编为快照读取；`_RESOURCE` 移入 `init_telemetry()` | 0 |
| `courtier/es/client.py`、`courtier/storage/client.py` | invalidate/重建钩子（双检锁单例改为可失效 holder） | 0 |
| `courtier/plugin/manager.py` | endpoints/token 变更 → 断开重连（复用退避重连机制） | 2 |
| `courtier/agent/api/services/run_manager.py` | 订阅 `config.changed` 刷新限流字段（构造时拷贝改为可刷新） | 0 |
| `courtier/agent/api/app.py` | lifespan 集成 ConfigService；`unconfigured` 守门中间件；`/health` 扩展；CORS env 覆盖逻辑 | 0/2/3 |
| `courtier/agent/api/routes/admin_settings.py`（新） | §5 管理 API | 1/2 |
| `courtier/agent/api/routes/setup.py`（新）+ `db.py` | 首启向导端点；删 `bootstrap_admin_user` | 3 |
| `alembic/versions/`（新迁移） | settings + settings_changes 两表 | 1 |
| `webui/src/views/admin/SystemSettings.vue`、`components/admin/SchemaForm.vue`、`views/SetupView.vue`、router、`api/client.ts` | §6 前端 | 1/3 |
| `courtier/.env.example` | 瘦身为 Tier 0 + 注释指向设置页；infra 变量移交 compose 默认值 | 3 |
| `docker-compose.yml` | infra 服务加开发默认值；app 服务 env 缩为 Tier 0 | 3 |
| `AGENTS.md`、根 `CLAUDE.md`、`docs/operations/` | §9 部署、§7.1 约定、密钥备份与匹配说明更新 | 3 |

---

## Phase 0：配置源统一（纯重构，零行为变化）

### Task 0.1: ConfigService env-only 版 + 消灭散装 Settings()
- [x] 新建 ConfigService（快照 + 版本 + 订阅口，本阶段数据源仅 env/.env），`get_settings()` 委托
- [x] 改 8 处临时 `Settings()` 调用点（`app.py`、`db.py`、`es/client.py`、`storage/client.py`、`loop_guards`、`guardrails`、`plugin/manager.py`、`middleware/auth.py` 的单例读取）统一走 ConfigService
- [x] 删 `cec_*` 字段（删前全仓 grep 复核无消费者）；修 embedding env 名 description
- [x] 回归：`uv run pytest -m "not integration"` 全绿，行为零变化

### Task 0.2: OTel 收编 + 可失效钩子
- [x] `tracer.py` 的 `os.getenv` 全部改读快照；`_RESOURCE` 移入 `init_telemetry()`
- [x] ES/MinIO client 加 invalidate（本阶段无触发方，仅挂钩子 + 单测）；RunManager 加 `refresh_limits()`（订阅 `config.changed`）
- [x] 回归全绿

## Phase 1：存储与管理面（热生效组）

### Task 1.1: settings 表 + SettingsStore
- [x] Alembic 迁移（settings/settings_changes）；Fernet 编解码 + 密钥缺失 fail-closed 单测；审计写入单测

### Task 1.2: ConfigService DB 集成
- [x] 快照合成三级优先级 + CORS env 覆盖例外；env-only 降级路径单测
- [x] `.env` 种子导入 + 来源标记；JWT secret 自举（DB 模式）
- [x] `config.changed` 发布与订阅者刷新（RunManager）单测

### Task 1.3: 管理 API（热生效组）
- [x] `GET`（脱敏 + schema + 来源 + 档位）、`PUT`（partial + 整模型校验 + 审计）、`test/llm`、`audit`；`require_admin`
- [x] API 测试（含校验失败 422 字段级报错、secret 留空不改、null 清除）

### Task 1.4: 前端系统设置页（热生效组）
- [x] SchemaForm 组件 + SystemSettings.vue（模型与上下文/守卫预算/可观测性日志项）+ api client 扩展 + 路由
- [x] `npm test` / `npm run build` 绿；SchemaForm 单测

## Phase 2：连接类与安全

### Task 2.1: MinIO/ES/插件配置热重建
- [x] 三类 client 接入热重建编排：**保存前探活（失败不落库）** → 保存 → invalidate ES/MinIO 单例 → 插件断开重连（manager 重解析 + restart_plugin）
- [x] `es_index_*`/embedding 维度变更确认对话框（索引重建影响）；插件 token 双侧警告（确认弹窗）；保存后 `revalidated` 回执
- [x] `test/minio`、`test/es`、`test/plugins` 端点（与 test/llm 合并为 `/test/{target}`）

### Task 2.2: 安全项
- [x] JWT 轮换端点（会话全失效语义 + 前端确认）；CORS env 逃生舱（合成侧已实现）+ 前端确认对话框
- [x] production validator 删除 + `unconfigured` 守门中间件（`setup_gate.py`：staging/production 且无 admin → /api/* 除 /health、/api/setup* 外 403 setup_required；非 API 路径放行供向导 SPA 加载）

### Task 2.3: Phase 2 测试
- [x] 保存前探活失败不落库 / 成功失效对应 client / 插件配置变更触发重连 / test 端点 / JWT 轮换三路径 / 守门五路径（production 拦截、setup/health/静态放行、admin 存在放行、dev 不拦、env-only 不拦）；CORS 逃生舱由合成单测覆盖

## Phase 3：首启体验与文档

### Task 3.1: setup 向导
- [ ] 后端 `/api/setup/*`（admin 创建 + 完成标记 + production 门禁：内网或 `COURIER_SETUP_KEY`）；删 `bootstrap_admin_user` 与 `ADMIN_USER/ADMIN_PASSWORD`
- [ ] 前端 SetupView + 全局路由守卫；`/health` config 状态

### Task 3.2: 配置瘦身与部署制品
- [ ] `.env.example` 瘦身为 Tier 0；compose infra 默认值 + app env 缩减；audit_log/upload/cache 等部署层项前端只读展示
- [ ] `AGENTS.md`（§4/§7.1/§9/§10）、根 `CLAUDE.md`、`docs/operations/`（密钥备份、infra 匹配、生产 secrets 注入）更新

### Task 3.3: Phase 3 测试
- [ ] setup 状态机测试（development 直通/生产门禁/完成后 404）；audit 前端面板

## Phase 4：全量回归与真实验收

### Task 4.1: 全量回归
- [ ] `uv run pytest -m "not integration"` 全绿；`uv run courtier validate-domain domains/docaudit/`；webui `npm test`/`npm run build`

### Task 4.2: 真实验收（对照总验收清单逐项）
- [ ] 删 `.env` 容器启动 → 向导 → 设置页全流程；热生效/热重建/重启三档实测；CORS 锁死救援演练；密钥错误演练；老环境种子导入实测

---

## 提交切分（约定式）

1. `refactor(config): unify settings source behind ConfigService`（Task 0.1）
2. `refactor(telemetry): route otel config through settings snapshot`（Task 0.2 前半）
3. `refactor(es,storage,run-manager): invalidatable clients and limit refresh`（Task 0.2 后半）
4. `feat(settings): db-backed settings store with encryption and audit`（Task 1.1）
5. `feat(settings): config service db integration, seed import, jwt bootstrap`（Task 1.2）
6. `feat(api): admin settings api for hot-reload groups`（Task 1.3）
7. `feat(webui): schema-driven system settings page`（Task 1.4）
8. `feat(settings): hot rebuild for storage, search and plugin connections`（Task 2.1）
9. `feat(security): jwt rotation, cors escape hatch, setup gate`（Task 2.2/2.3）
10. `feat(setup): first-run wizard replacing env admin bootstrap`（Task 3.1）
11. `chore(env): shrink env template to tier-0 and document ops`（Task 3.2）

## 总验收

1. 删除 `.env`，容器仅凭 Tier 0 env 启动，`/health` 报 `mode=db, setup_required` → 向导建 admin → 正常使用
2. 全部 Tier 1 配置前端可读写，重启后持久；每项显示来源与生效档
3. 热生效组：改 LLM key，下一会话即用新 key（日志验证）
4. 热重建组：改 MinIO 凭据 → 保存自动重连成功；故意填错 → 回滚旧快照 + 红条报错
5. 重启档：改 CORS → 标灰提示重启；把 DB 的 CORS 改坏 → Tier 0 env 救援恢复访问
6. 密钥演练：换 `COURTIER_SETTINGS_KEY` → secret 显示"已配置但不可读"且报清晰错误，非 secret 配置不受影响
7. 老环境升级：带 `.env` 首启 → 种子导入 → 设置页显示来源=db
8. 插件：改 endpoints 保存 → 插件断开重连，工具恢复注册；改 token 弹双侧警告
9. 全量测试绿 + webui build 绿 + validate-domain 通过

## 明确不做的事

- 插件自身 env 的前端管理 / host→plugin 配置下发（决策 3，边界来自插件独立化决策 6）
- infra 容器密码的向导轮换（决策 2）
- OTel provider、CORS middleware 的运行时热切换（决策 4；仍为重启档）
- `upload_dir`/`cache_dir`/`audit_log_dir` 的前端可编辑（部署拓扑，只读展示）
- 完整 JSON Schema 标准表单（自用轻量 SchemaForm 足够）
- Fernet 多密钥轮换机制（单密钥 + "丢失即重录"语义）
- DB 模式下 scripts 如何读 DB 配置（env-only 语义照旧，有实际需要再议）
- `MYSQL_URL` 之外的 DB 类配置入库（自举矛盾，永远 Tier 0）

## 实施记录

**Task 0.1 完成**（ConfigService env-only + 散装 Settings() 清零）。偏差与实测：
1. **ConfigService 落在 `courtier/config.py` 本模块**，不是计划写的 `courtier/agent/api/services/config_service.py`——config 是 core 层，services 反向 import config，放 services 会层次倒置且有循环导入风险；服务实例 `_config_service` 模块级 + import 时急切构建（保持旧单例"导入即校验"语义），`get_settings()` 委托。
2. 调用点归一实测：散装 `Settings()` 构造 8 处（config 单例、es/storage 惰性 helper、app 工厂、db 引擎/admin bootstrap、alembic、reindex 脚本）全部改读共享快照；`loop_guards`/`guardrails`/`plugin/manager`/`auth` 本就走 `get_settings()`，委托后自动获得未来热替换能力。
3. `cec_*` 五个字段删除前全仓 grep 确认零消费者（含 libs/plugins 的 CEC_ env 直读）。
4. 测试坑：`tests/courtier/test_config.py` 会 `importlib.reload` config 模块，跨模块缓存的 `Settings` 类引用会过期——新测试 `test_config_service.py` 在调用点实时 `import courtier.config` 做 isinstance 断言。`replace()`/`subscribe()` 的契约（顺序通知、异常隔离、自退订安全）已先行单测固化。
5. `db/_utils.py` 的 `_DEFAULT_DB_URL`（import 时读 env）保留——save_doc 直连路径的 Tier-0 语义，Phase 1 ConfigService DB 集成时一并处理。
6. 实测：`pytest -m "not integration"` 1757 passed / 6 skipped；ruff 绿。

**Task 0.2 完成**（OTel 收编 + 热重建钩子）。偏差与实测：
1. RunManager 的刷新方法落地为 **`apply_settings(settings)`**（计划名 `refresh_limits`）：除重拷限流字段外还把 `self._settings` 指向新快照——后续每次 run 的审计开关/模型配置等 getattr 读到新值，热生效面更大且无额外成本；app 工厂经 `get_config_service().subscribe(...)` 接线（新增公开访问器 `get_config_service()`，订阅句柄存 `app.state._config_unsubscribe`）。
2. `invalidate_es_client()` 顺带清 `_write_index_cache`（别名解析缓存）——换端点后别名解析不应残留；MinIO 客户端无打开资源，仅置空引用。
3. tracer 收编后 OTel 四个旋钮（service.name/环境/endpoint/log_level）全部来自快照；新增测试断言 provider resource 来自 settings 而非 env。
4. 实测：`pytest -m "not integration"` 1764 passed / 6 skipped；ruff 绿。**Phase 0 完成——配置读取单一化，为 Phase 1 的 DB 快照替换铺平。**

**Task 1.1 完成**（settings 表 + SettingsStore + SETTINGS_META）。偏差与实测：
1. **`settings.seq` 列取消**——sqlite 方言只对 INTEGER PRIMARY KEY 自增（BIGINT 非 PK 不行，store 单测无法移植）；全局版本改用 `MAX(settings_changes.id)`（审计表 PK 两方言都自增），新增 `SettingsStore.current_version()`。
2. **顺手删除 host Settings 的 `docparse_*`（10 字段）与 `font_model_*`（3 字段）镜像字段**——去 LLM 化后 host 侧零消费者（插件进程读自己的 env），归属 plugin.env；比计划仅删 `cec_*` 的范围略大。
3. **SETTINGS_META 从 `Settings.model_fields` 规则派生**（前缀→分组 + 显式 secret/rebuild/restart 集合），不逐字段手写表；Tier-0 字段与 `agent_runtime` 嵌套块排除。实测：65 个可编辑字段（model 19/retrieval 16/guards 16/web 7/observability 5/plugins 2），6 个 secret，hot 44/rebuild 16/restart 5。
4. 迁移守卫测试改为**版本无关的单 head 断言**（原测试硬编码当时的 head，新增迁移必红）。dev 依赖加 `aiosqlite`（store 单测用文件型 sqlite）。
5. 真实验证：迁移 `e5c90b1a7d42` 已对本地 dev MySQL 应用（两表建成）；store 真库冒烟（明文保存/读取/版本=2/清理）通过。回归 `pytest -m "not integration"` 1778 passed。

**Task 1.2 完成**（ConfigService DB 集成）。偏差与实测：
1. **种子导入修复一个实现期 bug**：secret 字段最初被无条件跳过，正确语义为"有加密密钥则一并种子导入、无密钥才跳过（整批不因它 fail-closed）"——单测抓出后修正。
2. 快照合成实现在 `settings_store.py`（`compose_snapshot`/`seed_from_env`/`refresh_settings_snapshot`），不在 config.py——合成需要异步 DB 访问，config.py 保持纯同步核心；`ConfigService` 增加 `source`（env|db）标记。`Settings.model_config` 增加 `populate_by_name`（model_dump 按字段名合成验证所需）。
3. 种子导入的"非默认值检测"用**环境名掩蔽上下文**构造纯默认实例对比（`.env` 文件也掩蔽），只导入真实差异项；本地实测 dev .env 会导入 ~7 项（LOGGER_LEVEL 等）——行为符合预期（.env 固化进 DB）。单测用 `_env_file=None` 基线隔离本地 .env。
4. JWT 自举：DB 模式 + 空 jwt_secret + 有加密密钥 → 生成 token_urlsafe(48) 加密落库；无密钥 → 告警跳过（env 值继续生效）。测试坑：本地 .env 有 JWT_SECRET，单测须显式 `JWT_SECRET=""` 才走自举分支。
5. lifespan 接线：mysql_url 非空 → 建 `app.state.settings_store` + `refresh_settings_snapshot`；为空 → `settings_store=None`（env-only）。降级（DB 不可达）不换快照只告警。
6. 实测：新增 15 个合成/种子/自举/降级单测；全量 1793 passed。

**Task 1.3 完成**（管理 API）。偏差与实测：
1. PUT 的"null=清除回退 env/default"需要 `SettingsStore.delete()`（删行 + 审计行 new_hash=None）——计划遗漏的存储原语，已补。
2. 校验失败返回 422 + 字段级 errors（pydantic ValidationError 提取 loc/msg）；PUT 成功后经 `refresh_settings_snapshot` 原子换快照并返回 `restart_required`（本次变更中 effect=restart 的字段）。
3. `test/llm` 支持请求体携带 llm_* 覆盖（保存前先测），lazy import 后端便于测试打桩。
4. 测试用独立 FastAPI app（只挂 settings 路由 + require_admin 依赖覆写 + sqlite store），并把路由模块的 `get_config_service`/`get_settings` 指到每测独立的 ConfigService——避免污染进程级全局快照（GET/PUT 必须读到同一个 service）。
5. 实测：12 个 API 用例（分组/掩码/来源标记/设置+清除/restart 上报/422/404/503/审计/LLM 测试成败两路）；全量 1805 passed。

**Task 1.4 完成**（前端系统设置页）。偏差与实测：
1. **SchemaForm 不做独立组件**——表单逻辑全部沉到纯模块 `webui/src/utils/settingsForm.ts`（initFormState/secretPlaceholder/buildUpdateBody：掩码 secret、仅变更提交、null=清除、int/float/bool/list/json 强转与错误收集），`SystemSettings.vue` 按类型分叉渲染（six 种控件内联）。纯模块可用现有 node 脚本测试模式（tsc 编译到 .tmp 导入）覆盖，组件保持薄。
2. 模板坑两则：分组 tab 的状态键是 `formState[activeCategory][field.name]`（非 `formState[field.name]`）；`string|boolean` 联合与 textarea/number input 的 v-model 类型不合，string 类字段走 `:value` + 类型化 `@input` 处理器（模板内 `as` 断言可用）。
3. 顶部加载不可用顶层 await（路由视图无 Suspense）——onMounted 加载 + loading/error 分支。
4. 页面能力：分组 tab、来源/生效徽章、secret 掩码占位、清除勾选（仅 source=db 字段显示）、组级保存横幅（applied/cleared/restart_required）、模型组"测试 LLM 连接"（带未保存覆盖值）、env-only 模式禁用编辑。
5. 实测：`npm test` 15 脚本全过（新增 test-settings-form.mjs：初始态/占位/变更语义/清除语义/五种强转错误）；`npm run build` + eslint 绿。**Phase 1 完成。**

**Task 2.1/2.2/2.3 完成**（Phase 2）。偏差与实测：
1. **热重建语义改为"保存前探活"**：PUT 在落库前用 prospective 快照探测（ES ping / MinIO list_buckets / 插件端点 TCP 拨号），失败 → 422"连接测试失败，未保存"，坏值永不入库——比计划原文"保存→重建→失败回滚"更安全（无回滚窗口、重启也不会复坏值）。保存成功后 invalidate ES/MinIO 单例（下次使用即重建）、插件经 manager 重解析 + restart_plugin 重连。前端 test 按钮携带未保存的表单值（保存前先测）。
2. 插件配置变更的重连语义：restart 全部插件连接（token 变更影响所有插件；端点变更个别插件断开后退避重连自动用新值）。确认弹窗提示"插件侧 env 需同步修改"。
3. `unconfigured` 守门落在独立模块 `setup_gate.py`（可单测的 install_setup_gate + admin_exists）；admin 存在性在 lifespan 启动时算一次存 `app.state._has_admin`，Phase 3 的 setup 端点完成后翻 True。production validator（import 时 raise）删除——首启才能进得去向导。
4. JWT 轮换端点要求加密密钥存在（422），env-only 503；前端 web 组带确认弹窗与结果提示。
5. 实测：Phase 2 新增 17 个后端用例 + 前端确认/测试/轮换交互；全量 1822 passed / 6 skipped；webui build+lint+15 脚本绿。

（其余 Task 待实施；按 Task 记录偏差、实测与排障。）
