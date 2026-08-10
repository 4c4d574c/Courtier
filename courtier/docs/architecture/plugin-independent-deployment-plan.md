# 插件独立部署与 libs 依赖治理：架构演进方案

> 本文档针对 `libs/` 同时被 `domains/` 与 `plugins/` 依赖所形成的错位依赖网，给出插件走向独立部署的分阶段演进方案。范围覆盖 `courtier/plugin/`、`libs/`、`plugins/`、`domains/docaudit/` 与 `courtier/db/`。
>
> **核心判断**（已与作者确认）：插件运行时的**通信边界是干净的**（JSON-RPC 2.0 over stdio + host service 反向 RPC，见 §1.6），风险全部集中在 **Python 包依赖面**——现状下插件无法脱离 courtier 应用单独构建、测试与部署，"插件独立部署"在当前依赖网下等于整体搬运宿主应用。
>
> 本文档仅为方案，不包含实施。各阶段实施后以滚动状态表补充验收记录（格式参照 `context-management-optimization.md`）。

---

## 实施状态（滚动更新）

| 阶段 | 状态 | 关键改动 | 验收 |
|---|---|---|---|
| 阶段一：libs 包化 + 独立 Plugin SDK 包 | ✅ 已落地（2026-08-10） | 新包 `libs/shared/plugin_sdk/`（courtier-plugin-sdk 0.1.0，仅依赖 pydantic；含 protocol/runtime/storage/context_manager/types/models/templates）；6 个 libs 包补 `pyproject.toml`（docparse/docannot/docmodels/validator/content_compliance/doccorrector，`<name>/<name>/` 双层目录，导入名不变）；8 个插件 pyproject 弃 `dependencies = ["courtier"]`，改依赖 SDK + 实际使用的 libs 包；主 pyproject 加 7 个 editable path 依赖；core 侧 `ToolResult`/`InputField`/plugin types 改从 SDK 导入并 re-export；`manager.py` 删除 `_build_plugin_pythonpath`，`main.py` 的 sys.path 注入收窄为项目根 + `domains/docaudit` | 8 个插件 venv 脱离宿主独立 `uv sync` 成功；中立 cwd 下插件 venv 内 `find_spec('courtier')` 为 `None`、`courtier_plugin_sdk` 可导入；7 个包 `uv build --wheel` 全部成功（轮内含规则 JSON）；红线 grep（`libs/`、`plugins/` 中对 courtier 应用包的 import）零命中 |
| 阶段二：切断 `libs/` → core 反向依赖 | ✅ 已落地（2026-08-10） | `doccorrector` ErrorCorrect 改显式参数注入，`CEC_*` 经 env 白名单下发；新增 `template_store` host service（`METHOD_TEMPLATE_STORE_GET`、manifest `KNOWN_HOST_SERVICES`/`read:templates`、manager handler 分支、SDK `HostTemplateStore`），template 插件 tools 全量改走 RPC；删除死代码 `validator/content_checker.py`、`validator/templates/crud.py`、`ParserConfig.from_settings`；`content_compliance` 类型改从 SDK 导入；search 插件 ES 访问下沉为插件本地 `es_client.py`（`ES_*` env 白名单），core 删除 `search_chunks` | 新增 `tests/plugin/test_host_services.py::TestTemplateStoreGetHostService` 6 用例与 `tests/plugin/test_sdk_templates.py` 2 用例；红线 grep 零命中 |
| 阶段三：`docmodels` 上移为共享模型包 | ✅ 已落地（2026-08-10） | `domains/docaudit/docmodels/` 移至 `libs/shared/docmodels/docmodels/`（导入名 `docmodels` 不变，`courtier/db/`、libs、plugins 消费方零改动）；删除空目录 `domains/docaudit/{models,rules}/`；根 `AGENTS.md`/`CLAUDE.md`、`courtier/CLAUDE.md` 布局描述同步 | 全量 `uv run pytest -m "not integration"`：1337 passed / 6 skipped；ruff clean；`validate-domain` 无需 PYTHONPATH 通过 |
| 阶段四：transport 按部署距离抽象 | 未触发 | 按方案约定按需实施，当前无跨机部署需求 | — |

> **偏差说明**（实施与正文方案的差异，以本表为准）：
>
> - §3.2 原计划新增的 `rule_store.*` host service 未实施：`validator/content_checker.py` 确认为无生产消费方的死代码，直接删除；`validator/templates/crud.py` 亦删除，模板访问统一走 `template_store.get`。
> - §3.3 原计划"全量修正消费方导入"实际为零改动：`docmodels` 上移后保持原导入名，各消费方无需修改。
> - `domains/docaudit` 仍保留在 `main.py` 与 pytest 的 sys.path 注入中：`courtier/agent/agents/input_models.py` 惰性导入 `skills.schemas.*`，属 core→domain 遗留软依赖，不在本次治理范围。
> - search 插件经 env 白名单（`ES_*`）获得 ES 连接配置，较 §1.6 "插件不持有宿主资源凭证"的隔离意图有所放宽；如需严格隔离，后续可改为 search 专用 host service。
> - 本次顺带对 20 个改动面内文件做了 black 对齐（多为此前会话遗留的格式漂移），无行为变化。

---

## 0. 问题清单总览

| # | 问题 | 级别 | 证据 | 修复阶段 |
|---|------|------|------|----------|
| 1 | `libs/` 五个库均无 `pyproject.toml`，靠宿主 spawn 插件时拼 PYTHONPATH 注入，无法被独立解析、安装与版本化 | 结构 | `courtier/plugin/manager.py:541-554` `_build_plugin_pythonpath` | 阶段一 |
| 2 | 全部 8 个插件的 venv 以 `dependencies = ["courtier"]` + editable path 依赖**整个 courtier 应用**（FastAPI/DB/ES 全家桶） | 结构 | `plugins/shared/*/pyproject.toml`（`../../..`）、`plugins/docaudit/audit/*/pyproject.toml`（`../../../..`） | 阶段一 |
| 3 | `libs/` → core 反向依赖：`Settings`、`courtier.db`、`courtier.plugin.types` | 结构 | 清单见 §1.3 | 阶段二 |
| 4 | core（`courtier/db/`）→ 领域包 `docmodels` 反向依赖，违反根 AGENTS.md §7.1 "Core must never import domain code" | 结构 | `courtier/db/load_doc.py:7`、`courtier/db/save_doc.py:6` | 阶段三 |
| 5 | `docmodels` 身处领域包却被 libs、plugins、core 三方共用，是错位的共享内核 | 结构 | `domains/docaudit/docmodels/` 消费方清单见 §1.5 | 阶段三 |
| 6 | 插件配置耦合宿主 `.env`、`COURTIER_REPO_ROOT` 注入与共享文件系统假设 | 部署 | `courtier/config.py:38` `_ENV_FILE`；`courtier/plugin/manager.py:519-535`（env 白名单）、`:560` | 阶段二/四 |
| 7 | transport 仅支持 stdio 子进程，无跨机部署路径 | 部署 | `courtier/plugin/client.py` | 阶段四 |
| 8 | 文档漂移：根 AGENTS.md 描述的 `domains/docaudit/{models,rules}/` 实为无内容的空目录，实际规则 JSON 在 `libs/` 下 | 卫生 | `libs/docaudit/validator/rules/*.json`、`libs/docaudit/content_compliance/rules/*.json` | 阶段三 |

---

## 1. 现状依赖面分析

### 1.1 `libs/` 不是 Python 包，而是 PYTHONPATH 补丁

`libs/shared/{docparse,docannot}` 与 `libs/docaudit/{validator,content_compliance,doccorrector}` 五个库均**没有 `pyproject.toml`**。它们能被插件 import，完全依赖宿主在 spawn 插件子进程时现场拼接 PYTHONPATH（`courtier/plugin/manager.py:541-554`，`_build_plugin_pythonpath`：项目根 + `libs/shared` + `libs/docaudit` + 领域包目录）。

后果：

- lib 自身的第三方依赖没有任何声明位置，只能"搭车"插件或宿主环境；parse 插件已被迫在注释中自证（"explicitly instead of relying on courtier's transitive dependencies"）。
- 脱离宿主 spawn 逻辑，任何工具（uv/pip/mypy）都无法独立解析 libs。
- libs 无法版本化，插件无法声明"我需要哪个版本的 docparse"。

### 1.2 插件虚拟环境 editable 依赖整个 courtier 应用

全部 8 个插件（`plugins/shared/`：parse、search、annotate、template；`plugins/docaudit/audit/`：format_audit、content_audit、text_correction、plagiarism）的 `pyproject.toml` 均为：

```toml
dependencies = ["courtier"]
[tool.uv.sources]
courtier = { path = "../../..", editable = true }   # docaudit 插件为 ../../../..
```

即每个插件 venv 都装进了整个 courtier 应用及其全部依赖（FastAPI、SQLAlchemy、ES、MinIO、OpenTelemetry……）。插件真正需要的只是 SDK 与若干 libs，但现状没有更小的可依赖单元。**这是"插件独立部署"的直接 blocker**：独立部署一个插件等于部署整套宿主应用。

### 1.3 `libs/` → core 反向依赖清单

| 位置 | 依赖 | 用途 |
|------|------|------|
| `libs/docaudit/doccorrector/corrector.py:553` | `courtier.config.Settings` | 读取 LLM 配置 |
| `libs/docaudit/validator/templates/crud.py:9-10` | `courtier.db.CRUDRepository`、`courtier.db.tables.FormatTemplateTable` | 模板表 CRUD |
| `libs/docaudit/validator/content_checker.py:27-29` | `Settings` + `courtier.db`（AsyncDatabase/CRUDRepository）+ `courtier.db.tables`（Rule/RuleDomain） | 规则加载 |
| `libs/docaudit/content_compliance/core.py:3-4` | `courtier.plugin.types`（ComplianceResult/Violation/ContentChecker） | 结果类型 |

libs 本应是依赖图底层的叶子，却反向引用应用层配置、数据库与插件协议类型。这些 import 在"宿主拼 PYTHONPATH"的现状下碰巧能工作，一旦插件脱离宿主环境即全部断裂。

### 1.4 core → 领域包反向依赖

`courtier/db/load_doc.py:7` 与 `courtier/db/save_doc.py:6` 直接 `from docmodels import ...`，而 `docmodels` 位于领域包 `domains/docaudit/docmodels/`。这违反根 AGENTS.md §7.1 的硬性约定（"Core must never import domain code"），且同样依赖 PYTHONPATH 拼接才能成立。

### 1.5 `docmodels`：错位的共享内核

`domains/docaudit/docmodels/`（`document.py` 运行时文档模型、`spec.py` 格式规范模型、`constants.py`）的实际消费方是三方：libs、`plugins/`、`courtier/db/`。被跨层共用的模型却放在"应只被宿主发现的领域包"里，是 §1.3/§1.4 两类反向依赖的共同根源之一。

附带漂移项（问题 8）：根 AGENTS.md 所述 `domains/docaudit/{models,rules}/` 为无内容的空目录；实际规则 JSON 在 `libs/docaudit/validator/rules/*.json` 与 `libs/docaudit/content_compliance/rules/*.json`。

### 1.6 已有隔离资产：通信边界是干净的

问题集中在包依赖面，运行时通信边界已经具备独立部署所需的全部要素：

- **JSON-RPC 2.0 over stdio**：`courtier/plugin/client.py`（`STREAM_LIMIT_BYTES = 64 MiB`），消息与字节流已有明确边界。
- **host service 反向 RPC 框架**：SDK 侧 `courtier/plugin/sdk/runtime.py` 的 `HostServiceClient`（负数 request id 区分反向调用），宿主侧 `courtier/plugin/manager.py:678-788` `_create_host_request_handler`。插件不持有宿主资源句柄，经 RPC 回调宿主服务。
- **已有服务与声明机制**：`cache.*`、`artifact_store.*`、`storage.put`；manifest 的 `dependencies.host_services` + `permissions` 声明，`courtier/plugin/manifest.py:20` `KNOWN_HOST_SERVICES = {"cache","artifact_store","storage"}`，`scanner.py:119` 校验。
- **凭证隔离意图明确**：插件 env 白名单（`manager.py:519-535`）刻意不含 DB/MinIO 凭证。

演进方案的基调因此是：**协议与框架不动，只治理 Python 包依赖面；新增的数据访问需求优先走 host service RPC，而不是给插件下发资源句柄。**

---

## 2. 目标部署形态

```text
形态 A（现状）            形态 B（阶段一~三后）          形态 C（阶段四后，目标态）
┌────────────────┐       ┌────────────────┐           ┌────────────────┐
│ courtier 应用   │       │ courtier 应用   │           │ courtier 应用   │
│  └ spawn 子进程 │       │  └ spawn 子进程 │           │  └ stdio 或 socket ───┐
│     插件 venv 内 │       │     插件 venv 仅 │           └────────────────┘      │
│     含整个宿主   │       │     含 SDK+libs │           ┌────────────────┐      │
└────────────────┘       └────────────────┘           │ 插件独立服务    │ ◄─────┘
 插件不可独立构建         插件可独立构建/测试           │ (独立容器/主机) │
                                                      └────────────────┘
```

- **形态 B**：部署拓扑不变（同机子进程），但插件依赖面收窄为 SDK + 显式声明的 libs 包，可独立构建、测试、出镜像。
- **形态 C**：插件可作为独立服务跨机部署，经 socket transport 复用同一 JSON-RPC 协议（含 host service 反向 RPC）；输入文件经 host service 中介，不再假设共享文件系统。

---

## 3. 演进路线

阶段一~三为纯依赖治理，低风险、可随时进行；阶段四按实际跨机需求触发。每阶段独立交付、独立测试；迁移按仓库惯例全量替换，不留兼容 shim。

### 3.1 阶段一：libs 包化 + 独立 Plugin SDK 包

**目标**：消灭 PYTHONPATH 补丁与"插件依赖整个宿主"，得到独立部署的最小依赖单元。

**改动点**：

- 为五个 lib 各补 `pyproject.toml`（`libs/shared/{docparse,docannot}`、`libs/docaudit/{validator,content_compliance,doccorrector}`），显式声明各自第三方依赖；只加 manifest，不动目录结构与 import 路径，把 diff 控制在声明层。
- 抽 `courtier-plugin-sdk` 独立包（建议落位 `packages/plugin-sdk/` 或 `libs/shared/plugin_sdk/`）：内容为现有 `courtier/plugin/sdk/`（runtime、`HostServiceClient`、tool 基类）+ JSON-RPC protocol types + `ToolResult`。**SDK 自身零 core 依赖**（不得 import courtier 应用代码），这是依赖图的底线。
- 8 个插件的 `pyproject.toml` 改为依赖 `courtier-plugin-sdk` + 实际使用的 libs 包，删除 `dependencies = ["courtier"]`；core 侧改为反向依赖 SDK 包。
- 同步更新：`manager.py:541-554` 的 PYTHONPATH 拼接随包化逐项退役（包化后由 venv 解析）；Dockerfile、插件 venv 初始化脚本、`AGENTS.md`/`CLAUDE.md` 中插件结构描述。

**影响面**：5 个 lib + 8 个插件 + 1 个新 SDK 包的 manifest 层；core 的 plugin SDK 导入路径。

**验证方式**：

```bash
# 每个插件 venv 脱离宿主独立解析
cd plugins/shared/parse && uv sync   # 其余 7 个插件同
# 红线：libs 与插件中不再存在对 courtier 应用包的 import（SDK 除外）
grep -rn "from courtier\." libs/ plugins/ --include="*.py" | grep -v "courtier_plugin_sdk"
# 全量回归
cd /home/lmwl/Documents/docaudit/agent/courtier
uv run pytest -m "not integration" -q
uv run ruff check courtier/ tests/
```

### 3.2 阶段二：切断 `libs/` → core 反向依赖

**目标**：libs 成为依赖图真正的叶子，只依赖 SDK 与第三方库。

**改动点**（对照 §1.3 清单逐项）：

- `doccorrector/corrector.py:553` 的 `Settings` → 构造函数参数注入：lib 定义自己需要的少量配置字段，由调用方（插件入口）显式传入。
- `validator/templates/crud.py` 与 `validator/content_checker.py` 的 DB 访问 → lib 定义窄 repository Protocol（如 `RuleRepository`/`TemplateRepository`）；宿主侧新增 `rule_store.*` / `template_store.*` host service 实现该 Protocol 的数据供给，注册进 `KNOWN_HOST_SERVICES`（`manifest.py:20`）并在 manifest 声明；插件经 `HostServiceClient` 调用。
  - 为什么不给插件发 DB 连接：env 白名单刻意不下发 DB 凭证（`manager.py:519-535`），该隔离意图应被保持；RPC 路径同时天然兼容阶段四的跨机形态。
- `content_compliance/core.py:3-4` 的 `courtier.plugin.types`（ComplianceResult/Violation/ContentChecker）→ 类型下沉至 SDK 包，core 侧改为从 SDK 导入。
- 插件配置解耦第一步：插件入口不再经宿主 `.env` 兜底（`config.py:38` `_ENV_FILE`），所需配置由 env 白名单显式下发或插件自身配置承载。

**影响面**：4 处 libs 代码 + 宿主侧新增 2 个 host service + manifest/scanner 白名单 + core 侧类型导入路径。

**验证方式**：`grep -rn "from courtier\.\|import courtier\." libs/` 零命中（SDK 除外）；全量回归同上；新增 host service 的契约测试（参照现有 `cache.*`/`storage.put` 测试）。

### 3.3 阶段三：`docmodels` 上移为共享模型包

**目标**：消除 core → 领域包违规依赖，让共享内核归位。

**改动点**：

- `domains/docaudit/docmodels/`（`document.py`、`spec.py`、`constants.py`）整体上移为带 `pyproject.toml` 的共享包（建议 `libs/shared/docmodels/`）。
- 全量修正消费方导入：libs、`plugins/`、`courtier/db/load_doc.py:7`、`courtier/db/save_doc.py:6`。迁移后 core 依赖的是共享模型包，AGENTS.md §7.1 "Core must never import domain code" 恢复成立。
- 卫生项（问题 8）：删除空目录 `domains/docaudit/{models,rules}/`；修正根 AGENTS.md 对规则 JSON 位置的漂移描述（实际位置 `libs/docaudit/validator/rules/`、`libs/docaudit/content_compliance/rules/`）。

**影响面**：导入路径全量替换，无行为变化。

**验证方式**：`grep -rn "from docmodels import" courtier/ libs/ plugins/ domains/` 全部指向新包路径；全量回归同上。

### 3.4 阶段四：transport 按部署距离抽象（按需求触发）

**目标**：从形态 B 切换到形态 C，插件可独立服务化部署。

**改动点**：

- 在 `courtier/plugin/client.py` 与 SDK runtime 之间抽 Transport 接口（双工字节流：send/recv JSON-RPC frame）。同机形态保持 stdio 子进程不变，作为默认 Transport。
- 跨机 Transport：插件以独立进程/容器运行，暴露 socket endpoint；宿主经 TCP + 长度前缀 framing 连接。`HostServiceClient` 的反向 RPC（负数 request id）与通道无关，协议零改动；沿用 64 MiB 帧上限（`client.py` `STREAM_LIMIT_BYTES`）。
- 输入文件抽象：现状插件经共享文件系统直接读 `COURTIER_UPLOAD_DIR` 下的上传文件。跨机形态补 `storage.get` host service（`storage.put` 已存在，annotate 输出已走该路径），文件类参数改传引用而非宿主本地路径。
- 配置解耦收尾：删除对宿主 `COURTIER_REPO_ROOT` 注入（`manager.py:560`）的依赖，跨机插件自带配置，由部署侧注入。
- 跨机形态新增课题（启用时才解）：通道鉴权/加密、重连与超时语义、插件服务的健康检查。

**影响面**：`plugin/client.py`、SDK runtime、manifest（插件寻址方式声明）。阶段一~三完成后插件已可独立构建出镜像，本阶段只是部署形态切换，不再触碰业务代码。

**验证方式**：Transport 接口单测；同机 stdio 路径全量回归（行为不变）；跨机形态的端到端冒烟（插件独立容器 + 宿主 socket 连接）。

---

## 4. 风险与取舍

- **包数量增多**：5 libs + 1 SDK + 8 插件各自带 manifest，依赖解析碎片化。对策：引入 uv workspace 统一管理（members = libs + SDK + plugins + core），一次锁定；代价是 Dockerfile/CI/文档同步调整。
- **host service RPC 替代直连 DB 引入延迟与可用性耦合**：规则/模板属低频读、可缓存，且插件本就是重 IO 子进程，RPC 开销占比可忽略；换取的是凭证隔离与跨机兼容，值得。
- **全量迁移的落地窗口**：按仓库惯例不留兼容 shim，阶段二/三需一次全量替换导入并跑通全量测试；阶段间可独立交付，控制单次窗口。
- **SDK 成为跨仓契约**：插件独立部署后 SDK 需要语义化版本与变更纪律（当前 monorepo 内 path 依赖暂无发版需求，但破坏性变更即升级为跨仓问题）。
- **阶段四不宜提前**：网络 transport 带来鉴权/加密/重连等新课题，无真实跨机需求时只完成接口隔离、不启用网络形态。

## 5. Non-goals

- 不改 JSON-RPC 2.0 协议与 host service 框架本身（已满足需求，见 §1.6）。
- 不动 Tool / SubAgent / Skill 三层抽象（边界见 `plugin-skill-boundary.md`）。
- 不做插件沙箱与 OS 级权限强制：现状插件与宿主同权限（根 AGENTS.md §10）；独立部署后以容器边界解决，不在本方案引入进程内沙箱。
- 不引入服务注册中心/服务网格：插件集合维持静态配置与目录扫描。
- 与 Pi 架构迁移计划正交：本方案不改变其概念映射（见 `pi-architecture-migration-plan.md`），两计划可独立推进。

---

## 附：验收命令（各阶段通用）

```bash
cd /home/lmwl/Documents/docaudit/agent/courtier
uv run pytest -m "not integration" -q
uv run ruff check courtier/ tests/
# 前端如涉及改动：
cd webui && npm run build && npm test
```
