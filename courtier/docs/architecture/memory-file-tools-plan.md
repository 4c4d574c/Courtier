# 通用文件工具与文件制记忆 — 实施计划

## 背景与目标

记忆能力从专用 API 改为**文件制**：模型通过三个通用文件工具 `read` / `edit` / `write` 直接操作记忆目录中的普通文件（索引 + 主题文件），策略边界上收权限门。对齐 ZCode 自动记忆的成熟模式；同时通用工具天然服务其它用途（便签、跨轮中间产物）。

替换对象：第一期记忆接线的 5 个 `memory_*` 工具 + FileMemoryStore 键值/命名空间机制。

## 对齐结论（已与用户确认，2026-08-29）

1. 工具就三个通用件：`read` / `edit` / `write`，无 memory_ 前缀、无工具内路径策略；**任何文件都可操作，边界由权限门（hook 层）限制**。
2. 拒绝语义：**单调用拒绝**——路径越界的调用返回带指引的错误 ToolResult，loop 继续，模型自行纠正；工具名级拒绝（工具被整体禁用）维持既有整轮 blocked 语义（双级）。
3. 工具内**不**留粗保险边界（策略唯一权威在权限门）。
4. v1 白名单 = 记忆根 + 本会话工作区两个根。
5. `read` 支持行区间（offset/limit，行号输出）；path 为目录时返回清单。
6. 记忆按领域分目录（`common/` + `<domain>/`），注入跟随会话活跃领域；注入双帽：单索引 400 字符 / hint 总量 1500 字符。
7. 无 delete：删除记忆 = `edit` 索引移除条目，孤儿文件无害留存。
8. 子代理共享会话工作区（替代原隔离命名空间——原方案是"只写孤岛"，父不可读）。

## 设计

### 三个工具（`tools/builtin/file_tools.py`，无状态、注册一次全局复用）

| 工具 | 参数 | 行为 |
|---|---|---|
| `read` | `path, offset?=1, limit?=500` | 全文带行号输出（`cat -n` 风格）；offset/limit 翻页；超长截断标注；**path 为目录 → 返回文件名+大小清单** |
| `write` | `path, content` | 创建或整体覆盖；父目录按需创建 |
| `edit` | `path, old_string, new_string` | 精确匹配替换；非唯一匹配报错（要求提供更多上下文） |

路径统一用绝对路径；允许根与拒绝消息中明示。

### 目录布局与约定

```
uploads/.agent_memory/            ← 权限根 #1（全局记忆，跨会话共享）
├── common/MEMORY.md + <主题>.md   ← 领域无关（用户偏好、通用约定）
├── docaudit/MEMORY.md + <主题>.md ← 领域记忆
└── <未来领域>/
uploads/.agent_sessions/<sid>/    ← 权限根 #2（会话工作区，懒创建）
```

`MEMORY.md` 索引是承重墙：存取记忆必须同步维护索引（约定写进工具描述）；注入每轮把相关索引进上下文。领域目录之间不隔离（同一 agent 的记忆）。

### 权限门：参数级路径策略（`permissions/gate.py` 扩展）

- 现有门在 loop 工具派发前逐调用 `allow(tool_call)`（loop.py:695），但只看工具名。扩展为双级：
  - **名级**（既有 `allow`）：工具被整体禁用 → 维持整轮 blocked；
  - **参数级**（新增，如 `path_denial(tool_call) -> str | None`）：`read`/`edit`/`write` 的 path 经 `Path.resolve()` 归一（消化 `..`/symlink）后必须落在允许根内；不满足 → 单调用拒绝；
- **fail-closed**：策略解析异常一律拒绝；
- 允许根在 `build_agent` 按会话构造门时确定（携带 sid 派生的会话工作区路径）；
- 额外允许根留 settings 前向扩展位（将来"uploads 只读"纯配置）。

### 注入（think_phase 钩子保留，实现换血）

`inject_memory_recall` 从关键词检索改为：读 `common/MEMORY.md` + 各活跃领域 `MEMORY.md`，逐索引截 400 字符、hint 总量截 1500 字符，经既有 `context.memory_recall_hint` 模板渲染为 `source="hint"` 消息。每真实用户轮一次、同轮去重机制不变。活跃领域来源：`build_agent` 播种初始 active_domains；`DomainActivator.activate()` 激活领域时通知注入器（新增回调接线，不改 loop）。无索引/无活跃领域 → 只注入 common；连 common 也无 → 零成本跳过。

settings：保留 `memory_auto_inject_enabled`、`memory_auto_inject_max_chars`（语义=单索引帽）；新增 `memory_auto_inject_total_chars`（默认 1500）；删除 `memory_auto_inject_top_k`。

### 子代理

子代理经 fork 共享工具与门策略（会话工作区共享，发现可写文件父代理下轮可读）。**策略必须传播到子代理**：AgentRuntime 派生点传递会话门（否则子代理默认 allow-all 门 = 越权漏洞），E2E 显式验证。

### 退役清单

- 5 个 `memory_*` 工具（含 memory_list）及其注册（`_ensure_builtin_memory_tools` 改为注册三个文件工具）；
- FileMemoryStore 键值/命名空间机制、MemoryManager 的 session/long_term/retrieval 层与 bigram（`_extract_query_terms` 留档于计划文档，将来 grep 工具可复活）；
- MemoryManager 瘦身为 ContextManager + 索引注入；`fork(sub_name)` 的命名空间语义取消，runtime 的 isinstance 分派回退为普通 fork()；
- settings 键 `memory_auto_inject_top_k`；
- dev 测试数据迁移：`long_term/gb_standard.json` → `docaudit/gb_standard.md`（顺手）。

### 模板

`context.memory_recall_hint` 保留（`{memories}` 占位符不变），文案去掉 memory_* 工具名，改为中性表述（索引内容即记忆，操作用文件工具）。

## 实施顺序（逐任务一提交；①先于其余，⑦依赖并行会话落地）

1. `docs` 本计划。
2. `feat(tools)` read/write/edit 三工具 + 单元测试（分页/行号/目录清单/覆盖/唯一匹配）。
3. `feat(permissions)` 权限门参数级路径策略（归一/白名单/fail-closed/拒绝消息）+ 单元测试。
4. `feat(loop)` 单调用拒绝语义（名级熔断保持）+ loop 级测试。⚠️ 触碰 `core/loop.py` 门站点——与并行会话（错误模板化）同文件，落地顺序需协调。
5. `feat(memory)` 索引制注入（MemoryManager 瘦身 + DomainActivator 通知接线 + settings 调整 + 模板文案）+ 测试。
6. `chore` 退役清单执行（删工具/删存储/回退 fork 分派/改注册/迁数据）+ 回归。
7. `test` E2E 实测（并行会话落地后重启服务）：存记忆→同轮/跨轮召回→清单问答（read MEMORY.md）→跨会话隔离（B 会话读 A 工作区被拒）→子代理越权被拒。

## 验收标准

- 全套件 `uv run pytest -m "not integration"` 全绿；触碰文件 ruff/mypy 干净；
- 权限门测试覆盖：穿越、symlink、根外绝对路径、fail-closed、白名单内放行、拒绝消息含根指引；
- 单调用拒绝：一次越界后 run 继续并完成；
- 注入：跟随活跃领域、双帽生效、同轮去重、禁用开关；
- E2E：文件制记忆三场景（存/召回/清单）真实会话通过。

## 风险与已知取舍

- 索引失准（模型漏维护）→ read 目录清单兜底；
- 无关键词检索，大记忆库退化 → 索引制先行，grep 工具二期可选；
- 无 delete，孤儿文件留存 → host 侧后续清理；
- loop.py 门站点与并行会话同文件 → 任务 4 的落地顺序动态协调，必要时 rebase。
