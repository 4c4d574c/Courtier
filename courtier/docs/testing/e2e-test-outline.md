# Courtier 端到端（E2E）测试大纲与测试用例

> 基线：main 分支 @ 2026-08-27（接口名称、参数、限流额度、SSE 协议均按当日源码核实，来源见文中括注文件）。
> 适用系统：Courtier + docaudit 域包（公文智能审计，GB/T 9704-2012），含 Vue 3 前端 `webui`。

---

# 第一部分　测试大纲

## 1. 目的与范围

### 1.1 目的

在真实部署形态（MySQL + MinIO + Elasticsearch + 8 个独立插件进程 + FastAPI 主应用 + Vue 前端）下，验证"用户上传公文 → 智能体编排审计 → 流式呈现结论"这一完整业务链路的功能正确性、协议健壮性与安全隔离性，形成可持续回归的测试资产。

### 1.2 范围内

| 维度 | 内容 |
|---|---|
| 部署与环境 | docker-compose 基础栈、插件启动脚本、MinIO 传输桶初始化、主应用启动迁移 |
| 业务功能 | 纯对话、文档问答、格式审计（GB/T 9704）、内容合规、查重、涉密判别、批注、检索、模板 |
| 编排内核 | 域自激活（activate_domain）、SkillTool 子代理调度、typed 输入、artifact |
| 协议健壮性 | SSE 事件协议、断线重连、水印回放、resync、停止/暂停/排队/重启清扫 |
| 安全 | JWT 认证、审批流、越权隔离、限流、refresh token 轮换与重用检测、插件通道令牌、传输桶隔离 |
| 观测性 | /metrics、Prometheus/Grafana、Langfuse 链路追踪 |

### 1.3 范围外（不在本大纲内）

- 单元/组件级行为（由 `uv run pytest` 与 `webui npm test` 覆盖）。
- 插件内部算法精度（规则命中准确性属 `libs/docaudit` 的评测范畴，本大纲只验"链路通、结论合理"）。
- 生产容量压测、长稳浸泡测试（仅抽样验证并发队列上限逻辑）。
- 跨主机分布式部署拓扑（插件 standalone 部署方案另见其计划文档）。

## 2. 被测系统链路速览

```
浏览器(webui) ──HTTP/SSE──► FastAPI(/api, JWT cookie/Bearer) ──► RunManager(后台 run, FIFO)
                                                      │                    │ RunRecorder ──► MySQL(会话/事件水位)
                                                      │                    ▼
                                                      │             Agent Loop(Think→Act→Observe)
                                                      │               ├─ activate_domain ──► 域工具/技能 SkillTool
                                                      │               ├─ 共享代理工具 convert/search/annotate/template
                                                      │               └─ 域代理工具 parse_layout/check_format/
                                                      │                  check_content/detect_plagiarism
                                                      ▼
                          NotificationHub ──GET /api/events──► 全局徽标/toast
插件进程(JSON-RPC/TCP 9101~9108) ⇄ 受限 MinIO(courtier-plugin-io) / ES / OCR / LLM
```

## 3. 测试环境

### 3.1 环境组成

| 组件 | 本地端口 | 说明 |
|---|---|---|
| MySQL 8.4 | 127.0.0.1:3366 | 会话/用户/审批持久化（compose 服务 `db`） |
| MinIO | 9000 / 9001(console) | 对象存储 + 插件传输桶 `courtier-plugin-io` |
| Elasticsearch 8.x | 127.0.0.1:9200 | 检索（xpack.security 开启，`$ES_PASSWORD`） |
| LLM（OpenAI 兼容） | `LLM_IP` | 编排器与子代理推理；embedding 同端点 |
| OCR 服务 | `DOCPARSE_OCR_API_URL` / `ANYDOC_OCR_API_URL` | 扫描件/图片解析的外部依赖 |
| 插件 ×8 | 127.0.0.1:9101–9108 | parse/anydoc/annotate/template/search/check_format/check_content/detect_plagiarism |
| 主应用 | :8000 | `uv run main.py`（先跑 Alembic 迁移） |
| webui dev | :5173 | Vite dev server，`/api` 代理到 8000 |
| 观测栈 | Grafana :3001、Langfuse :3000、Prometheus :9090、OTLP :4317/:4318 | compose 一键起 |

### 3.2 环境搭建步骤（全新机器）

```bash
cd courtier

# 1. 基础设施与观测栈
docker-compose up -d          # db/minio/es/plugin-*/otel/langfuse/prometheus/grafana

# 2. Python 依赖（离线环境注意 uv --offline 缓存）
uv sync

# 3. 配置
cp .env.example .env          # 填 MYSQL_URL、ES_PASSWORD、MINIO_*、LLM_IP/LLM_API_KEY、JWT_SECRET、ADMIN_PASSWORD
                              # 注意：上传目录变量名为 UPLOAD_DIR（.env.example 中注释的
                              # COURTIER_UPLOAD_DIR/DOCAUDIT_UPLOAD_DIR 别名在代码中未实现）
cp plugins/plugin.env.example plugins/plugin.env   # 填 COURTIER_PLUGIN_TOKEN（与 .env 一致）及各插件外部依赖

# 4. 迁移 + MinIO 插件账号初始化（幂等）
uv run alembic upgrade head
COURTIER_PLUGIN_MINIO_SECRET=<secret> uv run scripts/minio_plugin_io.py

# 5. 各插件 venv（首次）
for d in plugins/*/; do [ -f "$d/pyproject.toml" ] && (cd "$d" && uv sync); done

# 6. 启动插件与主应用（两个终端）
python scripts/dev-plugins.py            # 读 plugins/plugin.env 起 8 个 TCP 插件
uv run main.py                           # 自带 alembic upgrade head；--host/--port/--reload 可选

# 7. 前端
cd webui && npm ci && npm run dev        # E2E 手工测试用 dev server；验收构建用 npm run build
```

### 3.3 环境自检清单（所有用例的前置闸门）

- [ ] `curl -s http://127.0.0.1:8000/health` → `{"status":"ok"}`
- [ ] `scripts/dev-plugins.py` 日志中 8 个插件各自监听成功、无退出
- [ ] （admin 登录后）`GET /api/admin/extensions/plugins` 全部 ACTIVE，无 BLOCKED/DISCONNECTED
- [ ] `GET /health`、`GET /metrics` 可访问
- [ ] LLM 端点连通：任一纯聊天用例能产出 conclusion
- [ ] OCR 服务连通：任一图片样本能转出 Markdown（SHD-001 变体）

### 3.4 测试数据准备（样本文档集）

> 违规样本的设计须先对照库内规则文件，保证可命中：
> 格式规则 `libs/docaudit/validator/validator/rules/*.json`，内容合规规则 `libs/docaudit/content_compliance/content_compliance/rules/*.json`。

| 编号 | 样本 | 格式 | 特征 | 主要用途 |
|---|---|---|---|---|
| D1 | 合规通知《XX 关于××的通知》 | docx | 符合 GB/T 9704 版式（版头/主体/版记齐全） | FMT-001 及各链路正路径 |
| D2 | 违规通知 | docx | 人为制造 ≥3 处规则命中项（按上述 rules 设计，如成文日期格式、页边距、附件标注等） | FMT-002、FMT-005 |
| D3 | 合规通知版式件 | pdf | 版式正确、可解析出页眉/正文/页脚 | FMT-004 |
| D4a | 扫描件通知 | pdf(扫描) | 图像清晰可 OCR | CNT-003 |
| D4b | 公文照片 | jpg/png | 手机拍照质量 | SHD-001 OCR 分支 |
| D5 | 内容问题稿 | docx | 含已知错别字 ≥2 处、不规范表述 ≥1 处（对照 content_compliance rules） | CNT-001/002 |
| D6-L1/L2 | 对照库文献 ×2 | docx/pdf | 入资源库作比对文献 | SIM-*（经 /api/resources/upload 注入） |
| D6-T | 高相似投稿 | docx | 与 L1 连续相同表述超过阈值（detect_plagiarism `min_substring_length` 默认 20 字符之上量级） | SIM-001 |
| D6-N | 原创投稿 | docx | 与库内无雷同 | SIM-002 |
| D7a/b | 非法样本 | .txt / >50MB 的 pdf | 触发扩展名白名单与大小上限 | FILE-002/003 |
| D8 | 涉密特征稿 | docx | 含密级标注/涉密事项特征文本 | SECRET-001 |

### 3.5 执行约定与注意事项

1. **限流按客户端 IP 计**（`api/rate_limiter.py`）。手工密集调试易撞 429（login 5/min、新建会话 10/min 等）；撞线后可重启主应用清零（slowapi 内存态），或在用例间留间隔。执行大批量 API 用例时固定使用同一出口 IP，避免相互挤兑。
2. **登录锁定**：连续失败 10 次锁 15 分钟——AUTH-002 执行专用账号，勿用 admin。
3. **SSE 断言方式**：帧格式为 `id: <seq>\ndata: {json}\n\n`，序号在 `id:` 行而非 data 字段；用 `curl -N` 直读。浏览器侧凭据走 httpOnly cookie，curl 侧用 `Authorization: Bearer`。
4. **新建会话连接是 live-only**（不带回放）；只有 attach 连接（`/api/sessions/{id}/events` 或原生重连）才有回放语义。
5. `MAX_RUNS_PER_USER`（默认 3）与 `MAX_TOTAL_RUNS`（默认 20）不在 `.env.example` 中，CTRL 组用例需手动写入 `.env` 后重启。
6. LLM 输出具非确定性：对自然语言结论**不做全文断言**，断言聚焦结构性事实（是否给出结论、是否点名了植入的问题要素、数字与页码引用是否正确）。
7. 测试顺序建议：ENV → AUTH → FILE → RUN → DOM → 业务五线（FMT/CNT/SIM/SHD/SECRET）→ SSE/CTRL/PLG 故障类 → SEC → UI → OBS。故障注入组做完必须重启环境再回归正路径一遍。

## 4. 测试策略

- **优先级**：P0＝冒烟主干，每次发版必跑且 100% 通过；P1＝核心回归；P2＝增强项，允许带缺陷跟踪遗留。
- **方法**：黑盒功能测试为主（API 层 curl/脚本 + UI 层人工/浏览器自动化），故障注入采用进程级 kill、拔网线式断流、篡改 env 等手段。
- **通过准则**：P0 全过、P1 通过率≥95% 且无 S1/S2 级未决缺陷、P2 缺陷均已登记。
- **缺陷分级**：S1 主流程阻断且无绕过；S2 功能受损但有绕过；S3 次要功能/体验问题；S4 优化建议。
- **回归策略**：任何触及 agent loop / RunManager / SSE 通道 / 插件 SDK 的改动，SSE+CTRL+PLG 三组必回归；触及 prompt/技能定义的改动，业务五线必回归。

## 5. 测试模块一览

| 模块 | 名称 | 用例数 | 说明 |
|---|---|---|---|
| M0 | ENV 环境与部署 | 6 | 组合根：基础设施、插件、应用、前端产物 |
| M1 | AUTH 认证与账户 | 8 | 登录/注册审批/JWT/刷新/登出/用户管理 |
| M2 | FILE 文件上传 | 5 | 白名单/大小/MIME/鉴权 |
| M3 | RUN 会话与运行核心 | 8 | 生命周期/续聊/编辑重发/压缩/fork/列表 |
| M4 | DOM 域激活与技能调度 | 5 | activate_domain/子代理/扩展清单/开关 |
| M5 | FMT 格式审计 | 4 | GB/T 9704 正反样本/文种/批注闭环 |
| M6 | CNT 内容合规 | 3 | 纠错/合规/OCR 输入 |
| M7 | SIM 抄袭检测 | 3 | 高相似/原创/库动态补齐 |
| M8 | SHD 共享能力 | 5 | anydoc/search/read_chunks/annotate/template |
| M9 | SECRET 涉密判别 | 1 | auto 模式判级 |
| M10 | SSE 流可靠性 | 7 | 协议结构/回放/Last-Event-ID/resync/全局流/重启清扫 |
| M11 | CTRL 停止·暂停·队列 | 7 | stop/pause-resume/FIFO/总量兜底 |
| M12 | PLG 插件通道容错 | 5 | 断连/令牌/超时/传输桶数据面 |
| M13 | SEC 安全与隔离 | 6 | 越权/限流/metrics 保护/桶隔离 |
| M14 | UI 前端端到端 | 14 | 页面与交互全流程（可与 API 组并行执行） |
| M15 | OBS 观测性 | 3 | metrics/Grafana/Langfuse |

---

# 第二部分　测试用例

> 格式约定：每条用例含【前置】【步骤】【预期】。【前置】中"T-{ID}"指取得 access token（附录 A）；样本文档引用 §3.4 的 Dx 编号。

## M0　ENV 环境与部署

#### ENV-001 docker-compose 基础栈健康 ★P0
- 前置：已完成 `.env` 配置。
- 步骤：
  1. `docker-compose up -d` 后 `docker-compose ps`。
  2. 分别探测 db(3366)、MinIO(9000/console 9001)、ES(9200 带 basic-auth)、Grafana(3001)、Langfuse(3000)、Prometheus(9090)。
- 预期：
  1. 除注释掉的 app 服务外全部 Up/healthy；
  2. ES 需密码方可读写（匿名 401）；三个观测入口均出登录页。

#### ENV-002 主应用启动与自动迁移 ★P0
- 前置：ENV-001 通过；数据库为空或落后版本。
- 步骤：
  1. 记录 `alembic current` 版本号；执行 `uv run main.py`。
  2. 观察启动日志中 Alembic 输出与 Uvicorn 监听。
  3. `curl -s http://127.0.0.1:8000/health`。
- 预期：
  1. 启动前自动执行 `upgrade head`，版本推进到 head；
  2. Uvicorn 于 0.0.0.0:8000 就绪；
  3. 返回 `{"status":"ok"}`；`alembic current` 与 head 一致。

#### ENV-003 MinIO 插件传输桶初始化幂等 ★P0
- 前置：MinIO up；`.env` 含 MINIO_ROOT_*。
- 步骤：
  1. `COURTIER_PLUGIN_MINIO_SECRET=<secret> uv run scripts/minio_plugin_io.py` 第一遍执行；
  2. 完全相同的命令再执行第二遍；
  3. 加 `--verify` 执行隔离校验。
- 预期：
  1. 创建 24h 生命周期的 `courtier-plugin-io` 桶、最小权限策略与受限用户；
  2. 第二遍无报错、对象保持幂等（不重复创建报错）；
  3. `--verify` 通过（受限账号仅能触碰传输桶）。

#### ENV-004 插件一键启动与注册 ★P0
- 前置：各插件目录已 `uv sync`；`plugins/plugin.env` 就绪（TOKEN 与 .env 一致）。
- 步骤：
  1. `python scripts/dev-plugins.py`；
  2. 观察每个插件的启动行（含名字前缀与 `--listen 127.0.0.1:<port>`）；
  3. admin 登录后 `GET /api/admin/extensions/plugins`。
- 预期：
  1. parse/anydoc/annotate/template/search/check_format/check_content/detect_plagiarism 八个进程全部驻留；
  2. 端口与各自 plugin.yaml 的 runtime.port（9101–9108）一致；
  3. 扩展接口中八者状态均为 ACTIVE。

#### ENV-005 域包校验 ★P1
- 步骤：`uv run courtier validate-domain domains/docaudit/`。
- 预期：校验通过；requires_plugins（parse/template/search/annotate/detect_plagiarism/check_format/check_content）与服务（mysql/elasticsearch/minio）均满足。

#### ENV-006 前端生产构建与静态托管 ★P2
- 步骤：
  1. `cd webui && npm ci && npm run build`；
  2. 按 Dockerfile/静态挂载条件使 `$COURTIER_REPO_ROOT/static` 生效后重启主应用；
  3. 浏览器打开 `http://127.0.0.1:8000/`。
- 预期：vue-tsc 无类型错误、构建成功；根路径返回前端页面，深度路由刷新不 404。

## M1　AUTH 认证与账户

#### AUTH-001 管理员登录成功 ★P0
- 前置：bootstrap 完成（ADMIN_USER/ADMIN_PASSWORD 已设置）。
- 步骤：
  1. `POST /api/auth/login` 携带正确账密；
  2. 检查响应体与 Set-Cookie；
  3. 用返回 token 调 `GET /api/profile`。
- 预期：
  1. 200，`{token, token_type:"bearer", expires_in:900, user:{role:"admin",status:"active"}}`；
  2. 种下 httpOnly `access_token`(path=/api) 与 `refresh_token`(path=/api/auth, SameSite=Strict)；
  3. Bearer 方式取到本人资料。

#### AUTH-002 登录失败与连续失败锁定 ★P1
- 前置：专用测试账号 audit-t（避免污染 admin）。
- 步骤：
  1. 错误密码登录，观察响应码与报文；
  2. 连续失败至 10 次；
  3. 第 11 次（含立即重试）改用**正确**密码登录。
- 预期：
  1. 失败返回 401（凭证错误类提示，不泄漏存在性差异）；
  2. 前 9 次均为普通 401；
  3. 第 10 次起进入约 15 分钟锁定，正确密码亦被拒；等待期满后恢复（可用较短等待确认仍锁定即可结束用例）。

#### AUTH-003 注册→审批→通过/拒绝双分支 ★P1
- 前置：admin 已登录（T-admin）。
- 步骤：
  1. `POST /api/auth/register` 注册 user-a、user-b；
  2. 立即尝试 user-a 登录；
  3. T-admin `GET /api/admin/approvals` 查看待审批；
  4. 批准 user-a；拒绝 user-b；
  5. user-a 登录；user-b 登录。
- 预期：
  1. 注册返回成功，账号 status=pending；
  2. user-a 登录被拒（pending）；
  3. 待审批名单含两账号；
  4. 审批操作返回成功；
  5. user-a 登录成功；user-b 依旧被拒（rejected 不可登录）。

#### AUTH-004 注册约束与限流 ★P2
- 步骤：
  1. 用已被占用的用户名注册；
  2. 用非法用户名（正则外字符）/短密码（<8 位）注册；
  3. 第 4 次注册（同小时内）。
- 预期：
  1. 409；2. 400 且字段级错误信息；3. 429（register 限 3/hour）。

#### AUTH-005 未认证访问保护接口 ★P0
- 步骤：无凭据调用 `GET /api/sessions`、`POST /api/files`、`GET /api/events`。
- 预期：三者均 401。

#### AUTH-006 refresh 轮换与重用检测 ★P1
- 前置：已登录并有 refresh_token cookie。
- 步骤：
  1. `POST /api/auth/refresh` 换新 access；记录新旧 refresh cookie；
  2. 用旧 refresh 再次 refresh；
  3. 再用最新 refresh 第三次 refresh。
- 预期：
  1. 成功，返回新 access 且 Set-Cookie 轮换了 refresh_token（rotation）；
  2. 重用检测生效：旧 token 被拒，且该用户全家 refresh 被撤销；
  3. 第三步即使用最新 cookie 也 401（整族已撤销），需重新登录。
  - 备注：access 15 分钟到期后的静默续期可在 UI（UI-01）长会话中顺带验证；不必干等 15 分钟。

#### AUTH-007 登出 ★P1
- 步骤：登录 → `POST /api/auth/logout` → 检查 cookie 与随后的受保护请求。
- 预期：返回 `{"message":"已登出"}`；两个 cookie 被清除；原 access token 无法继续访问受保护接口。

#### AUTH-008 管理员用户管理边界 ★P1
- 步骤：
  1. T-admin `PATCH /api/admin/users/{uid}` 修改 user-a 的 email；
  2. 修改 user-a 的 password 后以新密码登录；
  3. 尝试将自己 role 改为 auditor / 自己 status 改为 disabled；
  4. 普通 auditor 账号访问 `GET /api/admin/users`。
- 预期：
  1. 200 更新生效；
  2. 新密码可登录；
  3. 400（禁止自我 role/status 变更）；
  4. 403（require_admin）。

## M2　FILE 文件上传

#### FILE-001 合法文件上传 ★P0
- 步骤：T-user 分别上传 D1(docx) 与 D4b(jpg)：`POST /api/files`，multipart 字段 `file`。
- 预期：均 200 且返回 `{"fileId":"..."}`；重复上传同名文件也各自获得独立 fileId；响应立即可用于 `GET /api/sessions?task=..&fileId=..`。

#### FILE-002 不支持的扩展名 ★P0
- 步骤：上传 D7a(.txt) 与任意 .exe 改名件。
- 预期：400，报文提示"不支持的文件格式"；未产生持久化文件。

#### FILE-003 超大小上限 ★P1
- 步骤：上传 D7b（>50MB 的真 pdf）。
- 预期：413"文件过大"；52,428,800 字节阈值恰好之下的合法 pdf 可上传（可选对照）。

#### FILE-004 伪装 MIME（magic-byte 校验）★P2
- 步骤：将 .png 扩展名的 docx 内容以 `application/octet-stream` 上传。
- 预期：400（魔数不符；octet-stream 退化走 magic-byte 检查）。反向对照：真实的 png 以 octet-stream 上传应成功。

#### FILE-005 未登录上传 ★P1
- 步骤：无凭据 `POST /api/files`。
- 预期：401。

## M3　RUN 会话与运行核心

#### RUN-001 纯文本问答完整生命周期 ★P0
- 步骤：
  1. `curl -N "…/api/sessions?task=你是谁"` 跟读事件流；
  2. 记录出现的事件类型与首事件、末事件。
- 预期：
  1. 首事件为 `session{sessionId(sess_*, modelName)}`；
  2. 过程中出现 `think`、`token`（或有）、`conclusion_token`、`usage{tokensIn,tokensOut}`、`loop_completed{status,…,totalSteps}`；
  3. 终态事件 `complete{conclusion 非空, tokensIn, tokensOut}`，随后流正常关闭；
  4. `id:` 行序号沿流单调递增。

#### RUN-002 多轮续聊上下文保持 ★P0
- 步骤：
  1. 会话首轮给出一段自定义信息（如"记住暗号 A1B2"）；
  2. 同 sessionId 以 `task=暗号是什么` 续问。
- 预期：第二轮 conclusion 能复述暗号；两条消息落同一 sess_ 会话详情（`GET /api/sessions/{id}` 可见完整历史）。

#### RUN-003 同会话并发发起冲突 ★P1
- 步骤：running 期间对同一 sessionId 再次发起新轮。
- 预期：409"会话正在运行或排队…"；原 run 不受影响直至正常终态。

#### RUN-004 编辑重发（editTurn）★P1
- 步骤：
  1. 完成两轮对话；
  2. `GET /api/sessions?task=<修正后的第一轮>&sessionId=<sid>&editTurn=0`；
  3. 查询会话详情核对消息数与内容。
- 预期：该轮之后的历史被截断并以新输入重跑；editTurn=0 时标题同步为新 task。

#### RUN-005 手动上下文压缩 ★P2
- 步骤：
  1. 对空会话调用 `POST /api/sessions/{id}/compact`；
  2. 对有若干轮历史的会话调用同一接口。
- 预期：
  1. 400（空历史）；
  2. 200 返回 `{beforeTokens,afterTokens,beforeMessages,afterMessages,compactCount}`，after≤before。

#### RUN-006 fork / rewind ★P2
- 步骤：对多轮会话分别调用 `POST …/fork`（含 node_id）与 `POST …/rewind`（指定 node_id）；rewind 缺 node_id 时复测一次。
- 预期：fork 生成可切换的新分支节点；rewind 使会话回退到目标节点之后的状态；node_id 缺失时 400/422 类拒绝。

#### RUN-007 会话列表实时状态 ★P1
- 步骤：
  1. 发起一个较长运行（上传 D1 要求格式审计）；
  2. 运行中轮询 `GET /api/sessions`（间隔 ≥6 秒防限流）观察该会话 status；结束后再查一次。
- 预期：运行中被实时标记 running/queued（含 modelName/task/stepCount/toolCount/issueCount 元信息）；结束后转为 completed。

#### RUN-008 重命名/置顶/删除 ★P1
- 步骤：
  1. `PATCH /api/sessions/{id} {"task":"回归样本", "pinned":true}`；
  2. `PATCH` 空 body；
  3. `DELETE /api/sessions/{id}` 后再次 `GET /api/sessions/{id}`。
- 预期：
  1. 200；列表中该会话置顶并显示新标题（截断至 ≤200 字符）；
  2. 400（没有需要更新的字段）；
  3. 删除返回 ok，复查 404；关联的未共享缓存文件被 GC（uploads/cache 目录无残留增长）。

## M4　DOM 域激活与技能调度

#### DOM-001 域自激活元工具 ★P0
- 前置：T-user 已上传 D1 取得 fileId。
- 步骤：
  1. `curl -N "?task=审核这份通知的格式规范&fileId=<fileId>"` 跟读全程；
  2. 检索事件流中 activate_domain 的相关事件（think 的 toolCalls / tool_start / tool_result）。
- 预期：
  1. 会话早期出现 `tool_result{name:"activate_domain", status:"ok"}`；
  2. 激活后同流内后续出现域工具调用（parse_layout/check_format 或技能子代理）；
  3. 无需任何手动注册动作（自激活），共享阶段看不到域工具（负检查：激活前的 act 不含 check_format）。

#### DOM-002 技能以子代理形态调度 ★P0
- 前置：DOM-001 同一持续运行的会话流。
- 步骤：继续观察/复跑上一步的事件流。
- 预期：
  1. 出现 `subagent_start` 与配对的 `subagent_end`，其间有 `subagent_think/token/tool_result/conclusion`；
  2. 汇聚的父级 `tool_result` 满足 `callKind:"subagent_run"`、`callScope:"subagent"`、`subagentName` 为具体技能名（如 format_audit）、status=ok、duration 有值；
  3. 前端组件层面（对照 UI-05）渲染出嵌套节点。

#### DOM-003 扩展清单一致性 ★P1
- 前置：T-admin。
- 步骤：
  1. `GET /api/admin/extensions/plugins`；
  2. `GET /api/admin/extensions/skills`；
  3. `GET /api/admin/extensions/skills/format_audit`。
- 预期：
  1. 八个插件在册且状态与真实连接一致；
  2. docaudit 五技能在册：format_audit/content_audit/plagiarism/secret_analysis/full_government_audit，其中 full_government_audit 默认 enabled:false；
  3. 详情含 input_model/typed 字段描述（document、doc_type 等）。

#### DOM-004 多技能协同综合任务 ★P1
- 步骤：上传 D5，`task=对这份文件做内容纠错并和资料库比对是否抄袭`，跟读到 complete。
- 预期：
  1. 链路上依序/并行出现 content_audit 与 plagiarism 两条子代理支线（各带自己的工具）；
  2. conclude 的 conclusion 同时包含纠错发现与查重比对结论两个部分；
  3. `loop_completed.totalSteps` 与实际步骤数吻合。

#### DOM-005 管理 Face 开关生效 ★P1
- 前置：T-admin；新开一个干净会话作对照基线（先复跑 FMT-001 成功）。
- 步骤：
  1. `POST /api/admin/extensions/skills/plagiarism/enabled {"enabled":false}`（body 按接口实参）；
  2. 新会话请求查重类任务；
  3. 重新开启后再试一次。
- 预期：
  1. 开关操作成功；
  2. 新会话不再调度 plagiarism 子代理（编排器给出合理的拒绝/改道回复，不得悬挂）；
  3. 恢复后能力重新可用。域级开关（`POST /domains/{name}/enabled`）以同样方式抽查一次。

## M5　FMT 格式审计（GB/T 9704）

#### FMT-001 合规样本通过 ★P0
- 前置：D1 已上传；chore 参照《党政机关公文格式》标准设计且经 rules 全绿（开发侧曾单测通过）。
- 步骤：`task=审核这份通知的格式规范&fileId=` 跑完整个会话。
- 预期：
  1. 链路触发 load_template（或域内等价取默认模板）→ parse_layout → check_format；
  2. conclude 给出"格式总体符合规范"性质的明确结论；
  3. 无 error/stopped 终态。

#### FMT-002 违规样本检出 ★P0
- 步骤：换 D2 重跑；收集 conclude 文本与 `issueCounts`。
- 预期：
  1. conclusion 至少点名 ≥3 处人为植入的违规要素且逐条可核对（表述可不同）;
  2. 结论不含对不存在问题的臆造指控（抽样复核 2 条）；
  3. `tool_result(check_format)` status=ok。

#### FMT-003 文种（doc_type）影响判定 ★P1
- 前置：同 D2 另存一份并把其中一条问题的性质改成文种相关差异（例如把"通知"改批成"函"，或反之）。
- 步骤：分别在 task 中声明不同文种重跑两次。
- 预期：两次结论对"适用文种/对应要素"的评价随之改变，其余一般项保持稳定；证明 doc_type 参数贯通 parse→template→check_format。

#### FMT-004 版式三段式解析 ★P1
- 前置：D3(pdf) 已上传。
- 步骤：task 指明"这份文件的版头/主体/版记布局是否正确"，跑完后检查 check_format 所依赖的解析质量（由结论回推）。
- 预期：parse_layout 链路 status=ok；结论能引用到具体的页眉/正文/页脚位置要素（说明 Document dict 的 pages[].page_content{header,body,footer} 投影生效）。

#### FMT-005 违规批注闭环 ★P1
- 前置：D2 会话进行中或紧接 FMT-002；MinIO 传输桶就绪（ENV-003）。
- 步骤：在同一会话追加 `task=把上述问题直接批注到原文并给我下载链接`。
- 预期：
  1. 出现 annotate_document 调用（rules 含 keyword/comment 对）；
  2. conclusion 中的 `download_url` 可被 curl 下载（presign_get 颁发的过期时间内）；
  3. 打开批注文件可见关键字批注成立。

## M6　CNT 内容合规审计

#### CNT-001 错别字纠正 ★P0
- 步骤：上传 D5，`task=校对这份文稿的文字和表述`。
- 预期：
  1. convert_document 先行产出 Markdown（tool_result ok）；
  2. conclusion 至少指出 2 个植入错别字的正确改法（位置±一句容差）；
  3. 全程无需要干预的错误终态。

#### CNT-002 不规范表述与合规建议 ★P1
- 步骤：紧接 CNT-001 会话，`task=从公文的规范性角度还有什么建议？`。
- 预期：conclusion 覆盖至少 1 处植入的不规范表述；建议属公文体裁常识范畴（无需精确措辞一致）。

#### CNT-003 扫描件输入 ★P1
- 步骤：上传 D4a（扫描 pdf），`task=读取并校对这份文件`。
- 预期：OCR 分支被触发（convert_document 返回 ocr/pages 元信息或耗时显著体现）；纠错结论基于 OCR 文本且对明显低质区域给出的核对提示合理；总时长低于插件 timeout(300s/600s)。

## M7　SIM 抄袭检测

#### SIM-001 高相似文本检出 ★P0
- 前置：D6-L1/L2 已入资源库（SEE ALSO M8/SHD-005），D6-T 已上传。
- 步骤：`task=检查这份文稿是否存在抄袭（与资料库比对）&fileId=D6-T`。
- 预期：
  1. 链路出现 search_documents（候选召回）与 detect_plagiarism（比对）两级调用；
  2. conclusion 判定为高相似并定位到与 L1 相似的段落；
  3. `tool_result(detect_plagiarism)` status=ok。

#### SIM-002 原创文本不误伤 ★P2
- 步骤：D6-N 重复同样的任务。
- 预期：conclusion 判定未见显著相似/查重通过；无高置信度的误导性告警。

#### SIM-003 对照库缺省时动态召回 ★P1
- 步骤：在 task 中限定 top_k 之外的题材关键词（或资源库仅有 L1/L2 时）提交查询性任务。
- 预期：搜索限定默认 limit（≤50）内返回；结论如实反映"对照范围内未发现/发现"的证据基础，不虚构库外来源。

## M8　SHD 共享能力（anydoc / search / annotate / template）

#### SHD-001 convert_document 万能转换 ★P0
- 步骤：上传 D4b(图片)，`task=把图片里的内容整理成文字给我`。
- 预期：convert_document 走 OCR 分支并返回可读 Markdown；格式字段含 markdown/format/ocr/pages 之类的元信息；`output_artifact_type=core.document_markdown` 链路成立（后续追问"第一章写了什么"可直接回答）。

#### SHD-002 search_documents + read_chunks 定位 ★P1
- 前置：资源库已有 D6-L1/L2（经 SHD-005）。
- 步骤：`task=资料库里关于<主题词>的内容有哪些？引用原文细节说明`。
- 预期：search_documents 命中相应 chunk；如跟进细读则 read_chunks（≤10 chunks）参与；answer 引用的文段确属上传文献内容（抽 1 条核对）。

#### SHD-003 annotate_document 批注下载 ★P1
- 已由 FMT-005 覆盖（那是域视角的本体用例），此处仅回归**共享入口**本身：不经域技能、直接要求"给这段文字加个'注意'批注"。
- 预期：照样产出 download_url 且可下载。

#### SHD-004 load_template 默认模板 ★P2
- 步骤：`task=加载通知类的标准模板看看`（域已激活）。
- 预期：load_template(doc_type=通知) 返回默认模板内容；template_store 宿主服务依赖生效。

#### SHD-005 资源库注入与管理 ★P1
- 前置：T-admin；资源库 ES 就绪。
- 步骤：
  1. `POST /api/resources/upload` multipart 上传 D6-L1 与 D6-L2（填 title/tags/publish_date）；
  2. `GET /api/resources/list`；
  3. `GET /api/resources/{rid}/pdf` 抽查原件预览；
  4. 普通 auditor 账号尝试 `DELETE /api/resources/{rid}`。
- 预期：
  1. 返回 `{id,title,chunkCount,charCount,visibility,status}`，chunkCount>0（切片入索引成功）；
  2. 两条均在列表中；
  3. 可下载原 pdf；
  4. 非 admin 删除被拒（403），admin 可删。

## M9　SECRET 涉密判别

#### SECRET-001 涉密特征判级 ★P2
- 步骤：上传 D8，`task=这份材料是否涉密？给出判断依据`。
- 预期：secret_analysis（mode=auto）给出分级倾向与理由；结论开放但必须有据（引用原文语句）。

## M10　SSE 流可靠性与恢复

#### SSE-001 事件协议结构完备性 ★P0
- 步骤：录制 FMT-001 全程原始 SSE 字节流（`curl -N -o run.sse`）。
- 预期：
  1. 每帧皆 `id:` + `data:` 两行空行结尾（除 resync 哨兵特例）；
  2. data JSON 的 type ∈ 附录 B 枚举；
  3. life-cycle 序列满足：session 最先、终态恰一个（complete/error/stopped 之一）、终态后流结束；
  4. usage/token 数值与 complete 一致（tokensIn/tokensOut 双方都出现时相等）。

#### SSE-002 attach 水印回放 ★P0
- 前置：RUN-001 式会话正在跑，记下此刻已见的最大 seq N。
- 步骤：另开终端 `curl -N "…/api/sessions/{sid}/events?since=N"`。
- 预期：首先回放 seq>N 的既有事件（内容与直播一致），无缝衔接 live 直到终态；`Cache-Control:no-cache`、`X-Accel-Buffering:no` 头存在。

#### SSE-003 Last-Event-ID 优先与容错 ★P1
- 步骤：
  1. 同一 attach 场景，带 `-H 'Last-Event-ID: <M>'` 与 query `since=<更小的N>`（M>N）同时给；
  2. 仅给非法值 `Last-Event-ID: abc`。
- 预期：
  1. 从 M+1 起回放（header 优先于 query）；
  2. 非法值不炸接口——回退到 query since 或持久化 watermark 语义。

#### SSE-004 resync 哨兵 ★P1
- 步骤：run 结束（grace 内）后，用一个远小于现存最老 seq 的 since（如 `since=0` 对比当前 first_seq）attach。
- 预期：响应首个事件为无合法 id 的 `{"type":"resync"}` 哨兵随即结流（data 行、无 `id:` 行），客户端语义为"窗口已被驱逐、请走快照恢复"。

#### SSE-005 终态宽限与过期 404 ★P2
- 说明：默认 grace=RUN_GRACE_SECONDS=600s，长时间等待成本高；该项允许降级为代码评审 + 抽检。
- 步骤：完成后 30 秒内 attach（应成功重放）；若可容忍等待，逾 600 秒后再 attach。
- 预期：期内可完整重放到终态；过期后 404（"会话没有可接续的运行"/"Session not found"），客户端回退快照。

#### SSE-006 全局事件流心跳与状态推送 ★P1
- 步骤：
  1. `curl -N "…/api/events"` 挂住静置 30 秒；
  2. 另一会话发起任务与终态，同时观察全局流。
- 预期：
  1. 每 25 秒一条 `: hb` 心跳注释帧；
  2. 收到 `{type:"run_status",sessionId,status,...}`，终态时附 conclusion 截断(≤200)/tokensIn/tokensOut 之一以上；
  3. 该流无 replay：主动断开重连后旧事件不再出现（靠列表对齐）。

#### SSE-007 重启清扫 interrupted ★P0
- 步骤：
  1. 发起长任务至 running；
  2. kill 主应用进程并立刻重启（`uv run main.py`）；
  3. `GET /api/sessions` 与会话详情检查该会话状态。
- 预期：重启后原 running/queued 会话被标记 interrupted（含 finished_at）；attach 其事件流得 404（in-process 运行丢失，启动清扫不改写其它状态）。

## M11　CTRL 停止·暂停·队列并发

#### CTRL-001 停止单个运行 ★P0
- 步骤：长任务 running 时 `POST /api/stop?sessionId=<sid>`。
- 预期：
  1. 返回 `{status:"ok",sessionId,stopped:true}`（属主校验：他人 404"未找到或无权限"）；
  2. 事件流以 `stopped` 终态收尾（≤5 秒内），session 状态 stopped；
  3. 未运行会话再 stop 得 404"未在运行"；并发插件调用被取消（plugin cancel_pending）。

#### CTRL-002 管理员全停 ★P1
- 步骤：admin 不带 sessionId 调 `/api/stop`；普通用户做同样操作。
- 预期：admin 返回 `{stoppedCount:N, sessionIds:[…]}`（无活跃时 `stopped:0` 附提示）；普通用户 403。

#### CTRL-003 全局暂停/恢复 ★P1
- 步骤：
  1. running 任务途中 `POST /api/pause`；
  2. 观察 60 秒事件流停滞；`POST /api/resume`；
  3. 普通用户分别调两接口。
- 预期：
  1. 返回 ok；2. 暂停期间该 run 不产新事件、不落新 seq（RunRecorder 写入阻塞语义），resume 后自动续跑至正常 complete；
  3. 非 admin 均 403。

#### CTRL-004 每用户 FIFO 排队 ★P0
- 前置：`.env` 设 `MAX_RUNS_PER_USER=2` 后重启。
- 步骤：同一用户快速串发 3 个会话任务（防限流间隔投放），各自抓首帧。
- 预期：
  1. 前两个进入 running；
  2. 第三个收到首事件 `queued{position}`，会话列表状态 queued；
  3. 一个前序 run 结束后，第三个自动升 running 且在新投递者之前保持优先；
  4. 恢复 `MAX_RUNS_PER_USER` 原值（或删除该行）重启还原。

#### CTRL-005 队列公平性（他人不被阻塞）★P1
- 前置：CTRL-004 环境维持限额 2。
- 步骤：用户 A 占满 2 个 slot 并排 1 个 queued；随后用户 B 首次投递任务。
- 预期：B 直接 running（受限用户的排队不阻塞他人）；B 结束后 A 的 queued 顺位晋升。

#### CTRL-006 停止排队中的运行 ★P1
- 步骤：对 CTRL-004 中 queued 的会话立即 `/api/stop?sessionId=`。
- 预期：出队即时生效；发起连接收到补发 `stopped` 终态；列表状态终化；释放的位置让后续排队者前移（其余 queued 若有则广播新 position）。

#### CTRL-007 总量兜底 MAX_TOTAL_RUNS ★P2
- 前置：`MAX_TOTAL_RUNS` 调小（如 3）重启。
- 步骤：多用户合计投满限额后再由新用户投递。
- 预期：超额投递进入 queued（不崩溃、不吞任务），额度释放后逐一晋升；完成后还原配置。

## M12　PLG 插件通道容错

#### PLG-001 进程被杀自动重连 ★P1
- 前置：全部插件 ACTIVE。
- 步骤：
  1. `kill` 掉 template 插件进程；
  2. 观察主应用日志 30–120 秒；
  3. 用 dev-plugins 单独拉起该插件；
  4. 新会话中请求模板类任务。
- 预期：
  1. 杀死后通道转 DISCONNECTED（扩展接口可见）；
  2. 主应用按指数退避持续重连（日志体现），应用整体存活；
  3. 重启后转回 ACTIVE；
  4. 功能恢复可用。

#### PLG-002 令牌不匹配拒绝 ★P1
- 前置：停环境，改 plugin.env 的 COURTIER_PLUGIN_TOKEN 与 .env 不一致。
- 步骤：先起插件后起主应用；观察两端日志与扩展状态；还原配置。
- 预期：`plugin.register` 握手被拒，永不转 ACTIVE（BLOCKED/DISCONNECTED 态），主应用健康不受损；还原则恢复。host 端 `plugin.auth` 方向同理可抽验（改 .env 一侧 token）。

#### PLG-003 插件中途死亡时的工具调用 ★P1
- 步骤：长 parse 任务进行到一半 `kill -9` parse 插件。
- 预期：该次工具调用以 `tool_result(status:"error")` 收敛（或会话以 error 终态收敛），≤timeout 上限时长内必有交代，绝不悬挂；主应用与其他插件不受牵连；随后重连恢复（联动 PLG-001）。

#### PLG-004 长 timeout 边界 ★P2
- 前置：OCR/LLM 可达但极慢（可用 tc 延迟或大文本灌入）。
- 步骤：投递接近 parse 插件 timeout_ms=600000 的任务。
- 预期：时限内正常返回或按超时报错，行为与 manifest 声明一致；不留僵尸连接。

#### PLG-005 传输桶数据面贯通 ★P1
- 前置：ENV-003 完成；D2 已在会话中。
- 步骤：完整走一遍 FMT-005（annotate 上传→presign URL 下载）；
  另抽查：对象生命周期标记为 24h（mc ilm ls courtier-plugin-io）。
- 预期：file-ref 在代理边界被改写为 `minio://` 引用、插件经 resolve_file 下发、put_file 上传与 presign_get 返回均工作；桶上对象带 24h 过期规则；受限账号无法触碰 `MINIO_BUCKET_DOCS` 等其他桶。

## M13　SEC 安全与隔离

#### SEC-001 跨用户会话隔离 ★P0
- 步骤：user-a 建 3 个会话；user-b 逐一 `GET /api/sessions/{ua_sid}`、对其 DELETE、PATCH、发起 attach、stop。
- 预期：user-b 一律 404（不泄漏对方会话存在性），无任何内容回显；user-a 本人不受影响；admin 可以代管（get_owned 的 admin 放行），应成功。

#### SEC-002 跨用户文件隔离 ★P1
- 步骤：user-a 上传文件得 fileId-fa；user-b 以 `?task=..&fileId=fa` 发起会话；user-b 再以伪造不存在 id 尝试。
- 预期：user-b 使用 fa 被 403"无权访问该文件"；伪造 id 404；owner 本人与 admin 正常。

#### SEC-003 接口限流 ★P1
- 步骤：1 分钟内连发 6 次 login（错误密码）、11 次新建会话 GET。
- 预期：分别在第 6、11 次前后收到 429（login 5/min、sessions 10/min）；429 报文体为 slowapi 标准超限提示；等待窗口过后恢复。

#### SEC-004 metrics 保护 ★P2
- 前置：设 `PROMETHEUS_METRICS_TOKEN=abc123` 重启。
- 步骤：裸 `GET /metrics`；带 `?token=abc123`；带错误 token。
- 预期：裸奔与错 token 401；正确 token 200 且 Prometheus 文本可被抓取（prometheus.yml 的目标配置对应）。

#### SEC-005 refresh 族谱撤销的攻击面收敛 ★P1
- 说明：AUTH-006 已验主线；此处补充并发面。
- 步骤：两客户端同时持同一 refresh cookie 并发各 refresh 一次（竞态重放）。
- 预期：至多一端成功，另一端 401 且触发族谱撤销；无"双活会话"残留。

#### SEC-006 插件侧横向移动面 ★P1
- 步骤：以插件受限 MinIO 账号直连 9000 尝试列举/读写非传输桶（docs/library、langfuse-events）与全局限额操作。
- 预期：一律 AccessDenied；传输桶之外无任何可达对象；DB 凭据在任何 plugin.env 中不存在（静态抽查 plugins/*/plugin.yaml 的 runtime.env）。

## M14　UI 前端端到端（Chrome 最新稳定版；dev server :5173）

#### UI-01 登录/登出与路由守卫 ★P0
- 步骤：
  1. 未登录直达 `/`、`/profile`；
  2. `/login` 输正确账密；
  3. 长闲置后回车触发一次 token 过期的续期路径（跨 15 分钟，可与别的用例穿插）；
  4. 头像菜单登出。
- 预期：
  1. 均被弹去 `/login?redirect=…`；
  2. 登录成功回跳 redirect 目标；主界面欢迎语"审衡智能体平台 / 审以明辨，衡以持正"可见；
  3. 过期自动 refresh 无感续期不掉线；
  4. 登出回到 /login，后退按钮不可回到内容页。

#### UI-02 纯对话流式渲染 ★P0
- 步骤：主页输入"介绍一下你自己"回车（Shift+Enter 应换行不发）。
- 预期：loading-dot 动画出现；Markdown 流式增量渲染；最后 normal 完成；Enter 空消息不发送。

#### UI-03 附件上传发起审计全流程 ★P0
- 步骤：
  1. "+"选择 D1（出现 FilePreviewDrawer 预览）；
  2. 输入任务发送；
  3. 观察 StepsMessage 思考过程、ToolCard 工具卡（含 display 名称与耗时/status）、conclusion 渲染。
- 预期：上传先得 fileId 再 connect；全程卡片按真实事件推进；结束后无悬浮 loading；文本可复制（对照 conclusion-copy 脚本的行为）。

#### UI-04 发送/停止一体化按钮 ★P1
- 步骤：运行中观察右侧圆钮变化并点击。
- 预期：发送箭头变停止方块；点击后会话以 stopped 卡片收尾（ChatStatusMessage"由用户手动停止"），按钮复原。

#### UI-05 步骤与子代理树渲染 ★P1
- 步骤：以 FMT-001 的会话界面为对象，展开各 step 与 SubagentNode。
- 预期：思考明细（text_response 或 tool_calls 清单+displayNames）逐层可见；子代理节点带 `[<技能名>]` 前缀的工具卡；进度与终态（ok/error）图标准确。

#### UI-06 侧边栏历史管理 ★P1
- 步骤：重命名、置顶、删除各演练一次；运行另一会话时观察徽标。
- 预期：变更即时反映；置顶排序生效；删除消失；运行中显示实时状态徽标（借助全局事件），折叠状态记忆于 localStorage。

#### UI-07 跨会话 toast ★P1
- 前置：当前停留在会话 X 界面。
- 步骤：另开会话 Y 发起任务后切回 X 视口；Y 到终态。
- 预期：右下角弹出 Y 的完成/失败 toast（8 秒自动消隐），点击可跳转到 Y；重新打开的页面 onReconnect 后列表对齐无幽灵态。

#### UI-08 刷新恢复与续接 ★P0
- 步骤：任务 running 时刷新页面（F5）→ 重新进入该会话；再试断网 10 秒（DevTools offline）后恢复联网。
- 预期：刷新后经 `restoreSession → attach?since=eventSeq` 无缝续播（先前内容不重不漏）；短暂断网由 EventSource 原生 Last-Event-ID 续传；连续 3 次重连失败才降级为"连接中断，请重试"横幅。

#### UI-09 排队可见性 ★P2
- 前置：MAX_RUNS_PER_USER=2。
- 步骤：触发第 3 个排队任务后停留界面。
- 预期：顶部/输入区出现"排队中，前面还有 N 个任务"提示条与 queue banner；position 变化实时更新。

#### UI-10 重启中断提示 ★P1
- 步骤：UI-08 的进行中会话遭遇主应用重启（联动 SSE-007），回到前台。
- 预期：显示"该任务因服务重启已中断，可重新发起"类提示（INTERRUPTED_HINT），并可一键重发。

#### UI-11 注册审批 UI 链路 ★P2
- 步骤：/register 提交 → 管理员 /admin/approvals 批准 → 重新登录。
- 预期：注册后界面明示待审批；管理员列表见新申请；批准后登录畅通（呼应 AUTH-003）。

#### UI-12 资源库页面 ★P2
- 步骤：/resources 上传 D6-L1（带元数据）、列表查看、PDF 预览、删除（admin）。
- 预期：对应 SHD-005 的图形化路径等价可用。

#### UI-13 个人中心改密 ★P2
- 步骤：/profile 修改邮箱与新密码（旧密码错误 vs 正确两个分支）。
- 预期：旧密码错则拒绝；正确则新密码下次登录生效。

#### UI-14 扩展管理页（admin）★P2
- 步骤：/admin/extensions 查看 8 插件状态与技能清单，开/关一项技能与 one 个域。
- 预期：与 DOM-003/DOM-005 的 API 行为一致的图形映射；异常状态（BLOCKED）可辨。

## M15　OBS 观测性

#### OBS-001 Prometheus 指标 ★P1
- 步骤：`curl -s http://127.0.0.1:9090/-/healthy`；运行一轮业务后 `GET /metrics` grep 应用指标名。
- 预期：Prometheus healthy；应用/API 指标存在且数值随流量变化。

#### OBS-002 Grafana 看板 ★P2
- 步骤：登录 :3001（GRAFANA_PASSWORD），打开数据源与面板。
- 预期：Prometheus 数据源连通；内置面板出图无空白 panel 报错。

#### OBS-003 Langfuse 链路追踪 ★P2
- 步骤：跑一轮 FMT-001，登录 :3000 查最近 traces。
- 预期：本次 run 的 trace 可见（模型调用树/时延/tokens），OTLP collector(:4317→langfuse /api/public/otel) 链路成立。

---

# 附录

## 附录 A　常用操作手册（curl）

```bash
BASE=http://127.0.0.1:8000

# 登录取 token（也可只用 cookie：加 -c jar.txt -b jar.txt）
TOKEN=$(curl -s $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"'"$ADMIN_PASSWORD"'"}' | jq -r .token)
AUTH="Authorization: Bearer $TOKEN"

# 上传
curl -s -H "$AUTH" -F file=@D1.docx $BASE/api/files      # => {"fileId":"..."}

# 新建会话（SSE，live-only）
curl -N -H "$AUTH" "$BASE/api/sessions?task=%E5%AE%A1%E6%A0%B8%E8%BF%99%E4%BB%BD%E9%80%9A%E7%9F%A5%E7%9A%84%E6%A0%BC%E5%BC%8F%E8%A7%84%E8%8C%83&fileId=<FILE_ID>"
# 纯聊天：                                   ?task=...
# 续聊：                                     ?task=...&sessionId=sess_xxxxxxxxxxxx
# 编辑重发：                                  &editTurn=0

# 附加到运行中的会话（replay+live）
curl -N -H "$AUTH" "$BASE/api/sessions/sess_xxx/events?since=37"
curl -N -H "$AUTH" -H 'Last-Event-ID: 37' "$BASE/api/sessions/sess_xxx/events"

# 全局状态流
curl -N -H "$AUTH" $BASE/api/events

# 停止 / 暂停 / 恢复
curl -s -X POST -H "$AUTH" "$BASE/api/stop?sessionId=sess_xxx"
curl -s -X POST -H "$AUTH" $BASE/api/pause ; curl -s -X POST -H "$AUTH" $BASE/api/resume

# 会话运维
curl -s -H "$AUTH" $BASE/api/sessions                       # 列表
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"task":"新标题","pinned":true}' $BASE/api/sessions/sess_xxx
curl -s -X DELETE -H "$AUTH" $BASE/api/sessions/sess_xxx
curl -s -X POST -H "$AUTH" $BASE/api/sessions/sess_xxx/compact

# 扩展观察（admin）
curl -s -H "$AUTH" $BASE/api/admin/extensions/plugins
curl -s -H "$AUTH" $BASE/api/admin/extensions/skills
```

## 附录 B　SSE 事件类型速查（按 source 实现归纳）

| 类别 | type | 要点 |
|---|---|---|
| 生命周期 | `session` | 首事件，携 sessionId/modelName |
| | `queued` | `{position}`，队列顺位变化时会重播更新 |
| 步骤 | `think` / `act` / `observe` / `step_verdict` | detail 区分 text_response 与 tool_calls:a,b |
| 工具 | `tool_start` / `tool_progress` / `tool_result` | tool_result 携 status(ok/error)/duration/summary/displayName/callKind(tool/subagent_run)/callScope(parent/subagent)/subagentName/issueCounts 等 |
| 流式文本 | `token` / `conclusion_token` / `usage` | usage={tokensIn,tokensOut} |
| 子代理 | `subagent_start/think/token/tool_result/conclusion/end` | 配对闭合 |
| 上下文 | `context_compacting` / `context_compacted` | |
| 守卫 | `guard_triggered` / `hint_injected` / `model_selected` / `model_fallback` | |
| 终局 | `loop_completed` → `complete` \| `error` \| `stopped` | 恰一个终态；error 带 detail/trace_id |
| 异常哨兵 | `resync` | 无 `id:` 行的特殊帧，示意水印失效转快照恢复 |

重放窗口边界：`RUN_LOG_MAX_EVENTS=50000` / `RUN_LOG_MAX_BYTES=8MB`，安全边界驱逐；终态后保留 `RUN_GRACE_SECONDS=600`s。

## 附录 C　关键配置速查（E2E 相关）

| 变量/常量 | 默认 | 备注 |
|---|---|---|
| ACCESS_EXPIRE | 900s(15min) | JWT HS256；承载顺序 Bearer > cookie access_token；?token= 查询参数仅限 SSE 路径（/api/sessions/、/api/events）与 /metrics（其余路径不再接受）
| refresh_token | 7 天 | path=/api/auth, SameSite=Strict, 轮换+重用检测 |
| MAX_RUNS_PER_USER | 3 | 不在 .env.example，需手写；0=不限 |
| MAX_TOTAL_RUNS | 20 | 全局兜底 |
| LOGIN_RATE | 5/min（register 3/hour, sessions GET/新建 10/min …） | 按 IP 键控，内存态，重启清零 |
| UPLOAD_DIR | `<repo>/uploads` | 白名单 .pdf/.docx/.bmp/.jpg/.jpeg/.png/.gif/.tif/.tiff；50MB 上限；注意 COURTIER_UPLOAD_DIR 别名未实现 |
| 插件端口 | 9101–9108 | COURTIER_PLUGIN_ENDPOINTS=name=host:port CSV；缺端点置 BLOCKED |
| RUN_GRACE_SECONDS | 600 | 终态日志保留窗，过期 attach 404 |
| LLM_IP / LLM_NAME | qwen3.6-27b | OpenAI 兼容；stream 不可用时自动回退非流式 |
