# 插件系统独立化（跨机部署 + 主进程拨号 + 彻底去 spawn）实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 把插件系统从"主进程 spawn 子进程 + stdio 管道"彻底改造为"插件独立启动、独立运行的网络服务"：插件进程可部署在任意主机，主进程按配置拨号连接，协议保持换行分隔 JSON-RPC 不变，文件双向传递走 MinIO 直连（插件持 bucket 级受限凭据）。spawn 路径整体删除，不留双模式。

**Architecture:** 插件做 TCP server（SDK 提供 listen 模式），接受连接后立即 `plugin.register`；主进程 `ProcessManager` 改为连接管理器（connect → 双向 token 鉴权 → 能力注册 → host_services/runtime_context 推送 → 健康检查 → 断线无限退避重连）。`ProxyTool`/`ToolRegistry`/`DomainActivator`/提示注入链路零改动（鸭子类型 `_client.plugin_name` 保留）。文件契约：LLM 侧仍传短路径引用，ProxyTool 派发边界把标记为 `file-ref` 的参数对应的本地文件 PUT 到 MinIO transfer bucket 并改写为 `minio://` 引用；插件 SDK `resolve_file()` 用自有受限凭据直连 MinIO 下载到请求级临时目录。输出方向对称：插件直传 MinIO，结果携带 `minio://` 引用，主进程按需读取并为前端签下载 URL。

**Tech Stack:** asyncio TCP（`asyncio.open_connection`/`start_server`）、MinIO（bucket policy + service account + lifecycle）、uv、pytest、Docker 多 target 构建、docker-compose。

**关联文档:** 无前置计划依赖。背景：2026-08-17 用户要求"插件系统独立启动和运行，不依附于主进程"，经多轮讨论敲定六项决策（见下）。文件传递方案经历两轮：初定主进程 HTTP 文件端点，后用户提出 MinIO 双向对称方案并拍板凭据直连，本文件以此为准。

---

## 决策记录（已与用户确认）

| # | 决策点 | 结论 | 被否方案 |
|---|--------|------|----------|
| 1 | 运行边界 | **直接支持跨机部署**——不得假设共享文件系统 | 同机/共享卷起步 |
| 2 | 连接方向 | **主进程连插件**——插件做 server，端点写在主进程配置；重连逻辑集中在主进程 | 插件连主进程（网关模式） |
| 3 | stdio spawn | **彻底删除**——所有环境一律独立启动 | 双模式并存 |
| 4 | 文件传递 | **MinIO 双向对称传递**（用户提出）：输入=主进程 PUT + `minio://` 引用，插件下载到自有临时目录；输出=插件直传 + `minio://` 引用，主进程按需读取 | 主进程 HTTP 文件端点；presigned URL 传递；RPC 内联 base64 |
| 5 | 通道鉴权 | **共享 token 双向校验**——`COURTIER_PLUGIN_TOKEN` 两侧配置 | mTLS（留作后续硬化）；不鉴权 |
| 6 | MinIO 凭据归属 | **插件持受限凭据直连**（用户拍板）：独立 transfer bucket + bucket 级 policy 的专用 service account，见 §4 隔离设计 | presigned URL（插件零凭据） |

决策 6 的残余风险（用户已知情接受）：任一插件机器被攻破 → 攻击者可读写 transfer bucket 中的**在途瞬态文件**（24h 生命周期自动过期）；触不到 docs/library 等其他 bucket、触不到 DB。缓解：policy 最小化、凭据轮换写入运维文档、per-plugin 独立账号留作后续硬化项。直连模式同时消除了 presigned 方案的"签名地址必须插件可达"配置坑（每插件 env 自配可达 endpoint）。

---

## 目标设计

### 1. 传输与握手（协议帧零改动）

- 换行分隔 JSON-RPC over TCP；正 id（host→plugin）/负 id（plugin→host）约定、`request.cancel`、`plugin.health` 等全部保留（`courtier/plugin/protocol.py` 与 SDK `protocol.py` 同步改）。
- 握手时序（host 拨号成功后）：
  1. 插件 accept 后立即发 `plugin.register` notification，params 增加 `token` 字段（插件→主进程鉴权）。
  2. 主进程校验 register 的 token（`hmac.compare_digest` 对 `COURTIER_PLUGIN_TOKEN`），不符 → 关闭连接、插件置 `BLOCKED`、日志告警。
  3. 主进程首个请求发 `plugin.auth{token}`（主进程→插件鉴权）。插件校验通过前拒绝除 `plugin.auth` 外的一切方法（JSON-RPC error）。
  4. 鉴权通过 → 主进程推送 `plugin.host_services`、`plugin.runtime_context`（内容与现状一致，COURTIER.md 全文仍由主进程读取后线上推送）→ 状态 `ACTIVE`。
- `plugin.yaml` 的 `api` 升为 `"2.0"`（握手含强制 auth，属破坏性变更），`PluginScanner.HOST_API_VERSION` 同步升 `"2.0"`；仓内 8 个插件同仓锁步迁移。

### 2. 连接配置

- 主进程新增环境变量（Settings，`.env.example` 同步）：
  - `COURTIER_PLUGIN_ENDPOINTS="parse_document=127.0.0.1:9101,convert_document=127.0.0.1:9102,..."`——启动时解析为映射；**扫描到的每个插件都必须有 endpoint，缺失 → 该插件 `BLOCKED` 并打清晰日志**；多余 endpoint → warn。
  - `COURTIER_PLUGIN_TOKEN`——共享密钥。
- 每插件 `plugin.yaml` 的 `runtime` 增加 `port` 字段（插件默认监听端口，9101–9108，SDK 在 `--listen`/env 未指定时使用；主进程不消费）。端口分配（按当前插件名；后续改名计划 Phase 1 落地时同步换 key）：

| 端口 | 插件 | 端口 | 插件 |
|------|------|------|------|
| 9101 | parse | 9105 | search |
| 9102 | anydoc | 9106 | check_format |
| 9103 | annotate | 9107 | check_content |
| 9104 | template | 9108 | detect_plagiarism |

- `manifest.py` 的 `runtime` 模型加 `port: int | None`（`extra="forbid"`，必须显式加字段）。
- 插件监听地址优先级：`--listen host:port` CLI 参数 > `COURTIER_PLUGIN_LISTEN` env > manifest `runtime.port`（host 取 `0.0.0.0`）。

### 3. 主进程生命周期重构（`courtier/plugin/`）

- 状态机：`SCANNED → CONNECTING → REGISTERING → ACTIVE → DISCONNECTED → CONNECTING…`；另有 `STOPPING/STOPPED`（管理员 stop）与 `BLOCKED`（缺 endpoint/token 不符/api 不兼容）。**删除** `CRASHED/RESTARTING/FATAL`（远程语义下不存在"重启插件"）。
- `ProcessManager` 改为每插件一个连接协程：connect（失败即退避）→ 握手 → ACTIVE → 保留健康循环（30s `plugin.health`，5s 超时，连续 3 次失败转 DISCONNECTED）→ 读循环 EOF/异常 → `on_unregister`（ToolRegistry 摘除）→ `cancel_pending` → DISCONNECTED → 指数退避重连（1s 起、2 倍、30s 封顶、**无限次**）。管理员 `restart` 动作语义改为"断开重连"。
- `PluginSystem.start()` 非阻塞：启动全部连接协程即返回，工具随连接建立动态出现（per-request agent 重建 + `DomainActivator.visible` 机制自动拾取，无需额外处理）。
- **删除清单**（spawn 时代产物）：子进程启动/`asyncio.create_subprocess_exec`、venv 探测、`_resolve_env` + `_ALLOWED_MANIFEST_ENV_VARS` + `_SETTINGS_ENV_FALLBACK`、`COURTIER_REPO_ROOT`/`COURTIER_UPLOAD_DIR` 注入、stderr tee 与 `_monitor_stderr`、`_kill_process`/SIGKILL、`_IMMEDIATE_CRASH_WINDOW` 熔断、`lifecycle.py` 整文件（重连逻辑内联进 manager）。
- `admin_extensions.py`：`GET /plugins` 返回新状态集；`POST /plugins/{name}/action` 的 restart → reconnect；`GET /plugins/{name}/logs` 返回 410 + "远程插件，日志请查插件侧容器/进程"（前端 `client.ts:475` 消费处加提示分支）。Prometheus `PLUGIN_STATE` 指标沿用新状态值。
- 日志：主进程记录 connect/disconnect/reconnect/auth 失败事件；插件日志归插件侧 stdout/stderr（容器即 docker logs）。

### 4. 文件契约（MinIO 双向对称，跨机的核心改造）

**专用基础设施**：独立 transfer bucket（默认名 `courtier-plugin-io`，env `MINIO_BUCKET_PLUGIN_IO` 可配），配 24h 生命周期自动过期；独立 MinIO service account（如 `courtier-plugins`），attached policy 仅允许该 bucket 的 `s3:GetObject`/`s3:PutObject`/`s3:DeleteObject`，其余 bucket 一律拒绝。交付 provisioning 脚本（`scripts/minio_plugin_io.sh`，mc 命令封装，幂等）。

**对象 key 约定**：
- 输入（主进程写）：`in/<sha256(相对路径|大小|mtime)>/<原文件名>`——内容寻址天然去重，同文件不重复 PUT；保留文件名使插件按扩展名分支的逻辑不失效。
- 输出（插件写）：`out/<插件名>/<uuid>/<原文件名>`。

**标记**：SDK `register_tool` 读取工具类新属性 `file_params: list[str]`，在注册 cap 的 `input_contract` 对应 property 上写 `"format": "file-ref"`。运行时真值在 register caps（host 只消费 register notification），manifest yaml 同步标注仅供静态阅读。

**输入方向（主进程 → 插件）**：
1. LLM 照旧传短路径（如 `/data/uploads/<会话>/请示.docx`）——上下文契约不变，URL/key 不进上下文。
2. `ProxyTool.execute()` 派发前，对 `file-ref` 标记参数：值必须 resolve 在 `upload_dir` 内（**fail-closed**，目录外直接报错）→ 按 key 约定 PUT 到 transfer bucket（以 key 的 sha 前缀做存在性检查当缓存，`asyncio.to_thread` 包裹同步 minio SDK）→ 参数值改写为 `minio://courtier-plugin-io/in/<hash>/请示.docx`。
3. 插件 SDK `resolve_file(value)`：`minio://` 前缀 → 用插件自有 env（`MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_SECURE`/`MINIO_BUCKET_PLUGIN_IO`）建 Minio client 下载到请求级临时目录（`COURTIER_PLUGIN_WORKDIR` 或系统 temp 下 `req-<id>/`），返回本地路径；无 scheme 的按本地路径直传（同机开发便利）。`tool.execute` 结束后 SDK 清理该请求临时目录。
4. 插件内现有"路径逃逸检查"（anydoc/parse 基于 `COURTIER_UPLOAD_DIR` 的 allowed-root）改以插件 workdir 为根——插件只应打开 resolve_file 返回的路径。

**输出方向（插件 → 主进程）**：
1. SDK 新增 `put_file(local_path, *, filename=None) -> str`：直传 transfer bucket（key 按输出约定），返回 `minio://` 引用。
2. 工具结果携带 `minio://` 引用；主进程需要字节时用自有（全权限）client 直接读；需要给前端下载链接时，走**新增 host service 方法 `storage.presign_get`**（反向 RPC，参数 bucket+key，主进程校验 bucket 必须是 transfer bucket 后签 presigned GET，有效期沿用现有 7 天上限内取值）——结果中 `download_url` 字段形态与现状一致，前端零改动。
3. 现有 `storage.put`（base64 反向 RPC）保留向后兼容，但文件类输出统一迁移到 `put_file`（annotate 作为范式插件完成迁移验证）；纯文本/dict 输出的插件（parse、anydoc 等预计）不动。
4. 文件字节不再经过 RPC 通道，**64MiB 单行帧上限不再约束文件输出**（仅约束文本类结果，现状不变）。

**逐插件审计**（Phase 0 Task 0.3 落地，结论记入实施记录）：入参含宿主机路径的工具全部列入 `file_params`；输出含本地路径的全部改 `put_file`。

### 5. 环境变量归属

- 主进程删除一切插件 env 注入。插件读自己的环境（现状本就是 `os.environ` 直读）。
- manifest `runtime.env` 块语义转换：从"主进程解析注入"变为"**插件必需环境变量声明**"——SDK 启动时校验各 key 在插件环境中存在，缺失即拒绝启动并列出缺失项（fail-fast）。`${ENV:VAR}` 写法废弃，只保留 key 列表。含文件参数的插件必须声明 `MINIO_*` 五个变量与 `COURTIER_PLUGIN_TOKEN`。
- 新增 `plugins/plugin.env.example`：汇总 8 个插件消费的全部环境变量（`LLM_*`/`DOCPARSE_*`/`ES_*`/`CEC_*`/`ANYDOC_OCR_API_URL`/`MINIO_*`（受限账号）/`COURTIER_PLUGIN_TOKEN`/`COURTIER_PLUGIN_LISTEN`/`COURTIER_PLUGIN_WORKDIR` 等）。主进程 `.env.example` 删去纯插件变量，加两个新配置项（endpoints/token）。

### 6. 部署制品

- 新 `courtier/Dockerfile.plugins`：多 target，每插件一个 target——builder 阶段以 repo 为 context 复制 `libs/` + 对应插件目录，`uv sync --frozen --no-dev --no-editable`（修正现状：主 Dockerfile 从不为插件建 venv、`.dockerignore` 排除 `plugins/**/.venv` 导致容器内插件依赖未闭环）；final 阶段 `CMD ["python", "entry.py"]`。anydoc target 额外装 libreoffice（参照 `Dockerfile.env`）。SDK 依赖新增 `minio` 包（`plugin_sdk` 的 pyproject 声明）。
- `docker-compose.yml`：新增 8 个插件服务（`build.target=plugin-<name>`，`environment` 各配各的——含受限 MinIO 账号，`restart: unless-stopped`）；`app` 服务取消注释，配 `COURTIER_PLUGIN_ENDPOINTS`（指向服务名）、`COURTIER_PLUGIN_TOKEN`。**不挂共享 uploads 卷给插件**（文件走 MinIO，这正是跨机设计的目的）。插件服务与 MinIO 同网络。
- 主 `Dockerfile`：`COPY plugins/ plugins/` 保留（主进程仍需扫描各 `plugin.yaml` 作为超时/权限策略根，插件代码随镜像但不执行——可接受，记录在案）。
- 本地开发：`courtier/scripts/dev-plugins.py`——读取 endpoint 配置（或内置端口表），逐个以 `<plugin>/.venv/bin/python entry.py --listen 127.0.0.1:<port>` 前台拉齐 8 个插件（日志带插件名前缀），加载 `plugins/plugin.env`。纯开发便利脚本，不进 `courtier` 包、无生命周期管理。

---

## 文件职责

| 对象 | 操作 | 阶段 |
|------|------|------|
| `libs/shared/plugin_sdk/src/courtier_plugin_sdk/runtime.py` | 加 serve/listen（`asyncio.start_server`、多连接各自独立 id 空间、accept 即 register 带 token、auth 闸口、shutdown 关连接）；删 stdio 生产入口（测试的 duck-typed I/O 注入保留）；启动时校验 `runtime.env` 声明的必需变量 | 0 |
| `libs/shared/plugin_sdk/src/courtier_plugin_sdk/files.py`（新） | `resolve_file`（`minio://` 下载 + 本地路径直传）、`put_file`（直传 transfer bucket 返回 `minio://`）、请求级临时目录与清理、Minio client 懒加载（env 缺失时报清晰错误） | 0 |
| `libs/shared/plugin_sdk/src/courtier_plugin_sdk/protocol.py` | `plugin.auth` 方法常量 | 0 |
| `libs/shared/plugin_sdk/pyproject.toml` | 新增 `minio` 依赖 | 0 |
| 8 个插件 `entry.py`/`tools.py` | 工具类加 `file_params`；文件参数过 `resolve_file`；文件输出改 `put_file`（审计驱动）；entry 删 stdio 假设 | 0 |
| 9 个 `plugin.yaml` | `api: "2.0"`；`runtime.port`；`runtime.env` 转必需变量声明（含 `MINIO_*`） | 0 |
| `courtier/plugin/client.py` | TCP 连接支持（`asyncio.open_connection`）；`plugin.auth` 调用；register token 校验挂接 | 1 |
| `courtier/plugin/manager.py` | 重写为连接管理器（状态机/退避重连/非阻塞 start）；删 spawn/env/stderr/SIGKILL/熔断；新增 host service 方法 `storage.presign_get`（bucket 白名单校验） | 1/2 |
| `courtier/plugin/lifecycle.py` | 删除 | 1 |
| `courtier/plugin/manifest.py` / `scanner.py` | `runtime.port` 字段；`HOST_API_VERSION="2.0"` | 1 |
| `courtier/plugin/protocol.py` | `plugin.auth` 常量；register params 加 `token`；`storage.presign_get` 方法常量 | 1/2 |
| `courtier/config.py` | Settings 两项：`courtier_plugin_endpoints`（映射解析）、`courtier_plugin_token`；transfer bucket 名配置 `MINIO_BUCKET_PLUGIN_IO` | 1 |
| `courtier/agent/api/routes/admin_extensions.py` | 状态集/restart→reconnect/logs→410 | 1 |
| `courtier/plugin/proxies.py` | `file-ref` 参数改写：upload dir 内校验 fail-closed → PUT transfer bucket（缓存）→ `minio://` 引用 | 2 |
| `scripts/minio_plugin_io.sh`（新） | transfer bucket + 受限 service account + bucket policy + 24h 生命周期，幂等 provisioning | 2 |
| `webui/src/api/client.ts` + 日志展示组件 | logs 410 分支提示"远程插件" | 1 |
| `courtier/Dockerfile.plugins`（新）、`docker-compose.yml`、`Dockerfile` | 插件镜像/服务/接线 | 3 |
| `courtier/scripts/dev-plugins.py`（新）、`plugins/plugin.env.example`（新）、`.env.example` | 开发体验与配置样例 | 3 |
| `AGENTS.md`（根）、`courtier/CLAUDE.md`、`courtier/docs/plugin-development.md` | 架构描述重写：独立服务、endpoint 配置、鉴权、MinIO 文件契约与受限账号运维（含轮换指引）、env 归属 | 3 |
| `tests/` | SDK serve/auth/resolve_file/put_file；manager/client 重写；ProxyTool 改写；storage.presign_get；loopback 集成（真插件 server + 连接 + 正反向调用 + 断线重连）；MinIO 相关 e2e 标 integration（依赖 compose MinIO）；删/改 spawn 时代测试 | 0–2 |

---

## Phase 0：SDK 服务模式与插件侧文件能力（插件先能独立跑起来）

### Task 0.1: SDK listen 模式 + 鉴权闸口 + 8 插件 entry 改造

**Files:** SDK `runtime.py`/`protocol.py`；8 个插件 `entry.py`；9 个 `plugin.yaml`

- [x] **Step 1:** SDK `protocol.py` 加 `METHOD_PLUGIN_AUTH = "plugin.auth"`；`PluginRuntime.run()` 改为 server：`--listen`/`COURTIER_PLUGIN_LISTEN`/manifest port 三级解析，`asyncio.start_server`；每连接独立通道状态（独立 id 空间、独立 pending 表），accept 后立即发 `plugin.register`（params 加 `token`，读插件 env `COURTIER_PLUGIN_TOKEN`，未配置则启动即报错）。
- [x] **Step 2:** auth 闸口：连接级状态 `authed=False`；`plugin.auth` 校验 token（compare_digest）通过前拒绝其他一切方法；`plugin.shutdown` 改为关闭该连接（连接断开不退出进程——server 常驻）。
- [x] **Step 3:** 删 stdio 生产入口（`run()` 不再绑 `sys.std{in,out}`；stdin 循环重构为连接处理函数，测试注入的 reader/writer 路径保留）。
- [x] **Step 4:** `runtime.env` 必需变量启动校验（缺失列出并退出非零）。**实施时语义修订**：字面量经 `os.environ.setdefault` 作默认值（SEARCH_* 旋钮依赖此），`${ENV:}` 标记忽略——底层库本就 `os.getenv` 带代码默认值，全部 fail-fast 会迫使部署方冗余配置；仅 `COURTIER_PLUGIN_TOKEN` 保持启动 fail-fast。
- [x] **Step 5:** 8 个 `plugin.yaml`：`api: "2.0"` + `runtime.port`（按端口表）+ `runtime.env` 清理（parse/anydoc 删 `${ENV:}` 块，search 保留字面量）；8 个 `entry.py` 无 stdio 假设，零改动。

### Task 0.2: SDK 文件能力（`resolve_file`/`put_file`/临时目录）

**Files:** SDK 新 `files.py`、`runtime.py`（请求生命周期钩子）、`pyproject.toml`

- [x] **Step 1:** `minio` 依赖入 SDK；Minio client 懒加载（读插件 env，缺配置时给出"该插件需要 MINIO_* 环境变量"的清晰报错）。
- [x] **Step 2:** `resolve_file(value)`：`minio://bucket/key` → 下载到请求级临时目录（保留原文件名）返回本地路径；本地路径直传。`put_file(local_path)` → 直传 `out/<插件名>/<uuid>/<文件名>`，返回 `minio://` 引用。
- [x] **Step 3:** `tool.execute` 分发时创建请求级临时目录（`COURTIER_PLUGIN_WORKDIR` 或系统 temp 下 `req-<id>/`）；响应发出后 `finally` 清理。

### Task 0.3: 8 插件文件参数标记 + 输出审计迁移

**Files:** 8 插件 `tools.py`/`entry.py`

- [x] **Step 1:** 逐插件审计入参：`parse_document(file_path)`、`convert_document(file_path)`、`annotate_document` 等全部文件参数列入工具类 `file_params`；SDK `register_tool` 将其写入 cap `input_contract` 的 `"format": "file-ref"`。
- [x] **Step 2:** 插件工具入口把文件参数过 `resolve_file`；原 allowed-root 逃逸检查改以插件 workdir 为根。
- [x] **Step 3:** 输出审计：凡返回本地输出路径的改 `put_file` + `storage.presign_get`（annotate 作为范式完成迁移）；产出审计结论表（哪个插件改了什么）记入本文件实施记录。

### Task 0.4: SDK 与插件测试

- [ ] **Step 1:** SDK 单测：listen/多连接/auth 通过与拒绝/env 缺失 fail-fast/resolve_file 与 put_file（minio client mock）/临时目录清理。
- [ ] **Step 2:** 插件测试改造：`tests/plugin/` 现有基于管道注入的用例适配新入口；`uv run pytest tests/plugin/ -q` 绿。

## Phase 1：主进程 connect 模式

### Task 1.1: 配置面

**Files:** `courtier/config.py`、`.env.example`、`manifest.py`、`scanner.py`

- [ ] **Step 1:** Settings 两项 + endpoint 映射解析 + `MINIO_BUCKET_PLUGIN_IO`（格式错/缺 endpoint 的校验与日志）。
- [ ] **Step 2:** manifest `runtime.port` 字段；`HOST_API_VERSION = "2.0"`。
- [ ] **Step 3:** `.env.example`：加两项新配置、删纯插件变量（迁至 `plugins/plugin.env.example`，Phase 3 落地文件）。

### Task 1.2: client/manager 重写（核心）

**Files:** `courtier/plugin/client.py`、`manager.py`、`lifecycle.py`（删）、`protocol.py`、`registry.py`（仅状态引用）、`__init__.py`

- [ ] **Step 1:** `protocol.py` 加 auth 常量；register params `token`。
- [ ] **Step 2:** `JSONRPCClient` 支持 TCP：`asyncio.open_connection` 建立 reader/writer；保留 id 匹配/响应缓冲/`cancel_pending`/超时语义；断开回调统一走 read-loop EOF。
- [ ] **Step 3:** `ProcessManager` 重写：每插件连接协程（connect→register token 校验→`plugin.auth`→host_services/runtime_context→ACTIVE→健康循环）；断线 → unregister+cancel → DISCONNECTED → 无限退避重连（1s×2ⁿ，30s 封顶）；`start()` 非阻塞；管理员 start/stop/restart（=reconnect）。
- [ ] **Step 4:** 删除清单逐项落地（spawn/env 注入白名单/Settings fallback/stderr tee/SIGKILL/5s 熔断/`lifecycle.py`）；`PluginSystem` 门面方法（broadcast/notify_plugin/cancel_pending/get_log_path）适配或删除。
- [ ] **Step 5:** 状态机枚举与 Prometheus 指标值更新；`BLOCKED` 路径（缺 endpoint、token 不符、api 不兼容）。

### Task 1.3: admin API 与前端

**Files:** `admin_extensions.py`、`webui/src/api/client.ts` + 相关组件

- [ ] **Step 1:** logs 端点 410 + 说明；action 语义调整；状态文案。
- [ ] **Step 2:** 前端 logs 调用处加 410 分支提示；`npm test` 与 `npm run build` 绿。

### Task 1.4: 主进程测试重写 + loopback 集成

**Files:** `tests/courtier/`（manager/client/scanner 相关）、`tests/agent/api/test_extension_admin.py`

- [ ] **Step 1:** spawn 时代用例删除/重写（env 注入、venv 探测、stderr、熔断全部下线）；新状态机/退避重连/鉴权失败路径单测。
- [ ] **Step 2:** loopback 集成：fixture 插件 server（SDK serve 模式）+ 主进程连接 → register/auth → `tool.execute` → 反向 host service 调用 → 杀插件进程 → 断言 DISCONNECTED 与重连恢复。

## Phase 2：MinIO 文件契约（主进程侧）

### Task 2.1: transfer bucket provisioning

- [ ] **Step 1:** `scripts/minio_plugin_io.sh`：建 bucket、建受限 service account、写 bucket policy（仅 transfer bucket Get/Put/Delete）、配 24h 生命周期；幂等；README 注释轮换步骤。
- [ ] **Step 2:** 对 compose MinIO 实测：受限账号访问 docs bucket 被拒、transfer bucket 读写正常、过期规则生效。

### Task 2.2: ProxyTool 改写 + `storage.presign_get`

**Files:** `proxies.py`、`manager.py`（host service 新增方法）、`protocol.py`

- [ ] **Step 1:** `file-ref` 参数改写：upload dir 内 resolve 校验 fail-closed → key 约定 PUT（存在性检查当缓存）→ `minio://` 引用；非标记参数不动。
- [ ] **Step 2:** `storage.presign_get(bucket, key, expires?)`：bucket 白名单（仅 transfer bucket）→ 签 presigned GET；权限声明沿用 host_services/permissions 体系（`storage` + `read:storage`）。
- [ ] **Step 3:** 单测：标记参数路径→`minio://`、目录外报错、重复派发不重复 PUT、presign_get 拒绝非 transfer bucket。

### Task 2.3: 端到端集成（compose MinIO，标 integration）

- [ ] **Step 1:** 上传文件 → 主进程派发 `convert_document(file_path)` → 插件 `resolve_file` 从 MinIO 下载 → 返回 Markdown 全文链路。
- [ ] **Step 2:** annotate `put_file` 输出 → 主进程收 `minio://` 引用 → `storage.presign_get` → URL 可下载且内容一致。

## Phase 3：部署制品与开发体验

### Task 3.1: 插件镜像与 compose

- [ ] **Step 1:** `Dockerfile.plugins` 多 target（builder 按插件 `uv sync --frozen --no-dev --no-editable`；anydoc target 带 libreoffice）。
- [ ] **Step 2:** `docker-compose.yml` 8 插件服务 + app 接线（endpoints/token；插件各配受限 MinIO 账号；不挂 uploads 卷）；`docker compose build` 与 `up` 实测；provisioning 脚本纳入部署文档/初始化流程。

### Task 3.2: 本地开发

- [ ] **Step 1:** `scripts/dev-plugins.py`（前台拉齐 8 插件、日志前缀、加载 `plugins/plugin.env`）。
- [ ] **Step 2:** `plugins/plugin.env.example` 全变量汇总。

### Task 3.3: 文档

- [ ] **Step 1:** 根 `AGENTS.md` §5.2/§5.3/§7.1/部署节重写（注意 §7.1"插件永不持有 DB/MinIO 凭据"原则更新为"仅持 transfer bucket 受限凭据"及理由）；`courtier/CLAUDE.md` 同步。
- [ ] **Step 2:** `docs/plugin-development.md` 重写：独立运行方式、env 归属、`file_params`/`resolve_file`/`put_file`/`storage.presign_get` 约定、鉴权模型。

## Phase 4：全量回归与真实会话验收

### Task 4.1: 全量测试

- [ ] **Step 1:** `uv run pytest -m "not integration"` 绿；`uv run courtier validate-domain domains/docaudit/` 过；`webui` `npm test` + `npm run build` 绿。

### Task 4.2: 真实会话烟测（dev-runner 起 8 插件 + 主进程）

- [ ] **Step 1:** 格式审核会话：`parse_document` 经 MinIO 拿文件、`check_format` 全链路、产物正常。
- [ ] **Step 2:** 内容审核会话：`check_content`、annotate 经 `put_file` 输出与前端下载、`template_store.get`、`$ref` 编号连续、会话日志核对（沿用用户惯例，以真实会话日志为准）。
- [ ] **Step 3:** 故障演练：会话中杀掉单插件 → 该工具报错语义清晰、重连后新会话恢复；停全部插件起主进程 → 启动不阻塞、工具随连接出现。

---

## 提交切分（约定式）

```
feat(plugin-sdk): standalone TCP serve mode with token auth            # 0.1
feat(plugin-sdk): minio-backed resolve_file/put_file with workdir      # 0.2
refactor(plugins): file-ref params and put_file outputs                # 0.3
test(plugin-sdk): serve mode and file capability coverage              # 0.4
feat(config): plugin endpoints and token settings                      # 1.1
refactor(plugin): replace subprocess manager with connection manager   # 1.2
feat(api): plugin admin semantics for remote plugins                   # 1.3
test(plugin): connection manager and loopback integration              # 1.4
chore(minio): transfer bucket provisioning with restricted account     # 2.1
feat(plugin): proxy file-ref rewrite via minio and storage.presign_get # 2.2
test(integration): end-to-end minio file delivery                      # 2.3
build(docker): per-plugin images and compose services                  # 3.1
chore(scripts): dev-plugins runner and plugin env example              # 3.2
docs: standalone plugin architecture                                   # 3.3
test: full regression and session acceptance                           # 4.x（如无代码改动则记录于计划）
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；`validate-domain` 通过；`webui` 测试/构建绿。
2. 跨机形态验证：compose（或双机）下插件与主进程分容器/分主机，完整格式+内容审核会话成功；主进程重启后自动重连，插件无感；插件重启后工具自动恢复。
3. 安全断言：无 token/错 token 连接无法注册与调用；插件环境内**仅持有 transfer bucket 受限凭据**（实测访问 docs/library bucket 被拒）、无 DB 凭据；transfer bucket 24h 生命周期生效。
4. 文件链路断言：输入经 `minio://` 引用到达插件且内容一致；annotate 输出经 `put_file` + `presign_get` 前端可下载；同文件重复派发不重复 PUT（日志/对象数验证）。
5. 遗留语义确认：admin `/plugins` 状态集正确；logs 端点 410 前端有提示；`.env.example` 与 `plugins/plugin.env.example` 注释完整。

## 明确不做的事

- TLS/mTLS（token 兜底；跨不可信网络部署在文档中指引 stunnel/WireGuard/Overlay 网络，mTLS 留作后续硬化）。
- per-plugin 独立 MinIO 账号与凭据轮换自动化（先单账号 + bucket policy，轮换步骤写文档；硬化留后续）。
- 插件多副本与负载均衡（一插件一实例；连接管理器按名单拨号，不引入服务发现）。
- `artifact_store` host service 的 session 接线（现状无插件使用，协议原样保留）。
- 上传文件源头迁入 MinIO（uploads 仍落主进程本地盘，首次派发时才 PUT transfer bucket；源头迁移是独立演进项）。
- 主进程多副本化；`pi/` 不动；历史 plans/specs 文档不改写。

---

## 实施记录

**2026-08-17 计划定稿前偏差修正**：实施起步时发现两项计划前提与仓库现状不符，已修订正文：
1. **9 插件 → 8 插件**：`correct_text` 插件已由并行计划移除（`b43ca3d`，纠错能力迁入 content_audit 技能提示词），磁盘残留 `.venv`/`__pycache__` 为未跟踪垃圾，可随手清理。端口表收缩为 9101–9108。
2. **插件名以现状为准**：`parse`/`anydoc`/`annotate`/`template` 尚未改名（改名计划 Phase 1 未实施），endpoint 配置 key、`runtime.port` 均按当前名落地；该计划落地时同步换 key 即可。

**Task 0.1 完成**（SDK serve 模式，`293dba7`）。偏差与实测：
1. **Task 1.1 的 `manifest.runtime.port` 字段与 `HOST_API_VERSION="2.0"` 提前随本任务落地**——否则 scanner 因 api/未知字段拒收新清单，树立即红。
2. **in-process 测试注入路径免于鉴权**：queue/TestWriter 注入只在测试中出现，无信任边界；三个直接调用旧内部 API 的测试（test_runtime 取消用例、test_search_plugin 两处 `_process_line`）改用新 `_Connection` 类型适配，随本任务提交以保持单提交绿。
3. **`runtime.env` 语义落地为"字面量 setdefault 默认值"**（见 Step 4 修订）；`${ENV:}` 形式从 parse/anydoc/search 清单删除，变量文档归 `plugins/plugin.env.example`（Task 3.2）。
4. **冒烟实测**：anydoc 插件 venv 直起 `--listen 127.0.0.1:19102`——register 立即到达且带 token；未鉴权请求被 -32003 拒绝；错 token 鉴权后连接被插件侧关闭；对 token 鉴权后 `tool.list` 正常返回。`tests/plugin/` 全绿（280 passed, 1 skipped）。
5. fixture 插件（echo/crashing/slow_register/bad_name_mismatch）api 同步升 2.0；`bad_api_mismatch` 保持 0.9 以覆盖不兼容分支。

**Task 0.2 完成**（SDK 文件能力，`5c0662c`）：`files.py` 新增 `resolve_file`（minio:// 下载，保留原文件名，同目录重名加序号前缀）/`put_file`（`fput_object` 直传）；`_default_tool_execute` 以 `request_workdir()` 包裹每次分发，finally 清理；`serve()` 启动时以 manifest name `files.configure(plugin_name=...)`。`minio` 包在 files.py 内惰性 import，SDK 基础导入不依赖它。

**Task 0.3 完成**（file_params + 输出迁移）。逐插件审计结论：

| 插件 | 文件入参 | 文件输出 | 处置 |
|------|----------|----------|------|
| parse | `file_path` | 无（返回 dict） | `file_params=["file_path"]` + `resolve_file`；删 `*_UPLOAD_DIR` allowed-root 检查 |
| anydoc | `file_path` | 无（返回 markdown 文本） | 同上 |
| annotate | `source`（路径或 base64 二态） | 批注 docx | `file_params=["source"]` + `resolve_file`（base64 直通，`docannot._try_base64` 对齐类型判断）；输出主路径改 `put_file` + `storage.presign_get`，`storage.put` 保留为回退 |
| template / search / check_format / check_content / detect_plagiarism | 无（文本/dict/ES/反向 RPC 取数） | 无 | 零改动 |

偏差与实测：
1. **沙箱边界整体上移至主进程**（ProxyTool fail-closed 校验，Phase 2）：插件侧 `*_UPLOAD_DIR` allowed-root 检查删除，本地路径直通（仅测试/同机开发出现）。parse/anydoc 旧沙箱测试改写为新语义（missing file 拒绝、本地文件直通、minio:// 走下载桩），escape/优先级用例删除——其断言由 Phase 2 主进程侧测试接管。
2. annotate 命名修复：批注输出文件名 stem 改为按"路径形态"推导（base64 输入得名 `annotated`），修复了旧代码对 base64 输入产出垃圾文件名的问题。
3. 实测：三插件注册 caps 均带 `file_params` 且对应 property 标 `format: file-ref`；`tests/plugin/` 279 passed, 1 skipped。
