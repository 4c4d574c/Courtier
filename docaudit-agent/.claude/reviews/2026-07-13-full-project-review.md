# Courtier / DocAudit 全项目代码审查报告

**审查日期**: 2026-07-13  
**范围**: `packages/core/src/courtier/`、`packages/domains/docaudit/`、`tests/`、`alembic/`、`scripts/`  
**方法**:
- 自动化基线检查：`pytest`（非集成测试）、`ruff`、`mypy`、`bandit`
- 专项审查 Agent：安全、核心引擎逻辑、异步/资源/错误传播、领域插件逻辑

---

## 1. 执行摘要

测试基线尚可（`804 passed, 6 skipped`），但静态检查与人工定向审查发现了**大量运行逻辑缺陷与安全漏洞**。核心 Agent 循环、SSE 流控、插件子进程生命周期、鉴权回退、文件/会话隔离等关键路径均存在可导致数据错误、权限绕过或服务资源耗尽的隐患。

| 级别 | 数量 | 说明 |
|------|------|------|
| CRITICAL | 7 | 会导致功能失效、数据覆盖、权限绕过或任务泄漏 |
| HIGH | 40+ | 安全漏洞、竞态条件、资源泄漏、重要逻辑错误 |
| MEDIUM | 20+ | 代码质量、异常吞没、性能/可维护性问题 |

**建议优先处理顺序**：
1. SSE 流控任务泄漏 + Agent 循环 `tool_calls` 被清空（CRITICAL）
2. 鉴权环境变量回退后门 + 会话/文件隔离缺陷（CRITICAL/HIGH）
3. Artifact 合约材质化器缺失 + 内存存储键冲突（CRITICAL）
4. 插件生命周期与并发竞态（HIGH）
5. 公文解析领域逻辑错误（HIGH）

---

## 2. 自动化检查基线

| 检查 | 结果 | 备注 |
|------|------|------|
| `pytest -m "not integration"` | ✅ 通过 | 804 passed, 6 skipped |
| `ruff check --select E,W,F,B,ASYNC,C4,SIM` | ❌ 大量 | 主要为 E501（行长）、I001（import 排序）、B904、B008、F841 等，需批量格式化 |
| `mypy` | ❌ 大量 | `no-any-return`、LockType\|None、default_factory 类型、db_manager 泛型等，部分可能隐藏运行时问题 |
| `bandit` | ⚠️ 1 High / 3 Low | `packages/core/src/courtier/prompts/engine.py:209` 使用 `autoescape=False` |

> 注：本次审查聚焦**运行逻辑 bug 与安全漏洞**，未把风格/格式问题全部列出。格式化问题建议通过 `ruff check --fix` + `black` 统一处理。

---

## 3. CRITICAL 级别问题

### C1. SSE 流断开不取消 Agent 任务，导致任务泄漏与队列无限增长
- **文件**: `packages/core/src/courtier/agent/api/services/stream_service.py:399`
- **问题**: `generate_sse_stream` 的 `finally` 只把任务从 `active_tasks` 中 `pop`，却**没有取消 `runner` 协程**。HTTP/SSE 消费者一旦断开，Agent 循环仍会继续运行，向无消费者队列写入事件，造成任务泄漏和内存无限增长。
- **修复**: 在 `finally` 中调用 `task_ref.cancel()`，并可选 drain 队列；等待取消完成后再清理 `active_tasks`。

### C2. 登录存在环境变量管理员回退后门，可绕过数据库账号状态与密码哈希
- **文件**: `packages/core/src/courtier/agent/api/routes/auth.py:124`
- **问题**: 当数据库缺失、查询报错或不存在管理员用户时，登录逻辑会回退到 `settings.admin_user / admin_password` 进行认证。这绕过了数据库中的账号状态、密码哈希和盐，构成后门。
- **修复**: 在生产/预发布环境彻底移除该回退；如保留仅用于本地 bootstrap，需显式 `deployment_env == "development"` 且默认关闭。

### C3. `add_observation` 提前清空 `tool_calls`，导致探索循环守护与终端工具检查失效
- **文件**: `packages/core/src/courtier/agent/core/loop.py:301`
- **问题**: `add_observation` 把 `current_state.tool_calls` 重置为空 tuple，随后循环调用 `update_tool_call_history`、`update_exploratory_tracking` 以及 `terminal_tool_called` 检查时看到的都是空列表，**守护逻辑永远触发不了**。
- **修复**: 在调用 `add_observation` 前先保存 `executed_tool_calls = current_state.tool_calls`，并把这组已执行调用传给历史更新、探索追踪和终端检查。

### C4. Artifact 合约自动绑定缺少 `docaudit.paragraph_list / list_dict` 材质化器
- **文件**: `packages/core/src/courtier/agent/artifacts/executor.py:69`
- **问题**: `MaterializerRegistry` 只注册了 `list_string` 和 `dict` 的 paragraph_list 材质化器，但 `models.py` 中该类型默认值为 `list_dict`。合约自动绑定到这个类型时会抛出 `KeyError`。
- **修复**: 注册 `("docaudit.paragraph_list", "list_dict")` 材质化器，或把 `_MATERIALIZE_AS_DEFAULTS` 改为已支持的值。

### C5. 内存存储键有损清理，导致不同键互相覆盖、隔离失效
- **文件**: `packages/core/src/courtier/agent/memory/store.py:42`
- **问题**: `_sanitize` 将 `my.key`、`my key`、`_my_key_` 等不同键映射为同一个文件名，造成**数据静默覆盖**和命名空间隔离破坏。
- **修复**: 使用可逆编码（如 percent-encoding 或 base32）保证每个唯一 `(namespace, key)` 对应唯一文件路径。

### C6. 文档标注 `_split_run_at` 在 `<w:t>` 文本为 `None` 时崩溃
- **文件**: `packages/domains/docaudit/plugins/docannot/_patch.py:62`
- **问题**: 对 `split_t.text[:split_local_offset]` 切片前未判空，遇到 text 为 `None` 的元素会抛出 `TypeError`。
- **修复**: 在切片前检查 `if split_t.text is None: continue`，或先初始化为空字符串。

### C7. Jinja2 Prompt 引擎关闭自动转义，存在 XSS 风险
- **文件**: `packages/core/src/courtier/prompts/engine.py:209`
- **问题**: `Environment(..., autoescape=False)`。用户上传内容或外部数据进入 prompt 后，若最终渲染到 Web UI，可能产生 XSS。
- **修复**: 启用 `autoescape=True` 或 `select_autoescape()`，并在渲染后按需去除不必要转义。

---

## 4. HIGH 级别问题（按领域分组）

### 4.1 安全与鉴权

| 文件 | 行 | 问题 | 修复建议 |
|------|----|------|----------|
| `packages/core/src/courtier/plugin/manager.py` | 305 | 插件 manifest 的 `${ENV:VAR}` 可解析任意主机环境变量并注入子进程，插件能读取本被白名单隔离的机密 | 限制 `${ENV:}` 仅允许非敏感白名单变量，或禁止该特性 |
| `packages/core/src/courtier/agent/api/routes/control.py` | 30 | pause/resume/stop 端点不校验调用者是否拥有目标会话 | 增加 session 所有权或 admin-role 校验 |
| `packages/core/src/courtier/agent/api/routes/sessions.py` | 24 | 通过 `user == settings.admin_user` 判断管理员，而非 JWT role claim；任何同名用户可越权 | 使用 JWT payload 中的 `role` claim |
| `packages/core/src/courtier/plugin/manager.py` | 512 | 插件对 artifact_store 的请求使用插件传入的 `session_id`，未验证插件是否代表该会话 | 将插件工具调用与当前 session 绑定，拒绝非本会话 ID |
| `packages/domains/docaudit/plugins/common/parse/tools.py` | 109 | `parse_document` 只检查路径在 upload root 下，不校验是否属于当前用户/会话 | 校验 file_id/path 属于当前会话的已注册文件 |
| `packages/core/src/courtier/agent/api/file_store.py` | 44 | file_id 仅使用 `secrets.token_hex(4)`（8 字符），可猜测，导致跨用户文件访问 | 使用至少 `secrets.token_hex(16)` |
| `packages/domains/docaudit/plugins/audit/plagiarism/core.py` | 61 | 查重算法 `O(N²·M²)` 且无文档/库大小上限，可被 CPU 耗尽攻击 | 限制文档长度与库大小，大库使用 MinHash 预筛选 |
| `packages/core/src/courtier/agent/api/middleware/auth.py` | 77 | SSE 通过 `?token=` 传 JWT，token 会进入 access log、代理日志、浏览器历史 | 强制 Authorization header，SSE 使用短期专用 token |
| `packages/core/src/courtier/agent/api/middleware/auth.py` | 50 | JWT 算法从 settings 读取且无白名单，可被 algorithm confusion 或 `none` 攻击 | 硬编码允许列表 `['HS256']`，明确拒绝 `none` |
| `packages/core/src/courtier/agent/api/routes/auth.py` | 140 | 数据库查询被 broad `except Exception` 包裹，任何 DB 错误都静默回退到环境变量认证 | 仅在显式未配置数据库时回退，其他异常应记录并抛错 |
| `packages/domains/docaudit/plugins/common/search/tools.py` | 247 | `skip/limit` 直接透传给 ES，无上界 | 校验并限制 `limit` 最大值 |
| `packages/domains/docaudit/plugins/common/annotate/tools.py` | 69 | `annotate_document` 接受 upload 目录下任意路径，不验证归属 | 校验源文件属于当前用户/会话，输出写入用户隔离目录 |
| `packages/core/src/courtier/agent/api/middleware/auth.py` | 121 | refresh token 轮换撤销旧 token，但无 token family 与重用检测 | 实现 token family，检测到重用即撤销整个家族 |
| `packages/core/src/courtier/agent/api/session_store.py` | 310 | session 以 JSON 文件落盘，本地有文件系统权限者可直接篡改 | 迁移到数据库并加访问控制，或对 JSON 做签名/加密 |

### 4.2 异步、资源与错误传播

| 文件 | 行 | 问题 | 修复建议 |
|------|----|------|----------|
| `packages/core/src/courtier/agent/api/services/stream_service.py` | 313 | `on_tool_progress` 用 `asyncio.create_task(...)`  fire-and-forget，异常丢失且任务无限制堆积 | 直接 await 或跟踪 task 并通过 `add_done_callback` 处理异常 |
| `packages/core/src/courtier/plugin/client.py` | 93 | `_read_loop` 捕获 broad `Exception` 后直接 break，未 fail pending futures 也未触发 `on_disconnect` | 所有异常路径都调用 `_fail_all_pending` 和 `on_disconnect` |
| `packages/core/src/courtier/plugin/manager.py` | 797 | `_kill_process` 无超时等待子进程，关机或崩溃恢复可能无限挂起 | 使用 `asyncio.wait_for` 加超时，未退出则 `SIGKILL` |
| `packages/core/src/courtier/plugin/sdk/runtime.py` | 553 | 异步 handler 被提交到线程池并新建事件循环，破坏 async 上下文、取消传播与 loop-affinity | 直接在主事件循环 await coroutine handler，仅同步工作进线程池 |
| `packages/core/src/courtier/db/db_manager.py` | 146 | `SessionStore.update/add_step` 持 `asyncio.Lock` 跨越 `await self._persist(...)`，串行化所有 session 操作并阻塞并发请求 | 在锁内拷贝必要状态，锁外执行文件写 |
| `packages/core/src/courtier/agent/api/file_store.py` | 53 | `register` 在释放锁后才 `await _save_index`，多个并发写可能损坏 `file_index.json` | 用第二把锁序列化磁盘写，或 temp-file + atomic rename |
| `packages/core/src/courtier/agent/core/structured_log_handler.py` | 55 | 日志文件在 `__init__` 打开，依赖调用方 close；配置/重置遗漏时 FD 泄漏 | 注册 `atexit` 或实现 `__del__` 调用 `self.close()` |
| `packages/core/src/courtier/plugin/client.py` | 201 | `_send_line` 捕获所有写异常并仅 debug 日志，host 对插件的响应可能静默丢失 | 对 host request response 的写错误应 propagate 或 error 级别日志 |
| `packages/core/src/courtier/plugin/sdk/runtime.py` | 444 | `_run_stdin_loop` 通过 `loop.connect_read_pipe` 打开 transport 却不保存/关闭 | 保存 transport 并在 runtime shutdown 时关闭 |
| `packages/core/src/courtier/agent/api/routes/control.py` | 51 | `stop_session` cancel task 但不 await，清理可能不完整且异常未被 retrieve | cancel 后用 timeout await 并处理 `CancelledError` |
| `packages/core/src/courtier/agent/api/session_store.py` | 103 | `list_all` 加载全部历史 session 文件到内存，无分页 | 增加 skip/limit，仅加载请求页 |
| `packages/core/src/courtier/agent/api/services/stream_service.py` | 273 | Agent 与生成器之间的 `asyncio.Queue` 无 `maxsize`，可无限增长 | 使用有界队列加反压或超限丢弃旧事件 |
| `packages/core/src/courtier/plugin/manager.py` | 633 | `_on_crash` 状态守卫非原子，并发崩溃信号可能重复 restart/kill | 用 `asyncio.Lock` 保护崩溃处理序列 |

### 4.3 核心引擎逻辑

| 文件 | 行 | 问题 | 修复建议 |
|------|----|------|----------|
| `packages/core/src/courtier/agent/tools/registry.py` | 294 | `_check_runtime_policy` 修改共享计数器无锁，并发执行会破坏 `max_calls`/`max_consecutive` 限制 | 在 `execute` 中用 `asyncio.Lock` 保护计数更新与策略检查 |
| `packages/core/src/courtier/agent/agents/base.py` | 319 | `Agent.run` 从已有 state 继续时把 `current_step` 重置为 0，可绕过 `max_steps` 预算 | 保留原 state 的 `current_step` 等字段 |
| `packages/core/src/courtier/agent/agents/orch.py` | 239 | `_attach_skill_callbacks` 直接修改共享 registry 中的 `SkillTool` 实例，并发/复用 Orchestrator 会互相覆盖回调 | 每次 run 创建独立 SkillTool 实例，或在调用时传入上下文 |
| `packages/core/src/courtier/agent/artifacts/projectors.py` | 56 | `Projector.project` 把 schema 校验错误加入 diagnostics 但不失败，仍继续物化不符合 schema 的数据 | schema_errors 非空时抛出异常或返回失败标记 |
| `packages/core/src/courtier/agent/memory/store.py` | 70 | `FileMemoryStore` 读写/删/清空通过 `asyncio.to_thread` 无锁，并发操作可能损坏 JSON 或目录 | 用 `asyncio.Lock` 保护文件/目录变更，并 temp-file + atomic rename |
| `packages/core/src/courtier/agent/hooks/chain.py` | 258 | `HookContext` 的 `metadata` dict 按引用传入，handler 可永久污染共享状态 | 构造 `HookContext` 时 copy metadata |
| `packages/core/src/courtier/agent/tools/registry.py` | 198 | `on_progress` 调用异步 `on_tool_progress` 但不 await，progress 协程被丢弃，流式进度丢失 | 检测可 await 结果并 `create_task` 跟踪，或限制为同步回调 |
| `packages/core/src/courtier/agent/core/loop.py` | 134 | `agent_loop` 用 `getattr(state, 'agent_name', 'unknown')` 覆盖参数，但 `AgentState` 无此字段，trace/metrics 始终为 `unknown` | 直接使用传入的 `agent_name`，仅空字符串时回退 |
| `packages/core/src/courtier/agent/agents/base.py` | 279 | 增量同步 registry 与 `_ensure_builtin_artifact_tools` 是非原子 check-then-act，并发 run 可能重复注册或遗漏 | 用 `asyncio.Lock` 保护，或使 `ToolRegistry.register` 对同名工具幂等 |
| `packages/core/src/courtier/agent/core/loop.py` | 353 | `check_and_inject_hints`/`check_explore_loop` 在 `update_exploratory_tracking` 之前运行，守护使用上一轮探索计数 | 先更新 null 追踪、工具调用历史、探索追踪，再执行守护检查 |
| `packages/core/src/courtier/agent/artifacts/binder.py` | 53 | `bind_tool_inputs` 在所需字段已在 `explicit_kwargs` 中时返回 `arguments={}`，与实际输入不符 | 无自动绑定时返回 `explicit_kwargs` 作为 `arguments` |
| `packages/core/src/courtier/agent/tools/registry.py` | 367 | `_register_output_artifact_simple` 吞掉文件读错误，把 `__persisted_output__` 标记 dict 当作真实内容注册 | 读失败时跳过注册或抛出清晰错误 |

### 4.4 公文审计领域插件

| 文件 | 行 | 问题 | 修复建议 |
|------|----|------|----------|
| `packages/domains/docaudit/plugins/docparse/parsers/pdf_parser.py` | 188 | 加粗/斜体检测只检查第一个 span（`if not font_weight` short-circuit） | 遍历所有 span，任一匹配即设为 True |
| `packages/domains/docaudit/plugins/validator/format_checker.py` | 414 | 字号比较使用精确浮点相等 `!=`，易产生误判 | 使用容差比较，如 `abs(a-b) > 0.5` |
| `packages/domains/docaudit/plugins/docparse/parsers/spacing.py` | 489 | `compute_first_indent` 对单 box 返回 key `0` 而非原始索引，下游查找错位 | 使用 `indexed_boxes[0][0]` 作为 key |
| `packages/domains/docaudit/plugins/docparse/parsers/spacing.py` | 647 | `compute_left_right_indent` 在缺少 margin 时返回顺序索引 `0..n-1`，与 `rec_boxes` 原始索引不匹配 | 返回 `{idx: ... for idx, _ in indexed_boxes}` |
| `packages/domains/docaudit/plugins/docparse/parsers/rules.py` | 344 | 运算符优先级导致条件 `"，" in text or "。" in text and len(text) > 40` 实际为 `"，" in text or ("，" in text and len(text) > 40)` | 加括号明确意图： `("。" in text or "，" in text) and len(text) > 40` |
| `packages/domains/docaudit/plugins/docparse/parsers/scanned/spacing.py` | 256 | `_is_cross_page_continuation` 对仅含空白字符的末行调用 `last_text.rstrip()[-1]` 会 `IndexError` | 先 `rstrip` 再判空再索引 |
| `packages/domains/docaudit/plugins/docparse/parsers/structure_recognizer.py` | 399 | `_build_attachment_note` 直接修改 Pydantic `Font` 对象，违反不可变性约定 | 重建不可变的 Font/Paragraph 对象 |

### 4.5 其他 HIGH/MEDIUM 要点

- `packages/domains/docaudit/plugins/doccorrector/corrector.py:145`：错误导入 `pycorrector.pyrorrect`， supplemental 检查被静默跳过。
- `packages/domains/docaudit/plugins/validator/content_checker.py:336`：`temperature=0.0` 被当作未设置并覆盖为 settings 值，无法显式指定 0.0。
- `packages/domains/docaudit/plugins/docparse/parsers/spacing.py:360`：空 `rec_boxes` 返回 `{0: {...}}`，无对应行。
- `packages/domains/docaudit/plugins/docparse/parsers/pdf_parser.py:186`：字号只取第一个 span，首个 span 为 0 时后续有效字号被忽略。
- `packages/domains/docaudit/plugins/common/template/tools.py:30`：JSON schema 要求 `template_id`，但 `execute()` 用 `kwargs.get` 视为可选，schema 与实现不一致。
- `packages/domains/docaudit/plugins/docparse/parsers/rules.py:482`：同一正则 `DISTRIBUTION_DATE_PATTERN.search(text)` 连续调用两次，应复用 match 对象。
- `mypy` 多处 `no-any-return` / `LockType | None` / `default_factory` 类型错误提示类型边界不够严谨，建议逐步补齐类型注解并处理 `None` 分支。

---

## 5. 验证建议

1. 为 CRITICAL/HIGH 问题逐个编写回归测试。
2. 在 CI 中加入 `ruff check`、`mypy`、`bandit` 并设置质量门禁。
3. 对 SSE 断开、插件崩溃、并发会话等场景增加压力/异常测试。
4. 对鉴权路径增加 fuzz 与越权测试（特别是 `auth.py` 回退逻辑与 `control.py` 会话归属）。

---

## 6. 结论

项目架构已较完整，测试覆盖率良好，但**关键路径的并发安全、鉴权边界、资源生命周期、领域算法正确性**仍有显著风险。建议先集中修复 CRITICAL 与 HIGH 安全问题，再处理核心引擎竞态与领域逻辑错误。修复后应重新跑完整测试集并补充针对上述缺陷的回归用例。
