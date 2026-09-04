# 记忆分层与 DB 化 — 实施计划

## 背景与目标

现行文件制记忆(见 `memory-file-tools-plan.md`,本方案完成后标记取代)是**部署级全局一份**:
`<cache_dir>/../.agent_memory` 按 `common/` + `<domain>/` 分目录,模型用通用文件工具
`read`/`edit`/`write` 直接读写,PathPolicyGuard 会话根 = `[memory_home, session_workspace]`。
分区维度只有"全局 vs 领域",**没有用户维度**——任何用户的 agent 可读写整棵记忆树,
recall 注入也把同一份索引进每个用户的每一轮(实际已发生串扰:`common/user_pref.md`
存的是个人偏好,却被注入所有人)。

目标:

1. **分层**:全局共享层(领域知识/团队规范)+ 用户层(个人偏好/上下文),按用户严格隔离。
2. **DB 唯一真相源**:记忆条目入 `memory` 表;文件制记忆退役,`.agent_memory` 不再使用。
3. **插件形态工具**:新增 `plugins/shared/memory/` 插件,经 host service 回调宿主读写 DB
   (同 `template` 插件先例);**关闭记忆 = 停用插件**,无独立开关。
4. **审计**:每次增删改记 `memory_changes`(hash-only,照 `settings_changes` 先例)。
5. **管理后台露出**:全局记忆页(全角色可看,仅 admin 可编辑)+ 我的记忆页(各管各层)。

## 对齐结论(已与用户确认,2026-09-04)

1. 方案 B 分层:全局共享层 + 用户层;recall 注入用户层在前、全局层在后。
2. DB 唯一真相源;记忆工具做成**插件**;用户要关记忆就停用记忆功能(插件启停即开关)。
3. 匿名会话(owner_id=None)**无记忆**:工具不进注册表、注入直接跳过。
4. **不迁移** `.agent_memory` 存量内容(本地那几个文件直接废弃)。
5. **只有管理员的 agent** 拥有全局层的写/编辑权;普通用户 agent 只读全局层、读写自己的用户层。
6. 审计记 **hash**(sha256,新旧值),不存内容快照;删除不可恢复。
7. admin **不能**浏览普通用户的用户层记忆;删除用户账号时级联删除其全部记忆条目。
8. UI:**全局记忆页全角色可见**(普通用户只读,admin 可增删改);另有「我的记忆」页管自己的层。
9. 存量 `tool_path_policies` 设置机制不变;变的是内置三件套的**基线根**(见下)。
10. **两层都分「通用 + 领域包」两类**(2026-09-04 追加):全局层与用户层各自都有 通用 条目和
    按领域包归类的条目——旧文件制 `common/` + `<domain>/` 的分类维度完整保留,叠加到
    layer 维度上;recall 注入仍只跟随会话活跃领域。
11. **表设计不用 NULL 作默认值**(2026-09-04 追加,哨兵用自解释词不用空串):通用 =
    `domain "common"` 哨兵("common" 为领域包命名保留字),全局层 = `owner_id 0` 哨兵;
    四键列全 NOT NULL,MySQL 唯一索引做实。审计 old/new_hash 的可空照 `settings_changes`
    先例(动作语义,非默认值)。

## 设计

### 数据模型(`courtier/db/tables/memory.py` + Alembic migration)

```
MemoryTable
  id            PK
  layer         "global" | "user"
  owner_id      Integer, NOT NULL, default 0   # 0=全局层哨兵(用户 id 自增从 1 起,不冲突);
                                                # 逻辑引用 users.id,不建 DB 级 FK
  domain        String(64), NOT NULL, default "common"
                # "common"=通用哨兵(自解释,查询/巡表可读);非空领域包名=领域条目(两层都有)
                # "common" 同时是领域包命名保留字:memory 服务写入校验拒绝同名领域包,
                # 哨兵与领域名永不撞车
  title         varchar                 # 条目标题,模型/UI 按它寻址
  content       TEXT
  created_by / updated_by   varchar     # 操作人用户名(与 settings 同风格)
  created_at / updated_at   datetime
  唯一约束:(layer, owner_id, domain, title)
  # 四列全 NOT NULL → MySQL 唯一索引真实去重(NULL 参与时 MySQL 视 NULL≠NULL,约束会失效;
  # 哨兵化的附带收益就是把约束做实了)

MemoryChangeTable(审计,append-only)
  id, entry_id, action(create|update|delete|clear)
  layer, owner_id(NOT NULL default 0), domain(NOT NULL default ""), title   # 条目定位信息
  old_hash / new_hash   sha256, nullable    # delete 时 new_hash 无值;同 settings_changes 先例
  actor             varchar                 # 操作人(用户或 "agent:<username>")
  created_at
```

### 服务层与 API(`memory_service.py` + `routes/memory.py`)

- `MemoryService`:CRUD + **层权限判定** + 审计写入,单点收口(UI、agent 链路都走它)。
- 权限矩阵(fail-closed):

| 调用者 | 全局层读 | 全局层写/删 | 用户层读 | 用户层写/删 |
|---|---|---|---|---|
| admin | ✔ | ✔ | 仅自己 | 仅自己 |
| 普通用户 | ✔ | ✘ | 仅自己 | 仅自己 |
| 匿名 | —(无记忆) | — | — | — |

- 路由(全部登录态;匿名 401):
  - `GET /api/memory/global`、`GET /api/memory/global/{id}` — 全角色只读;
  - `POST/PATCH/DELETE /api/memory/global...`、`DELETE /api/memory/global?all=true` — admin only(403);
  - `GET/POST/PATCH/DELETE /api/memory/mine...`、`DELETE /api/memory/mine?all=true` — 本人层;
  - `GET /api/admin/memory/changes` — 审计流水(admin only)。

### memory 插件(`plugins/shared/memory/`)

- `plugin.yaml`:`host_services: [memory_store]`,`permissions: [read:memory, write:memory]`,
  端口 9110(避开现有 9101-9109)。
- **单工具 `memory`**,action 枚举:`list` / `read` / `write`(按 title upsert)/ `delete`。
  参数:`action, title?, domain?, content?`——`domain` 缺省=`common`(通用),填领域包名=领域
  条目;写入时校验 domain 必须是 `common`(保留哨兵)或已加载领域包名(domain catalog,
  防拼错滋生垃圾分类),但**不**限制为会话活跃领域(注入侧才按活跃领域过滤)。
  模型按 title 寻址,DB id 不出模型面。
- 身份注入走**宿主侧 ContextVar,不过插件的手**:proxy dispatch 与 host service 反向回调
  都发生在宿主进程内,build_agent 组装 agent 时把 `owner_id`/`is_admin` 放入请求级
  ContextVar(沿用 rerank 的 ContextVar 先例),`memory_store` handler 直接读——插件转交的
  任何身份字段仅作日志冗余,授权一律以宿主侧 ContextVar 为准(插件无法伪造身份)。
  实施时验证回调与 dispatch 是否同 task;不同 task 则降级为 dispatch 时显式传 handler 闭包。
- `memory_store` host service(宿主侧注册,新 host service 类型):转发到 `MemoryService`
  并执行层权限矩阵(fail-closed;越权返回结构化错误,模型可见)。
- 审计 actor 标记:agent 发起的写记 `agent:<username>`,UI 发起的记用户名。

### recall 注入改造(`core/memory_manager.py`)

- `_collect_index_segments` 改为经 **构造时注入的 async provider**(duck-typed callable,
  build_agent 传入;core 不 import DB 层——同 prompt_engine/artifact_store 注入先例):
  - 段序:用户层(通用 → 活跃领域)在前,全局层(通用 → 活跃领域)在后;**领域条目只取
    会话活跃领域**(provider 每次注入时读 `self._active_domains`——它已由 build_agent 播种、
    `note_domain_active` 随 `activate_domain` 实时扩展,mid-run 激活下一轮自动生效);
  - 每条目「标题:内容首 ~80 字」;双帽沿用 `memory_auto_inject_max_chars`(单条目)/
    `memory_auto_inject_total_chars`(hint 总量),总量不够时后段整段让位(沿用现 break 语义);
  - provider 查询失败 → 返回空,注入零成本跳过(fail-open,与现 OSError 处理一致)。
- 删除【记忆工作区】硬编码中文段;工具用法指引并入 `context.memory_recall_hint`
  模板(prompt bundle 改文案:用 `memory` 工具 list/read 详情、write/delete 维护)。
- **联动跳过**:provider 由 build_agent 闭包——会话注册表里没有 `memory` 工具
  (插件停用/未连接/匿名)时不注入,避免给模型看得见够不着的索引。

### guard 与文件工具收敛(`agent_service.py`)

- 基线根 `[memory_home, session_workspace]` → `[session_workspace]`(`memory_home` 派生与
  `MemoryManager.memory_home` 属性退役);`session_workspace` 不变(便签/中间产物仍是文件制)。
- 回答"为什么原来有 memory 根":`tool_path_policies` 前端设置只是**每工具声明覆盖**
  (capability meta);内置 read/edit/write 三件套没声明时走的是 build_agent 里写死的
  **基线根**(PathPolicyGuard 的"老名单"分支)。本方案把记忆移出文件工具世界后,
  基线根里自然不再有 memory;`tool_path_policies` 机制本身不动。
- 插件 proxy 注册在全局表、会话 clone;**匿名会话 clone 后 unregister `memory` 工具**
  (模型不可见,而非注册后靠 guard 拒)。

### 管理后台 UI(webui)

- 导航:侧栏「知识与记忆」组加两项(该组现全角色可见,正好):
  - **全局记忆** `/admin/memory`(全角色):列表/搜索/详情,按「通用 / 领域包」分组或筛选
    (领域包名列表来自 domain catalog);admin 显编辑/删除/清空控件,普通用户纯只读
    (按登录角色渲染,后端仍 403 兜底);
  - **我的记忆** `/admin/my-memory`(全角色):自己层 CRUD + 一键清空,同样按
    「通用 / 领域包」分组;新建/编辑可选通用或某个已加载领域包。
- 样式沿用 admin.css / `.tbl-*` 统一表风格;组件内无 style 块(hard constraint)。

### 级联删除

- 删除用户(admin_users 现有链路)时级联 `DELETE FROM memory WHERE owner_id=?`;
  审计表 append-only,条目行删除后历史记录保留(回答"曾经有过什么"靠审计,内容不可恢复)。

## 任务分解

| # | 任务 | 内容 |
|---|---|---|
| T0 | DB 层 | `MemoryTable`/`MemoryChangeTable` + Alembic migration + 查询 helpers |
| T1 | 服务与 API | `MemoryService`(权限矩阵+审计)、`routes/memory.py`、审计查询路由 |
| T2 | host service | `memory_store` 注册 + 请求级身份 ContextVar(含同 task 验证/降级路径) |
| T3 | memory 插件 | `plugins/shared/memory/`(单工具 schema + host service 回调 + plugin.env.example) |
| T4 | 接线 | build_agent:身份 ContextVar、匿名 unregister、guard 根收窄、注入 provider 闭包 |
| T5 | 注入改造 | MemoryManager:DB provider(通用/活跃领域分段+双帽)、hint 模板文案、联动跳过;`memory_home` 退役 |
| T6 | webui | 全局记忆页 + 我的记忆页 + 导航项 + npm test |
| T7 | 级联删除 | 用户删除挂钩 |
| T8 | 文档 | AGENTS.md(root)/CLAUDE.md/courtier CLAUDE.md/guardrails.md 记忆与根段落;本文件收尾 |
| T9 | 验证 | pytest 全量(新权限矩阵/审计/注入/服务用例)+ 真机冒烟(双用户串扰用例) |

## 边界与不做

- 不做记忆条目版本历史/回收站(hash-only 审计,删除不可恢复)。
- 不给普通用户看审计流水(admin-only);不给 admin 看用户层条目。
- 不迁移 `.agent_memory` 存量;部署后该目录不再读写,可手删。
- 领域包写入不限制为会话活跃领域(仅校验 domain catalog 里存在);注入才按活跃领域过滤。
- 不做记忆设置项(注入帽沿用 `memory_auto_inject_*`;开关即插件启停)。
