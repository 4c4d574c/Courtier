# 账号注销与数据清除 — 实施计划

## 背景与目标

平台当前对用户只有"停用"(status=disabled,含拒绝注册),没有任何删除路径:账号行、
聊天会话、上传文件、个人资源、审核结果、用户层记忆全部永久留存。本计划实现**真正的
账号注销**:清除该用户的全部个人数据,并留下可核查的清除回执。

清除管线的记忆部分直接接线 `memory_service.delete_user_memory`(记忆 DB 化计划
`memory-db-partition-plan.md` T7 预留的钩子)。

## 数据清除清单(已在代码中核实的归属链)

| 数据 | 存储 | 归属键 | 处置 |
|---|---|---|---|
| 账号行 users | MySQL | — | 删除(refresh_tokens 经 FK CASCADE 自动清除) |
| 聊天会话 + 运行记录 | 磁盘 JSON(`.cache/sessions/`) | owner=用户名 | 逐会话删除(复用现有 delete_session) |
| 会话产物 | 磁盘缓存 + ES `courtier_results` | 按会话 | 随会话清除;ES/MinIO 部分实施时补齐 |
| 上传文件 | 磁盘 `uploads/<年份>/` + `.file_registry` | owner=用户名 | 删文件 + 注册表记录 |
| 个人资源 | MySQL + ES `courtier_chunks` + MinIO | owner_id + visibility=personal | 复用 delete_resource 三层同清 |
| 公共资源 | 同上 | visibility=public | **保留**;防御性置 owner_id=NULL |
| 审核结果 | audit_results 五张表 | user_id=用户名(字符串) | 删除本人行 |
| 共享文档 documents | MySQL + MinIO | doc_id 内容指纹去重,无归属 | **保留**(不含个人标识,可能被他人同内容共享) |
| 用户层记忆 | MySQL memory | owner_id | delete_user_memory 钩子 |
| 审计流水 | memory_changes / settings_changes 等 | actor=用户名 | **保留,actor 匿名化**为 `deleted-user:<id>` |
| settings.updated_by | settings 表 | 用户名 | 同上匿名化 |

范围外(明确不做):Langfuse 追踪(外部观测系统)、插件传输桶(24h 生命周期自动清)、
`librarys` 共享文档库(无归属字段,属系统不属个人)。

## 对齐结论(已与用户确认,2026-09-04)

1. **双入口**:管理员和用户本人都可发起注销;**本人发起需管理员审核通过后执行**,
   管理员发起则选择用户 + 二次确认弹窗后立即执行。
2. **管理员不可被注销**(自助与管理员触发都拒绝;如确需删除某管理员,先由其他管理员
   降级为 auditor 再删)。公共资源只有管理员能传(现有代码已强制),因此
   **公共资源不随账号删除**;级联中仍防御性跳过 public 资源并置 owner_id=NULL。
3. 审计流水选**方案 B**:保留流水,actor 匿名化为 `deleted-user:<id>`(不可复用的
   整型 ID,不是用户名)。
4. 删本人 audit_results 行;保留 documents(内容寻址全局共享)。
5. **用户名和邮箱释放**可再注册。与 3 互相咬合:正因为释放,匿名化必须用不可复用的
   用户 ID 而非用户名,否则新人顶旧名会污染历史引用。不建 tombstone 表。
6. **立即执行**,不做冷静期。执行前强制停止该用户在跑任务;执行后登录态消亡
   (refresh token 随 FK 级联,access token 靠短有效期自然过期)。
7. 确认强度:本人自助需**输入登录密码**(提交申请时验证);管理员触发需选择用户 +
   二次确认弹窗。
8. 需要**删除回执**:独立日志表记录每次注销(目标、触发方式、各类数据清除条数)。

## 设计

### 状态流

```
本人自助:资料页提交(密码验证) → deletion_requests 行(status=pending)
         → 用户期间照常可用,可自行撤回(cancel)
         → 管理员在审批页 批准 → 立即执行级联管线 / 拒绝 → status=rejected
管理员直删:用户管理页 选择用户 + 二次确认 → 立即执行(目标为 admin 则 400)
重复提交 → 409;执行成功后若存在 pending 请求行一并标记 executed
```

### 数据模型(Migration)

```
DeletionRequestTable (deletion_requests)
  id PK; user_id Integer index(不建 FK——执行后用户行已删,行保留作记录);
  status "pending"|"approved"|"rejected"|"cancelled"|"executed";
  requested_by "self"|"admin"; decided_by varchar(管理员用户名,admin 永不删除故不匿名);
  created_at / decided_at

DeletionLogTable (deletion_log) — 清除回执,append-only
  id PK; user_id; trigger "self_approved"|"admin"; executed_by varchar;
  counts JSON(各类数据清除条数: sessions/files/resources/audit_results/memories/…);
  failures JSON(外部清理失败步骤,尽力而为语义); created_at
```

### 级联管线(`account_deletion_service.py`,顺序即依赖)

0. **预检**:目标存在且 role≠admin(双入口都拦);经申请发起时校验请求行 pending。
1. **停跑**:RunManager 停掉该用户全部 running/queued 运行(现有 stop 机制)。
2. **会话**:枚举 owner=用户名 的会话 JSON → 逐会话复用现有删除(连带磁盘产物);
   补齐 ES `courtier_results` / MinIO 会话产物清理(实施时按产物写入面核实)。
3. **上传文件**:file_registry 中 owner=用户名 的记录 → 删磁盘文件 + 注册表行。
4. **个人资源**:visibility=personal 且 owner_id=uid → 复用 delete_resource
   (MySQL + ES chunks + MinIO 原件/转换缓存);public → 置 owner_id=NULL 保留。
5. **审核结果**:五张 audit_results 表 DELETE WHERE user_id=用户名。
6. **记忆**:delete_user_memory(uid)(自带逐条审计,actor=system)。
7. **匿名化**:memory_changes / settings_changes 中 actor ∈ {用户名, agent:用户名}
   → `deleted-user:<id>`;settings.updated_by=用户名 → 同上。
8. **删账号行**:users DELETE(refresh_tokens FK CASCADE 自动)。
9. **回执**:写 deletion_log(counts 逐步累计;外部清理失败的步骤记入 failures);
   pending 请求行标 executed。

**失败语义**:外部数据(磁盘/ES/MinIO)尽力而为、失败记入回执 failures 不阻断;
MySQL 行删除(5-8)在单个事务里,任一步失败整体回滚——最坏情况是"外部对象多删了、
行还在",可重跑管线收敛,不会出现"账号没了数据还在引用它"的反向不一致。

### API

```
POST   /api/profile/deletion-request   {password}   本人提交(密码错 401;admin 403)
GET    /api/profile/deletion-request                本人查询申请状态
DELETE /api/profile/deletion-request                本人撤回
GET    /api/admin/deletion-requests                 pending 列表(含用户名/申请时间)
POST   /api/admin/deletion-requests/{id}/approve    批准并执行
POST   /api/admin/deletion-requests/{id}/reject     拒绝
DELETE /api/admin/users/{id}                        管理员直删(admin 目标 400)
```

### UI(webui)

- **资料页(ProfileView)**:新增「注销账号」区——说明文案 + 密码输入 + 提交;
  pending 态显示"注销申请审核中"+ 撤回按钮;rejected 态提示可重新申请。
- **用户管理(UserManagement)**:行操作加「删除账号」(非 admin 行),二次确认弹窗
  文案明示清除范围;admin 行不显示并提示不可注销。
- **注册审批页(ApprovalManagement)**:加「注销申请」页签(与注册审批并列),
  批准/拒绝操作。

## 任务分解

| # | 任务 | 内容 |
|---|---|---|
| T0 | DB 层 | 两张新表 + migration |
| T1 | 管线 | account_deletion_service(预检/停跑/九步级联/回执)+ 匿名化 helpers |
| T2 | API | profile 三端点 + admin 四端点 |
| T3 | webui | 资料页注销区 + 用户管理删除 + 审批页注销申请页签 |
| T4 | 测试 | 管线级联用例(sqlite + tmp 目录,ES/MinIO 沿用现有 mock 模式)、权限矩阵、回执 |
| T5 | 验证 | 全量 pytest + webui test/build + 真机双账号冒烟(申请→批准→数据清点→回执) |

## 边界与不做

- 不做冷静期/撤销执行(申请可撤,执行不可逆)。
- 不删共享 documents 与公共资源;不做文档引用计数。
- 不做 Langfuse/插件桶清理(见范围外)。
- 用户名/邮箱释放,不建占用表;依赖"匿名化按 ID"保证历史引用不串。
