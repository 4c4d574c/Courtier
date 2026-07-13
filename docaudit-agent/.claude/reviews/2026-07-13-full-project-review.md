# 项目审查报告 — Courtier 平台

**日期：** 2026-07-13  
**范围：** 整个 Python FastAPI 后端、领域插件、基础设施文件  
**审查人员：** Python 代码质量 + 安全 + 静默失败（3 个并行专业代理）  
**验证结果：** ✅ 821 测试通过，⚠️ 212 个 lint 问题

---

## 总体评估

代码库具有稳固的基础，包含正确的密码哈希、JWT 处理、路径遍历保护和适当的安全头。最近的 25 项修复使代码库处于良好状态。然而，本次审查发现了 **36 项独特发现**：1 个 CRITICAL、15 个 HIGH、14 个 MEDIUM 和 6 个 LOW。

最重要的模式有三个方面：
1. **静默失败** — 后端（ES、磁盘持久化、插件通知）在 DEBUG 级别静默失败，在 INFO 级别的生产环境中不可见
2. **安全配置** — docker-compose.yml 中的默认凭据和端口 8000 上未认证的 `/metrics`
3. **错误处理边界** — 多处静默吞掉异常，在崩溃/重启时导致数据丢失

---

## CRITICAL（1 项发现）

### C-1：Docker Compose 可观测性栈中的硬编码/弱默认凭据

**文件：** `docker-compose.yml`  
**严重性：** CRITICAL  
**类别：** 安全

多个服务附带硬编码的默认凭据，如果在生产环境中保持不变，将提供对内部基础设施的直接访问：

| 行号 | 服务 | 默认凭据 |
|------|---------|-------------------|
| 172 | langfuse-clickhouse | `CLICKHOUSE_PASSWORD=default` |
| 174 | langfuse | `ENCRYPTION_KEY` 回退到 64 个 `0` 字符 |
| 175 | langfuse | `SALT` 回退到 `"salt"` |
| 176 | langfuse | `NEXTAUTH_SECRET` 回退到 `"courtier-nextauth-secret-change-me"` |
| 185 | langfuse | `LANGFUSE_INIT_USER_PASSWORD` 回退到 `"adminadmin"` |
| 92 | elasticsearch | `ES_PASSWORD` 按原样在健康检查测试命令中传递，在 `docker inspect` 中可见 |
| 265 | grafana | `GF_SECURITY_ADMIN_PASSWORD` 回退到 `"admin"` |

**修复：** 添加启动验证脚本，如果使用任何默认值则拒绝启动。在部署指南中记录必须覆盖所有默认值。

---

## HIGH（15 项发现）

### H-1：Context 压缩回退可能产生上下文溢出
**文件：** `packages/core/src/courtier/agent/core/context_manager.py:297-314`  
**类别：** 正确性

当 LLM 摘要失败时，回退保留最后 `min(8, len(messages))` 条消息，没有内容截断。如果这 8 条消息包含大型工具输出，上下文可能超出模型限制，导致后续 LLM 调用失败。

**修复：** 对回退消息应用每条消息的内容截断（例如，将每条消息的内容修剪到 4000 字符上限）。

### H-2：会话持久化静默失败，无任何错误信号
**文件：** `packages/core/src/courtier/agent/api/session_store.py:327-333`  
**类别：** 静默失败

当会话持久化失败时，异常被记录但调用者从未收到任何失败指示。内存状态已在调用 `_persist` 之前更新。崩溃将永久丢失会话。

**修复：** 将错误传播给调用者或切换到写前确认模式。至少设置脏标志。

### H-3：ast.literal_eval 在 LLM 输出上（不安全反序列化风险）
**文件：** `packages/core/src/courtier/agent/core/model.py:63`  
**类别：** 安全

当 JSON 解析失败时，`_parse_tool_arguments()` 回退到 `ast.literal_eval()`。虽然比 `eval()` 更安全，但 `literal_eval` 并非为安全边界而设计。LLM 输出是根本不可信的输入。

**修复：** 实现稳健的 JSON 修复策略（单引号转双引号、尾随逗号移除）。如果无法解析，返回 `{"_parse_error": True}` 并让代理循环重试，而不是尝试不安全的解析。

### H-4：个人资料密码更新使用错误的模式键（静默失败）
**文件：** `packages/core/src/courtier/agent/api/routes/profile.py:91`  
**类别：** 正确性 / 安全

个人资料更新路由设置 `update_data["new_password"]`，但 SQL UPDATE 尝试设置不存在的列 `new_password`。用户认为密码更改成功，但密码保持不变。

**修复：** 将 `"new_password"` 更改为正确的字段名。根据 admin 端点使用 `"password"`。

### H-5：Prometheus 指标端点未认证暴露
**文件：** `packages/core/src/courtier/agent/api/app.py:162-168`  
**类别：** 安全

`/metrics` 端点完全绕过 JWT 认证。任何能够访问端口 8000 的人都可以抓取指标，暴露内部架构细节。

**修复：** 通过环境变量要求共享密钥（`?token=` 或 `Authorization: Bearer`），或在仅内部的单独端口上提供指标。

### H-6：刷新令牌通过 SSE 查询参数在 URL 中暴露
**文件：** `packages/core/src/courtier/agent/api/middleware/auth.py:84-85,179-180`  
**类别：** 安全

认证中间件通过 `?token=` 查询参数接受 JWT 令牌以兼容 SSE/EventSource。URL 中的令牌被反向代理、浏览器历史记录和服务器访问日志记录。

**修复：** 考虑为 SSE 连接使用单独的短生命周期令牌，或建议在生产部署中配置反向代理以从访问日志中剥离 `token=`。

### H-7：primary 后端持久化失败以 DEBUG 级别静默记录
**文件：** `packages/core/src/courtier/agent/core/cache_store.py:156-159,188-192`  
**类别：** 静默失败

当 Elasticsearch 在持久化期间失败时，失败仅以 DEBUG 级别记录。在生产环境中（INFO 级别），此失败完全不可见。磁盘上重复数据累积且无警报。

**修复：** 以 WARNING 或 ERROR 级别记录，并发出结构化事件或指标计数器增量。

### H-8：_count_business_artifacts 在任何失败时返回 0
**文件：** `packages/core/src/courtier/agent/core/loop_guards.py:170-177`  
**类别：** 静默失败

计算业务工件的任何失败都静默返回 0。这影响检查进度，代理循环可能因误导性的 `"no_business_artifacts"` 原因错误终止。

**修复：** 返回哨兵值或传播错误，以便调用者可以决定是终止还是重试。

### H-9：插件 notify() 静默吞掉写入失败
**文件：** `packages/core/src/courtier/plugin/client.py:287-299`  
**类别：** 静默失败

发送到插件的通知失败（例如，`request.cancel`、`plugin.shutdown`）仅以 DEBUG 记录。在 `manager.py:781-786` 的 `cancel_pending()` 中也复合了此静默。

**修复：** 以 WARNING 级别记录，并向调用者暴露失败（例如，返回布尔值或引发异常）。

### H-10：工件存储磁盘持久化即发即弃，无错误跟踪
**文件：** `packages/core/src/courtier/agent/artifacts/store.py:258-261`  
**类别：** 静默失败

`create_task` 没有 `add_done_callback` 来捕获异常。如果 `_backend.persist()` 协程引发异常，异常会被静默消耗。工件数据静默丢失。

**修复：** 添加记录异常的完成回调，或使用适当的错误处理。

### H-11：控制路由中 asyncio.TimeoutError 和 CancelledError 静默通过
**文件：** `packages/core/src/courtier/agent/api/routes/control.py:83-86,103-104`  
**类别：** 静默失败

当等待已取消的代理任务超时（5 秒后），代码静默继续。任务可能仍在后台运行，持有资源。僵尸代理任务无限期消耗资源。

**修复：** 超时后，记录警告并强制清理。从 `active_tasks` 中移除任务。

### H-12：模块级全局变量在导入时计算 — 脆弱的路径解析
**文件：** `packages/core/src/courtier/config.py:20-21`  
**类别：** 正确性

`.parent.parent.parent.parent.parent` 链假设文件始终位于仓库根目录下恰好 5 级深度。如果 `config.py` 移动，路径将静默指向错误目录。

**修复：** 使用哨兵文件（例如，向上搜索 `pyproject.toml`）在首次访问时动态定位项目根目录。

### H-13：SSE 适配器 handle_id 回退将空字符串视为缺失
**文件：** `packages/core/src/courtier/agent/api/sse_adapter.py:316-317`  
**类别：** 正确性

`event.handle_id or event.subagent_name` 将 `handle_id=""` 视为假值。使用 `is not None` 检查来区分空字符串和 None。

### H-14：缺少速率限制的端点
**文件：** `packages/core/src/courtier/agent/api/routes/auth.py`、`sessions.py`、`admin_users.py`、`files.py`、`control.py`  
**类别：** 安全

11 个端点缺少 `@limiter.limit` 装饰器，包括注销、会话详情、文件上传和管理用户操作。

**修复：** 添加速率限制：注销 10/分钟，会话端点 10-30/分钟，文件上传 10/分钟。

### H-15：内容检查器分类器域名在提示中硬编码
**文件：** `packages/domains/docaudit/plugins/validator/content_checker.py:68-97`  
**类别：** 正确性

`_CLASSIFICATION_SYSTEM_PROMPT` 将 20 个域名硬编码为静态字符串。如果实际数据库表具有不同的域，LLM 将被要求分类到可能不匹配任何 `RuleDomain` 条目的名称中。

**修复：** 在初始化时从数据库加载域名。

---

## MEDIUM（14 项发现）

### M-1：用户通过登录时间枚举
**文件：** `packages/core/src/courtier/agent/api/routes/auth.py:136-143`  
**类别：** 安全

不存在的用户返回时没有 bcrypt 开销，而存在的用户在错误密码时经过 bcrypt。时间差异允许用户名枚举。

**修复：** 为不存在的用户名添加虚拟 bcrypt 验证以标准化时间。

### M-2：rate_limiter 仅使用客户端 IP，非基于账户
**文件：** `packages/core/src/courtier/agent/api/rate_limiter.py`  
**类别：** 安全

速率限制器仅基于 `get_remote_address`。攻击者可以使用僵尸网络将暴力破解尝试分布到多个 IP。

**修复：** 添加基于账户的速率限制，在 N 次连续失败后暂时锁定账户。

### M-3：会话数据以未加密 JSON 形式存储在磁盘上
**文件：** `packages/core/src/courtier/agent/api/session_store.py`  
**类别：** 安全

会话记录（包括任务描述、文件元数据、工具结果）以纯 JSON 文件形式持久化。无静态加密。

**修复：** 在生产环境中使用加密文件系统，或在写入磁盘前使用 Fernet 加密会话数据。

### M-4：无连续登录失败后的账户锁定
**文件：** `packages/core/src/courtier/agent/api/routes/auth.py:112-168`  
**类别：** 安全

登录有速率限制但无按账户锁定机制。

**修复：** 向 `UserTable` 添加 `failed_login_attempts` 和 `locked_until` 列。

### M-5：插件 stdin 循环中 JSON 解码错误静默丢弃
**文件：** `packages/core/src/courtier/plugin/client.py:114-125`  
**类别：** 静默失败

UTF-8 解码失败和 JSON 解析失败都被静默跳过。如果插件产生垃圾输出，每一行都被丢弃且无日志条目。主机无法区分"插件正在思考"和"插件崩溃"。

**修复：** 在 JSON 解码失败时记录 WARNING，包含格式错误行的前 200 个字符。

### M-6：_send_line 在 PluginRuntime 中捕获所有异常
**文件：** `packages/core/src/courtier/plugin/sdk/runtime.py:625-628`  
**类别：** 静默失败

如果 stdout 失败，来自插件的每个响应都静默消失。插件继续处理，而主机认为它只是慢。

**修复：** 在 stdout 失败时设置 `self._running = False`，导致事件循环干净退出。

### M-7：generate_with_streaming_fallback 回调按令牌失败 — 日志风暴
**文件：** `packages/core/src/courtier/agent/core/loop_streaming.py:40-53`  
**类别：** 静默失败 / 性能

如果令牌回调失败一次，所有后续令牌可能也会失败，为每个字符生成 WARNING。无断路器。

**修复：** 跟踪连续失败并在 N 次连续错误后停止循环。

### M-8：bootstrap_admin_user 不加区分地捕获所有异常
**文件：** `packages/core/src/courtier/agent/api/db.py:75-80`  
**类别：** 静默失败

捕获从连接被拒绝（暂时性，应重试）到表未找到（模式未迁移，致命）的所有内容。所有内容都相同处理。

**修复：** 至少区分连接错误和其他失败，或在警告中记录异常类型。

### M-9：DRUDGE.md 加载失败静默通过
**文件：** `packages/core/src/courtier/plugin/manager.py:870-874`  
**类别：** 静默失败

如果存在 DRUDGE.md 但无法读取，异常被静默丢弃。插件在没有项目级规则的情况下运行，且没有它已缺失的指示。

**修复：** 至少记录 WARNING，以便操作员知道规则文件无法读取。

### M-10：过多简化 CJK 字符范围检测
**文件：** `packages/core/src/courtier/agent/core/context_manager.py:388-398`  
**类别：** 正确性

范围 `"一"` (U+4E00) 到 `"鿿"` (U+9FFF) 仅覆盖基本 CJK 统一汉字块。缺少扩展 A、兼容性汉字和扩展 B-F。

**修复：** 使用 `unicodedata.name(c, "").startswith("CJK")` 或 `regex` 库的 `\p{Han}`。

### M-11：ParseTool 缓存在内存中无界
**文件：** `packages/domains/docaudit/plugins/common/parse/tools.py:84`  
**类别：** 性能

`ParseTool` 按文件路径缓存解析结果，无驱逐策略。重复解析不同文档将无限增长缓存。

**修复：** 使用合理大小上限添加 LRU 驱逐策略。

### M-12：SSE 适配器无队列溢出保护
**文件：** `packages/core/src/courtier/agent/api/sse_adapter.py:619`  
**类别：** 正确性

`_emit_sse` 使用 `await self._queue.put()`，如果队列已满将阻塞。如果 SSE 消费者停止读取，代理循环将挂起。

**修复：** 在 `_emit_sse` 中使用 `put_nowait`，添加超时或队列满时防护。

### M-13：similarity("", "") 返回 1.0 掩盖空推理检测
**文件：** `packages/core/src/courtier/agent/core/loop_utils.py:66-72`  
**类别：** 正确性

两个空推理字符串被视为"相同"(1.0)，这意味着如果模型在连续步骤中不产生推理内容，推理循环检测器可能将其标记为循环并强制终止。

**修复：** 当两个字符串都为空时返回 `0.0`，因为无推理内容不携带循环检测信号。

### M-14：CRUDRepository 硬编码 LIMIT 默认值可能让调用者意外
**文件：** `packages/core/src/courtier/db/db_manager.py:136-137`  
**类别：** 可用性

`list()` 将 `limit=0` 限制为 1000。传递 `limit=0` 可能是故意的（"不给我行，只给计数"）。

**修复：** 考虑将 `limit=0` 视为"返回空列表"而不是限制。

---

## LOW（6 项发现）

### L-1：OTLP Collector 在所有接口上暴露 Prometheus 指标
**文件：** `otel-collector-config.yaml:26` — Prometheus 导出器端点绑定到 `0.0.0.0:8889` 而不是 `127.0.0.1`。

### L-2：Dockerfile HEALTHCHECK 使用未认证的 /metrics 端点
**文件：** `Dockerfile:49` — 如果 H-5 通过添加认证修复，HEALTHCHECK 将中断。

### L-3：HookChain 注销在迭代时改变列表
**文件：** `packages/core/src/courtier/agent/hooks/chain.py:161-170` — 虽然由于立即返回目前在技术上是正确的，但此模式很脆弱。

### L-4：_log_task_exception 具有永远不会执行的死代码
**文件：** `packages/core/src/courtier/plugin/sdk/runtime.py:179-188` — `except asyncio.CancelledError` 分支是死代码；`task.exception()` 不会引发 `CancelledError`。

### L-5：多个大文件超过 800 行指南
- `plugin/manager.py` — 884 行
- `docparse/parsers/spacing.py` — 829 行
- `artifacts/models.py` — 810 行

### L-6：用户名字段允许特殊字符
**文件：** `packages/core/src/courtier/agent/api/routes/auth.py:39` — 添加正则验证器：`pattern=r'^[a-zA-Z0-9_\-\.@]+$'`。

---

## 验证结果

| 检查 | 结果 |
|------|--------|
| 测试（821 个） | ✅ 通过 |
| Ruff lint | ⚠️ 212 个错误（101 E501 行过长，92 I001 导入，14 F401 未使用导入，5 其他） |
| 安全扫描 | ⚠️ 见 CRITICAL + HIGH 发现 |

---

## 正面发现（做得好的方面）

1. ✅ 使用 bcrypt 进行密码哈希（轮次=12，可配置）
2. ✅ JWT 算法固定 — 仅限 `{"HS256"}`，不允许 `"none"`
3. ✅ 刷新令牌轮换，带重放检测 — 完整令牌族撤销
4. ✅ httpOnly + SameSite=Strict cookie 用于刷新令牌
5. ✅ 安全头中间件（X-Content-Type-Options、X-Frame-Options、CSP、HSTS）
6. ✅ 文件处理中的路径遍历保护
7. ✅ 文件上传验证（MIME 白名单、扩展名、50MB 限制、安全文件名）
8. ✅ 可配置的 CORS，适当处理凭据
9. ✅ 关键认证端点的速率限制
10. ✅ 生产密钥强制 — 在 staging/production 中验证 JWT_SECRET 和 ADMIN_PASSWORD
11. ✅ 前端使用 DOMPurify v3.4.11 净化 LLM 生成的 markdown

---

## 审查文件清单

共审查了 60 多个文件，涵盖：
- `packages/core/src/courtier/agent/` — 核心引擎（循环、工具、钩子、遥测、工件、权限、上下文）
- `packages/core/src/courtier/agent/api/` — API 层（路由、服务、中间件、SSE 适配器、会话/文件存储）
- `packages/core/src/courtier/plugin/` — 插件系统（SDK、注册表、客户端、管理器）
- `packages/core/src/courtier/db/` — 数据库层
- `packages/domains/docaudit/plugins/` — 领域插件
- `docker-compose.yml`、`Dockerfile`、`otel-collector-config.yaml`、`.env.example`

---

## 下一步建议

1. **立即：** 修复 C-1（Docker Compose 默认凭据）和 H-4（个人资料密码更新错误）
2. **本周：** 解决 H-7、H-8、H-9、H-10 — 静默失败在生产环境中不可见
3. **本轮：** 修复 H-3（ast.literal_eval 回退）和 H-11（僵尸代理任务）
4. **冲刺：** 为缺少的端点添加速率限制（H-14），并修复 session_store 持久化（H-2）
5. **待办：** 评估 MEDIUM 项 — 大多数是质量/安全加固
6. **Lint：** 运行 `uv run ruff check --fix packages/` 自动修复 109 个问题，手动处理剩余的 ~100 个 E501 行过长
