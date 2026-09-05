# 审查遗留项：设计方案与修复计划（2026-09-05）

来源：2026-09-04/05 夜间审查-修复马拉松的遗留清单（`docs/reviews/2026-09-05-round6.md` 交接清单 + 各轮「记录，未修」表）。
状态：**设计完成，待批准实施**。

## 需求对齐记录（2026-09-05 与用户确认的 4 项决策）

| 决策点 | 选择 | 含义 |
|--------|------|------|
| 运行启动通道 | **fetch 流式** | POST + ReadableStream，前端重实现 SSE 帧解析与重连；服务端 SSE 帧格式不变 |
| API 文案 | **错误码化** | 后端返回结构化 `code + params`，不再承载 UI 文案；前端建文案表渲染 |
| 注销合规 | **保留期清理** | 审计文件隔离后按保留期（30 天）清除；`agent:<插件>` actor 审计行匿名化保留操作记录 |
| 实施顺序 | **架构先行** | 先做 artifacts/auth 重构打地基，再做行为类改动 |

---

# Part A — 设计级改动

实施顺序遵循「架构先行」：**A1（地基）→ A2 → A3 → A4 → A5**。

## A1 架构重构：artifacts schema 去域化 + auth 服务层抽取（先行批次）

### A1.1 ArtifactSchema 注册表实例化

**现状**：`courtier/agent/artifacts/models.py:36-47,161-300` 把 8 个 `docaudit.*` ArtifactSchema 写死在核心的**进程级全局单例** `_artifact_type_schemas_cache` 中，且该单例无作用域隔离、无注销 API（R4 审查发现）。新增领域必须改核心。

**设计**：

1. 新建 `ArtifactSchemaRegistry` 类（`artifacts/schema_registry.py`）：
   - `register(schema: ArtifactSchema)` / `get(type_name) -> ArtifactSchema | None` / `registered_types() -> frozenset[str]`
   - 构造时注册 core.* 通用类型（从现 models.py 拆出的核心部分）
   - 只读视图 `readonly()` 供查询方使用（消除「任何持有者可改全局」的问题）
2. 实例归 **AgentRuntime 所有**（与 ToolRegistry 同生命周期，per-request）；`get_artifact` 工具与投影路径（`store.py` 的 schema 查询点）改为从 runtime 携带的 registry 解析。
3. `domains/docaudit/` 新增 `artifact_schemas.py` 持有 8 个 `docaudit.*` schema 定义；`DomainActivator.activate()` 调 `registry.register_many(docaudit_schemas)`（幂等：已注册同名则跳过）。
4. 迁移：持久化 artifact_id 不变（`$ref:<tool>:<seq>` / `projected:<type>:<hash>`），无数据迁移；未激活域的 session 查询其 schema 得到 `None` → 走现有 untyped cached_output 路径（与今天域未激活行为一致）。

**验收**：核心 `grep -r docaudit courtier/agent/artifacts/` 零命中；现有 artifacts 测试迁移为注入式构造后全绿；新建一个最小测试域注册自定义 schema 并投影成功。

### A1.2 auth/user 服务层抽取

**现状**：`routes/auth.py`（登录/锁定/注册/刷新/登出）、`routes/profile.py`（改密）、`routes/admin_users.py`（用户 CRUD/重置）、`routes/setup.py`（首建 admin）直接操作 ORM 与密码/JWT 逻辑（R1 P3-12）。

**设计**：

1. 新建 `services/user_service.py`：
   - `authenticate(username, password) -> UserTable`（锁定检查、bcrypt 校验、失败计数——现 auth.py:203-225 逻辑整体迁入）
   - `change_password(user_id, current, new)`（校验 + 改哈希 + **吊销 refresh token**，现 profile.py 逻辑迁入）
   - `admin_reset_password(user_id, new)`（同上，现 admin_users.py 逻辑迁入）
   - `register / set_role / set_status`（状态转换校验规则集中于此）
2. 新建 `services/auth_service.py`：`issue_session(user, request, response)`（双 cookie 签发）、`rotate(token_hash, session)`（包装现 middleware 的 rotate_refresh_token + 用户状态校验）、`revoke_family(user_id, session)`。
3. 路由瘦身为：解析请求体 → 调服务 → 映射 `UiError`（见 A5；本批先保留现有中文 detail，错误码化批次统一替换）。
4. 行为不变的护栏：`test_auth_lockout.py`、`test_refresh_token_flow.py`、`test_profile_password.py`、`test_memory_routes.py` 全部原样通过。

**验收**：路由文件中不再出现 `select(UserTable)` / `bcrypt` / `hash_password` 直调；全部现有认证测试无修改通过。

## A2 事件总线分级

**现状**：`core/event_bus.py:110-129` 单一有损队列（`drop_oldest`, maxsize 1000）。token 事件洪峰可能挤掉 `tool.result`/`think.tool_calls` 等关键事件——SSE 输出与持久化**双丢失**（R4-P8）。

**设计**：

1. `EventBus.subscribe(event_types: frozenset[str] | None = None, *, maxsize, overflow)`：订阅带类型过滤；`publish` 按事件类型路由到匹配订阅的队列。`None` = 收一切（兼容现有测试）。
2. `RunRecorder.start_listening` 建两个订阅：
   - **token 队列**：`{"llm.token", "llm.content_token"}`，maxsize 200，`drop_oldest`——token 丢失是纯显示损失（终态结论含全文）；
   - **critical 队列**：其余全部类型，maxsize 2000，`drop_oldest` + 深度金丝雀（>80% 时 warning 日志 + `event_bus_dropped_total{kind="critical"}` 计数）。关键事件本身低频，2000 实际不可达。
3. 两个监听任务共用现有 `_dispatch_lock`（串行化保证不变）；`drain_pending` 检查两个订阅队列；`stop_listening` 取消两个任务。
4. 乱序说明：token 与 critical 队列各自保序、跨队列可能交错——前端流式管道已按「非缓冲事件先 flush 缓冲 token」处理到达序（`useAgentSession.ts:152`），无需改动。
5. 指标：`event_bus_dropped_total` 增加 `kind` 标签（critical/token）。

**验收**：新测试——灌 5000 个 token + 1 个 tool.result，断言 tool.result 必达且 token 部分丢失不报错；现有事件总线/SSE 集成测试全绿。

## A3 SSE fetch 流式（run-start 与 attach 全部迁移）

**现状**：run-start 走 `GET /api/sessions/run?task=...`（EventSource 限制），任务全文进 URL；attach 走 `GET /sessions/{id}/events`。前端 `createEventSource`/`attachSessionEvents` 基于浏览器 EventSource。

**设计**（服务端 SSE 帧格式 **完全不变**——id:/data: 行、resync、水印协议原样，只换传输层）：

1. **后端**：
   - 现 `GET /sessions/run`（`routes/sessions.py:141`）的处理核心（run 创建 + `generate_sse_stream`）抽为共享函数；
   - 新增 `POST /sessions/run`：JSON body `{task, fileId, fileIds, sessionId, editTurn, modelId}`，返回同样的 `StreamingResponse`（text/event-stream）；
   - `GET /sessions/run` 保留一个发布周期（deprecated 标注），确认前端切净后删除；
   - 任务长度上限从「前端 4000 字符兜底」改为服务端校验（错误码 `task.too_long`，见 A5）。
2. **前端**：新建 `webui/src/utils/sseStream.ts`：
   - `openSse(url, {method, body, lastEventId, signal, onEvent, onError})`：`fetch` + `TextDecoder` + 增量帧解析（按 `\n\n` 分帧，处理多行 `data:` 与 `id:` 行）；
   - 重连策略：**run-start 流断开后转 attach 流**（`/sessions/{id}/events` + `Last-Event-ID` 头，fetch 可带头），attach 流断开则原地带 `Last-Event-ID` 重连；指数退避 3s→30s（复用 useRunEvents 模式）；generation 守卫沿用；
   - `attachSessionEvents` 重写为 fetch GET + `Last-Event-ID` 头；全局事件通道（useRunEvents）同批迁移（GET + 头）；
   - 认证：`authFetch` 的 Bearer 头 + cookie 双通道——迁移完成后移除 `?token=` 白名单中的 `/api/sessions/run`、`/api/sessions/{id}/events`（`/api/events` 视全局通道迁移同步移除）。
3. **删除项**：前端 4000 字符 task 限制（JSON body 不受限；服务端保留自己的长度校验）。
4. **回滚**：前端与后端同发布；GET 路由保留期内可一键切回。

**验收**：新增 sseStream 的帧解析/重连单测（模拟半帧、多行 data、断流）；手工冒烟——启动、暂停、停止、断网重连（Last-Event-ID 续传）、editAndResend、attach 恢复运行中会话；现有 test-attach-resume 等脚本迁移后全绿。

## A4 注销合规：保留期清理

**现状**：注销九步级联不清 `.agent_logs` 审计文件（目录结构 `<audit_log_dir>/<session_id>/`，run_manager.py:666 以 session_id 为 run_id）；`agent:<插件名>` actor 的 `memory_changes` 审计行未匿名化且 `title` 留存明文（R3 C-P5）。

**设计**：

1. **审计文件隔离（注销时，best-effort）**：管线在收集到用户 session_ids 后，将 `<audit_log_dir>/<session_id>` 逐个移入 `<audit_log_dir>/_quarantine/<user_id>-<deletion_ts>/`；失败记入 `receipt.failures`（子代理 run 同目录，天然一并隔离）。
2. **定期清除（保留期）**：app lifespan 启动一个 daily sweeper（模式同 RunManager reaper）：删除 `_quarantine/` 中年龄超过 `audit_retention_days`（新设置，默认 30，归 observability 组）的目录。
3. **memory_changes 匿名化补强**：`delete_user_memory` 在删除条目前收集 entry_ids 返回；管线对 `memory_changes` 执行 `UPDATE ... SET actor = 'deleted-user:<id>', title = '' WHERE entry_id IN (ids)`——覆盖 `agent:<插件>` actor 且清除明文标题；现有 `_anonymize_actor(username, agent:<username>)` 保留作双保险。
4. 回执 `deletion_log.counts` 增加 `audit_files_quarantined`。

**验收**：新测试——注销后审计目录已移入隔离区、30 天 sweeper 清除、memory_changes 的 actor/title 已匿名化；现有 test_account_deletion.py 补断言后全绿。

## A5 错误码化（依赖 A1 服务层落地后实施）

**现状**：HTTP/SSE 面向用户的文案硬编码中文 200+ 处；`parseErrorDetail` 前端只认字符串与 Pydantic 数组两种 detail。

**设计**：

1. 新建 `courtier/agent/api/ui_errors.py`：
   ```python
   class UiError(HTTPException):
       def __init__(self, code: str, /, **params): ...
   # detail 序列化为 {"code": "auth.account_locked", "params": {"seconds": 598}}
   ```
   错误码常量集中在一个模块（`UI_ERROR_CODES`），命名 `<域>.<原因>`。
2. **模型可见错误不走此通道**：`ExecutionResult`/工具错误继续用 `render_error` 渲染文本（模型需要自然语言）。
3. **SSE 终态**：`emit_terminal("error", code=..., params=..., trace_id=...)`——payload 增加 `code`/`params` 字段，`detail` 过渡期双写，切换完成后删除。
4. **前端**：`webui/src/constants/errorMessages.ts` 建立 code → 模板表（zh-CN 先行，en-US 结构就位）；`parseErrorDetail` 识别 `{code, params}` 形态，未知 code 回退到域级通用文案 + 原样展示 params。
5. **迁移批次**（每批独立验收）：auth/profile/setup（依赖 A1.2 服务层）→ sessions/run_manager 终态 → resources/memory → admin 其余 → 删除旧字符串 detail。
6. 过渡期兼容：`detail` 为对象期间，前端旧字段读取（`detail` 直接当字符串）由 parseErrorDetail 统一兼容，无需双端同步发版。

**验收**：grep 路由层不再有裸字符串 `HTTPException(...)`（白名单清单归零）；前端文案表覆盖全部已发 code；未知 code 注入测试确认回退文案。

---

# Part B — 二组：协议/并发修复方案

## B1 注销/停止的取消按会话隔离

- `JSONRPCClient` 的 pending 表记录每条请求的 `session_id`（ProxyTool 派发时从 agent 上下文取）；
- `cancel_pending(session_id: str | None = None)`：为 None 取消全部（stop-all 用），否则只发该会话在途请求的 `request.cancel`；
- `routes/control.py`：单会话 stop 传该会话 id；stop-all 传 None（admin-only 已有）。

## B2 流式 tool_calls 回退的幽灵文本

- 后端检测到回退（`core/model.py:412` 非流式重发）后：loop 先发布一条 `think.retry` 事件（payload 带 `reason: "fallback"`，前端现有 handler 即会重置本步缓冲），再把回退响应全文作为单个 content token 发布——真实结论恢复流式语义（一次性到达），前端无需新逻辑。

## B3 get_artifact 编号 ref 消歧

- 创建 typed artifact 时（ToolProxy 投影路径）**同时注册编号别名** `$ref:<tool>:<seq>`（seq 与 cache_store 计数器同源）；
- `_find_artifact_for_ref` 的前缀兜底**收紧为仅 `:latest` 后缀**（编号 miss 走持久层，读不到即明确报 `artifact_typed_ref_not_found`）——R7 曾单做后半（破坏双层设计回滚），补上前半（别名注册）后两部分合起来才是完整修复。

## B4 插件 SDK 背压

- 每连接 `asyncio.Semaphore(N)`（manifest 新增 `runtime.max_concurrent`，默认 8）；满载返回 JSON-RPC 错误 `-32000 busy`（fail-fast 而非排队 OOM）；
- sync handler 的执行不可取消属 Python 线程语义限制——在 `plugin-development.md` 明示：重计算必须 `asyncio.to_thread` + 分片检查取消事件（parse 已是范例）。

## B5 宿主校验插件上报的 session_id

- `JSONRPCClient` 在 call 时记录每条 pending 请求的会话上下文（来自 ProxyTool 的 agent 上下文）；
- 宿主 reverse 请求处理（artifact/cache/memory/template 各分支）比对 `params.session_id` 与发起请求的会话，不一致 `_deny(INVALID_PARAMS)` 并告警计数。

---

# Part C — 三组：小型修复方案（✅ 已全部实施）

> 实施记录：C1（ES 别名）、C2（object_exists）、C3（guards 结构校验）、
> C5（memory list 查询）、C7（CORS 409 / init_index）、C8（杂项）见提交
> 029224a / a8a9c72 / 7c5761a；C4（jwt 接线+Literal）见 18fe35d；C6（插件卫生三件）
> 见 a17f50d 前后各提交。

| # | 修复 | 具体改动 |
|---|------|----------|
| C1 | ES 别名一致性与写索引解析 | `_delete_by_field`/`update_chunk`/`append_annotation` 的 index 参数改打基础别名（ES 路由到实际持有文档的索引）；`resolve_write_index` 优先取 `is_write_index: true` 的条目 |
| C2 | `object_exists` 只吞「不存在」 | 仅捕获 `S3Error` 且 code ∈ {NoSuchKey, NoSuchBucket} 返回 False，其余异常上抛；预览/转存路径的调用方补 try 降级 |
| C3 | guardrail_guards 加载期容错 | `compose_snapshot`/Settings 校验器只做结构校验（descriptor 形状）；import 检查仅在保存路径执行；加载期不可导入的声明交给运行时跳过+告警（现有 registry 语义） |
| C4 | jwt 配置清理 | 删除无消费者的 `jwt_expire_seconds`/`jwt_access_expire_seconds`（或接线到 `ACCESS_EXPIRE`，二选一）；`jwt_algorithm` 改 `Literal["HS256"]` |
| C5 | memory 工具 list 能力 | `tool_action` 的 list 透传 `query/skip/limit` |
| C6 | 插件卫生三件 | annotate 两处临时文件用 `TemporaryDirectory` 兜底删除；parse 缓存键改 minio key（内容 digest）+ 条目字节上限；anydoc 加 `MAX_OCR_PAGES`（超限报错提示拆分）+ 页渲染入 `asyncio.to_thread` |
| C7 | 设置保存的两处假成功 | env 逃逸激活时对 cors 字段保存直接 409（说明 env 覆盖）；rebuild 组含 `es_index_*` 变更时保存后重跑 `init_index` |
| C8 | 杂项 | `SettingsKeyMissing` 捕获转 422；`/metrics` 从 query-token 白名单移除（该路由自带共享密钥校验）；upsert 竞态败者的冗余审计行已在 1a58c97 清掉 |

---

# Part D — 四组：运维方案（✅ 已实施 D1/D3；D2 digest 与 D4 存量迁移待运维窗口）

> 实施记录：D1（minio healthcheck、mem_limit、PLUGIN_TOKEN 必填）、D3（Dockerfile.env
> dev-only 标注、dev-plugins 启动 watchdog）见 1a0c95c。

| # | 项 | 改动 |
|---|-----|------|
| D1 | compose 加固 | `8000:8000` → `127.0.0.1:8000:8000`；mysql/es/langfuse/clickhouse 加 `mem_limit`/`cpus`；minio 加 healthcheck（curl storage API）；langfuse 服务 `depends_on` 补 minio；`COURTIER_PLUGIN_TOKEN` 改 `${COURTIER_PLUGIN_TOKEN:?在 .env 设置}` |
| D2 | 镜像 digest | 对 `python:3.12-slim`、`mysql:8.4`、`minio/minio:RELEASE...`、`uv:0.11.4` 执行 `docker pull` 后取 digest 写入（提供一次性脚本或手动操作说明）；季度复审 |
| D3 | 杂项 | `Dockerfile.env` 文件头标注 dev-only；`dev-plugins.py` 对早退子进程打印退出码并整体退出（避免静默半套运行） |
| D4 | 存量数据 | docs 桶里的历史 `plugin-outputs/` 一次性迁移脚本到 transfer 桶或删除（24h 生命周期对它们已失效）；legacy `documents.doc_id` 加唯一索引（cheap，顺手） |

---

# 实施顺序与验收门（架构先行）

| 批次 | 内容 | 验收门 |
|------|------|--------|
| P0（地基） | A1 artifacts registry 实例化 + auth/user 服务层抽取 | 核心零 docaudit 引用；路由零 ORM/bcrypt 直调；全部现有测试无修改通过 |
| P1 | A2 事件总线分级 | 灌压测试：token 丢失不影响 tool.result 必达 |
| P2 | A3 SSE fetch 流式 + B1/B2/B3（同一传输层改造顺路） | 手工冒烟清单（启动/暂停/停止/断网重连/attach/edit-resend）+ 帧解析单测 |
| P3 | A4 注销合规 + B4/B5 | 注销隔离/sweeper 测试；SDK 背压测试 |
| P4 | A5 错误码化（分四小批）+ C 组 + D 组 | 路由层裸字符串 detail 归零；C/D 组逐项核销 |

每批验收门统一为：`uv run pytest -m "not integration"` 全绿 + `webui npm run lint/test/build` 全过 + 该批新增测试通过 + 按仓库惯例独立 conventional commits。
