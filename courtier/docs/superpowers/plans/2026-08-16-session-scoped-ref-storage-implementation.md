# $ref 存储层会话隔离与每会话编号 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。Phase 间有依赖（见各 Task「依赖」行），Phase 内 Task 相互独立。

**Goal:** 将工具结果的持久化存储按会话 id 隔离，使 `$ref:<tool>:N` 编号在每个会话内从 1 开始并跨请求连续，同时删除为"跨会话防撞号"而存在的全部全局机制（模块级共享计数器、ES 聚合续号、TTL 缓存）。**ref 字符串格式不变**（`$ref:<tool>:N`），模型侧提示词、工具描述、历史消息引用零改动。**不做历史数据兼容**（旧 ES 文档、旧磁盘文件、版本 1 的 artifact 快照均不再可读，已与用户确认）。

**Architecture:** 改动分布三层：ES 后端（`courtier/agent/runtime/es_backend.py`：文档增 `session_id` 字段、`_id` 改为 `{session_id}#{ref}` 复合键、读取按会话过滤、删除聚合续号）；持久化后端与工件库（`core/cache_store.py`、`artifacts/store.py`：会话子目录、每会话计数器、计数器入快照、删除共享计数器与 seed）；装配层（`agent/api/services/agent_service.py`、`stream_service.py`：session_id 注入构造链）。链路保持：ToolRegistry.execute → ArtifactStore.persist → _PersistenceBackend（磁盘 + ES 主后端）→ get_artifact 读取；子代理经 ScopedArtifactView 共享同一会话 store，编号自然接续。

**Tech Stack:** Python 3.12+、uv、pytest、Elasticsearch 8.x（索引新增 `session_id` keyword 字段，无 reindex——旧文档不再读取）。

**关联文档:** `docs/superpowers/plans/2026-08-16-doccorrector-consistency-and-get-artifact-fixes.md`（同日上一计划，本计划不与其文件冲突）。证据会话：`courtier/.agent_logs/sess_b57b098ea501/`（search_documents 编号从 11 起）。

---

## 背景与现状问题

| # | 问题 | 证据位置 | 阶段 |
|---|------|----------|------|
| 1 | `$ref:<tool>:N` 的 N 是进程级全局序列：模块级 `_SHARED_REF_COUNTERS` 跨所有会话累计，新会话编号不从 1 开始（日志中 search_documents 从 11 起） | `cache_store.py:42`、`cache_store.py:403-419` | 1 |
| 2 | 全局编号存在的唯一理由：ref 兼任共享 ES 结果索引的文档 `_id`，跨会话撞号会用 `op_type=create` 互相覆盖/写失败 | `es_backend.py:150-160` | 0 |
| 3 | 进程重启后靠 ES terms 聚合续号（`seed_ref_counters` + `max_ref_sequences` + TTL 缓存），每次建 store 都要聚合一次 | `store.py:79-84`、`es_backend.py:281-337` | 0/1 |
| 4 | 发号竞态：`persist` 调用 `_next_ref_id` 时未持锁（`cache_store.py:249` 在 `self._lock` 之外），共享计数器无跨会话锁，并发会话可能撞号 | `cache_store.py:233,249` | 1 |
| 5 | 每次 HTTP 请求都新建 store（`build_agent` → `_build_artifact_store` 每请求构造），而 `ref_counters` 不进快照——跨请求编号连续性完全依赖进程内存里的共享计数器，删它必须先让编号入快照 | `agent_service.py:169`、`store.py:296-333`（snapshot 无 ref_counters） | 1 |
| 6 | 磁盘缓存共享目录无会话边界，无法按会话清理 | `cache_store.py:125-127`（`self._cache_dir = Path(cache_dir)`）、`config.py:295` | 0 |

**目标形态：** 每会话独立命名空间：磁盘文件在 `<cache_dir>/<session_id>/` 下，ES 文档带 `session_id` 字段且 `_id` 为 `{session_id}#{ref}` 复合键；每会话计数器从 1 起、经快照跨请求连续；共享计数器、ES 聚合续号、TTL 缓存全部删除；并发撞号问题随全局状态消失。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/agent/runtime/es_backend.py` | mapping 增 `session_id`；`__init__` 接收 session_id；store 写复合 `_id` + session_id 字段；read/_get/_search 按会话过滤；`_ensure_index` 对已存在索引补字段；删除 `max_ref_sequences`、`_seq_seed_cache`、`_SEQ_SEED_TTL_SECONDS`、`tool`/`seq` 写入与 `_REF_ID_RE` | 修改 | 0 |
| `courtier/agent/core/cache_store.py` | `_PersistenceBackend` 接收 session_id，缓存目录改为 `<cache_dir>/<session_id>/`；删除 `_SHARED_REF_COUNTERS` 与 floor 逻辑；`persist` 发号段持锁；删除 `seed_ref_counters` | 修改 | 0/1 |
| `courtier/agent/artifacts/store.py` | `ArtifactStore.__init__`/`restore` 增 `session_id` 参数并透传；snapshot 增 `ref_counters`，`SNAPSHOT_VERSION` 1→2，`load_snapshot` 恢复计数器；删除 `__init__` 里的 seed 调用 | 修改 | 0/1 |
| `courtier/agent/api/services/agent_service.py` | `_build_artifact_store` 增 session_id 并注入 ES 后端与 store 构造；`build_agent` 透传（签名已有 `session_id`） | 修改 | 0 |
| `courtier/agent/api/services/stream_service.py` | `_prepare_artifact_store_for_session` 增 session_id（兜底 `ArtifactStore()` 构造时传入），调用点 `generate_sse_stream` 已有 session_id | 修改 | 0 |
| `tests/courtier/test_es_ref_collision.py` | 重写为会话隔离语义的 ES 单测（fake client） | 修改 | 0/1 |
| `tests/courtier/test_session_scoped_refs.py` | 每会话编号、快照接续、会话目录、并发发号锁单测 | **新建** | 1 |
| `tests/agent/test_cache_store.py`、`tests/agent/test_registry.py`、`tests/agent/test_context_manager.py`、`tests/agent/tools/test_get_artifact*.py`、`tests/agent/tools/test_list_artifacts.py` 等 | 删除 `_SHARED_REF_COUNTERS.clear()` fixture/调用；需要编号断言的用例改用 session_id 构造或快照 round-trip | 修改 | 1 |
| `courtier/prompts/defaults/zh-CN/behavioral.yaml`、`en-US/behavioral.yaml`、各工具 description | **不改**（ref 格式不变） | — | — |

> 构造点核对：`ArtifactStore`/`ElasticsearchResultBackend` 的生产构造点共 4 处——`agent_service.py:302/311`（build_agent 每请求主链路）、`stream_service.py:171`（续接兜底）、`context_manager.py:139`（artifact_store 未注入时的独立场景兜底，无会话上下文，保持默认 session_id=""）、`app.py:148`（应用级 store，与会话无关，不动）。前两处按本表注入 session_id。

> 契约约束（不变）：`$ref:<tool>:N` 字符串格式与 `_REF_PATTERN`/`_EMBEDDED_REF_PATTERN` 不变；`result_id` 字段值不变（仍为 `$ref:<tool>:N`，复合化只发生在 ES `_id` 层）；无 session_id 参数时（测试/CLI 场景）行为与现状一致（共享目录、per-store 编号）。

---

## Phase 0：存储层会话隔离

### Task 0.1: ES 文档会话隔离

**Files:** `courtier/agent/runtime/es_backend.py`、`tests/courtier/test_es_ref_collision.py`

**依赖:** 无。

- [x] **Step 1:** `RESULT_INDEX_MAPPING` 增 `"session_id": {"type": "keyword"}`；删除 `tool`/`seq` 两个属性（新索引不再声明；已有索引遗留字段无害，不处理）。
- [x] **Step 2:** 构造器接收会话 id：

  ```python
  def __init__(self, index_name: str | None = None, session_id: str = "") -> None:
      ...
      self._session_id = session_id
  ```

- [x] **Step 3:** `store()` 改为会话隔离写入：`body["session_id"] = self._session_id`；`_id = f"{self._session_id}#{result_id}"`（session_id 为空时 `_id = result_id`，维持无会话场景）；删除 `tool`/`seq` 字段写入与 `_seq_seed_cache` 更新块；`op_type=create` 保留（现在约束的是会话内唯一性——同一会话并发请求撞号仍要响亮失败）。
- [x] **Step 4:** `_get`/`_search` 按会话过滤：`_get` 直接用复合 `_id`（session_id 为空时用原 result_id）；`_search` 的 bool 查询改为 `must: [term result_id, term session_id]`（session_id 为空时只保留 result_id 条件）。
- [x] **Step 5:** `_ensure_index` 对已存在索引补字段（幂等）：

  ```python
  try:
      self._client.indices.create(index=self._index_name, body=RESULT_INDEX_MAPPING)
  except Exception as exc:
      if "resource_already_exists_exception" in str(exc):
          try:
              self._client.indices.put_mapping(
                  index=self._index_name,
                  body={"properties": {"session_id": {"type": "keyword"}}},
              )
          except Exception:
              logger.warning("Could not update ES result index mapping", exc_info=True)
      else:
          logger.warning("Could not create ES result index: %s", exc)
  ```

- [x] **Step 6:** 删除 `max_ref_sequences`、模块级 `_seq_seed_cache` 与 `_SEQ_SEED_TTL_SECONDS`、`_REF_ID_RE`（删除后 es_backend 内无使用点）。
- [x] **Step 7:** 重写 `test_es_ref_collision.py`（fake client 沿用现有 `_fake_es_client` 模式，去掉聚合 mock）：
  - (a) 写入断言：`_id == f"{sid}#{ref}"`、body 含 `session_id` 且不含 `tool`/`seq`；
  - (b) 两个 backend（不同 session_id）各写 `$ref:search_documents:1` → 两个文档并存互不覆盖；
  - (c) 同一 backend 二次写同一 ref → 抛冲突（create 语义）；
  - (d) `read` 只命中本会话文档；他会话同号文档查不到（`_get` 抛 404 → 返回错误）；
  - (e) 索引已存在时 `put_mapping` 被调用（fake client 断言 `indices.put_mapping` 收到 session_id keyword）。

**验收:** `tests/courtier/test_es_ref_collision.py` 全绿；现有 `tests/courtier/` 其余测试无回归。

### Task 0.2: 磁盘会话子目录 + session_id 注入链路

**Files:** `courtier/agent/core/cache_store.py`、`courtier/agent/artifacts/store.py`、`courtier/agent/api/services/agent_service.py`、`courtier/agent/api/services/stream_service.py`

**依赖:** 无（与 Task 0.1 同批合入）。

- [x] **Step 1:** `_PersistenceBackend.__init__` 增 `session_id: str = ""`：

  ```python
  self._cache_dir = Path(cache_dir) / session_id if session_id else Path(cache_dir)
  self._cache_dir.mkdir(parents=True, exist_ok=True)
  ```

  hash dedup 索引随会话目录落盘，天然会话级去重（同内容跨会话不再共享缓存文件——语义更干净）。
- [x] **Step 2:** `ArtifactStore.__init__(..., session_id: str = "")` 透传 `backend_kwargs["session_id"]`；`ArtifactStore.restore(cls, snapshot, cache_dir, session_id="")` 构造时传入（快照 `ref_map` 是绝对路径，恢复不受目录变化影响；新写入落会话子目录）。
- [x] **Step 3:** `agent_service._build_artifact_store(settings, existing_store, session_id="")`：构造 `ElasticsearchResultBackend(index_name=settings.es_index_results, session_id=session_id)` 与 `ArtifactStore(cache_dir=..., session_id=session_id)`；`build_agent` 内 `store = _build_artifact_store(settings, None)`（line 169）改为传 `session_id=session_id`。
- [x] **Step 4:** `stream_service._prepare_artifact_store_for_session(..., session_id="")`：兜底 `ArtifactStore()` 改为 `ArtifactStore(session_id=session_id)`；`generate_sse_stream` 调用处传入（函数已有 `session_id` 参数）。
- [x] **Step 5:** 单测：带 session_id 的 store persist 后文件位于 `<cache_dir>/<session_id>/` 下且 `ref_map` 指向该目录；两个不同 session_id 的 store 各写同号 ref，文件互不覆盖；不带 session_id（默认）目录行为回归不变。

**验收:** 新增单测全绿；`tests/agent/test_cache_store.py` 现有用例（默认无 session_id）全绿。

---

## Phase 1：每会话编号

### Task 1.1: 删除全局编号机制，计数器入快照

**Files:** `courtier/agent/core/cache_store.py`、`courtier/agent/artifacts/store.py`

**依赖:** Phase 0（复合 `_id` 落地后，会话内从 1 编号才不会在 ES 撞号）。

- [x] **Step 1:** 删除模块级 `_SHARED_REF_COUNTERS`（`cache_store.py:42`）与 `_next_ref_id` 中的 floor 逻辑，只保留 per-store `self.ref_counters` 递增；删除 `seed_ref_counters` 方法。
- [x] **Step 2:** `ArtifactStore.__init__` 删除 seed 调用（`store.py:79-84` 整段）。
- [x] **Step 3:** `persist` 发号段持锁（`self._lock` 是每 store 的 asyncio.Lock，`persist` 本身 async）：

  ```python
  async with self._lock:
      ref_id = self._next_ref_id(tool_name, label)
  ```

  同一会话并发请求共用同一 store（经快照恢复后共享上下文），此锁消除会话内撞号。
- [x] **Step 4:** 编号入快照（解决"每请求重建 store"的连续性）：
  - `snapshot()` 返回 dict 增 `"ref_counters": dict(self.ref_counters)`；
  - `SNAPSHOT_VERSION` 1→2；
  - `load_snapshot()` 恢复：`self._backend.ref_counters.update(snapshot.get("ref_counters") or {})`（快照是会话权威状态，直接合并即可）。
- [x] **Step 5:** 单测（`tests/courtier/test_session_scoped_refs.py`）：
  - (a) 两个 store（session_id A/B）同一工具首次 persist 各得 `$ref:<tool>:1`；
  - (b) 快照 round-trip 后编号接续：persist 得 :1 → snapshot → 新 store load_snapshot → persist 得 :2；
  - (c) 同一 store 并发 persist（`asyncio.gather` 50 次）编号无重复；
  - (d) 无 session_id 时编号行为与现状一致（per-store 从 1 起，进程内不共享）。

**验收:** 新增单测全绿；全量 `uv run pytest -m "not integration"` 无回归（随 Task 1.2 一起执行）。

### Task 1.2: 既有测试适配

**Files:** `tests/agent/test_cache_store.py`、`tests/agent/test_registry.py`、`tests/agent/test_context_manager.py`、`tests/courtier/test_es_ref_collision.py`、`tests/agent/tools/test_get_artifact.py`、`tests/agent/tools/test_get_artifact_defense.py`、`tests/agent/tools/test_list_artifacts.py`、`tests/agent/api/test_agent_service.py` 等

**依赖:** Task 1.1。

- [x] **Step 1:** 删除所有 `_cs._SHARED_REF_COUNTERS.clear()` fixture 与调用（已知 6 处文件：test_cache_store、test_registry、test_context_manager、test_es_ref_collision、test_get_artifact、test_get_artifact_defense；实施时以 grep 全量为准）。
- [x] **Step 2:** 依赖"跨 store 编号递增"语义的用例改为：显式传 session_id 的两个 store 各自独立编号断言，或快照 round-trip 接续断言。
- [x] **Step 3:** 涉及 `ElasticsearchResultBackend(` 构造的测试补 session_id 参数（不传时行为兼容，但按新语义补上更贴合生产路径）。
- [x] **Step 4:** 全量回归并修正失败用例。

**验收:** `uv run pytest -m "not integration"` 全绿。

---

## Phase 2：集成验收（2026-08-17 本地部署 localhost:8000 实测）

- [x] **Step 1:** ES 实测：已存在的 legacy 索引经 `put_mapping` 回填 `session_id` keyword 成功；新会话文档 `_id` 为复合键（实测 `sess_3cfae5b2ff0a#$ref:parse_document:1`、`sess_3cfae5b2ff0a#$ref:content_audit:1`，均带 `session_id` 字段）。fake-client 层面的隔离断言已在单测覆盖。
- [x] **Step 2:** 日志烟测（真实会话 ×3 轮）：
  - 第 1 轮（上传请示文档跑内容审核）：`parse_document:1`、`content_audit:1` —— **新会话编号从 1 开始**；磁盘缓存落 `uploads/.cache/sess_3cfae5b2ff0a/`（`parse_document_1_*.json`、`content_audit_1_*.txt`）；
  - 第 2 轮（续接，触发 check_content，小结果未持久化，无新 ref）；
  - 第 3 轮（续接，强制再次 content_audit）：`content_audit:2` —— **编号经 artifact 快照跨请求接续**，未回 1。
- [x] **Step 3:** 旧文档（无 session_id 的 `_id`=ref 文档）不再被任何读取路径命中：`_get` 走复合键、`_search` 带 session_id 过滤，实测同 ref 他会话读取返回 not found（单测 `test_read_only_hits_own_session` + 实测第 3 轮读回自己的 content_audit:2）。

**烟测中发现的回归（已修复，`247cdbd`）**：上一轮给 CEC 调用加的 `max_tokens=self.max_length`(16383) 在真实端点（上下文 8192）上导致 correct_text 全部 400（prompt 4225 + completion 16384 > 8192）。修复：调用不再传 `max_tokens`；截断守卫升级为**拆分重试**——批次输出截断或 prompt 超长时在段落/句读边界对半拆分重试（下限 200 字、深度 4），拆分对无分隔符拼接保证文本逐字节重组，拆不动才 identity 回退并 warning。真实端点验证：6356 字输入不再报错，拆分重试后 87 条纠错、errors/target 一致，仅 2 个 390 字小批低于下限按原文保留并披露。**注意：部署进程需重启（插件子进程随宿主常驻）后该修复才在线上生效。**

---

## 提交切分（约定式，逐 Task 一提交）

```
feat(storage): session-scoped ES result documents                      # 0.1
feat(storage): per-session cache directories with session_id plumbing  # 0.2
refactor(storage): per-session ref numbering, drop global counters     # 1.1
test(storage): session-scoped ref isolation coverage                   # 1.2
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；新增 `test_session_scoped_refs.py` 覆盖每会话编号、快照接续、并发锁、会话目录四类断言；`test_es_ref_collision.py` 重写为会话隔离语义。
2. 集成（需 ES）：跨会话同号共存 + 会话内读回正确 + mapping 含 session_id。
3. 日志烟测：新会话 ref 从 1 编号；同会话跨请求编号经快照接续。
4. `uv run courtier validate-domain domains/docaudit/` 通过（回归确认无意外破坏）。

## 明确不做的事

- **历史数据兼容**：旧 ES 文档（`_id`=ref、无 session_id 字段）、旧共享目录缓存文件、版本 1 的 artifact 快照一律不再可读/可恢复，不做迁移与双读路径（已与用户确认）。
- **ref 字符串格式**：`$ref:<tool>:N` 不变，`_REF_PATTERN`/`_EMBEDDED_REF_PATTERN`、提示词（behavioral.yaml 等）、工具 description 全部不动。
- 前端与 SSE 协议不动。
- `op_type=create` 冲突语义保留（转为会话内唯一性护栏）；`_write_file` 的文件名 uniq 后缀（时间戳+uuid）保留。
- 不做 ES 索引重建/别名（旧索引用 put_mapping 补字段即可满足新写入）。

---

## 实施记录（2026-08-16）

Phase 0 + Phase 1 已完成（提交 `9ef0186`、`e80ff20`，含代码与测试；提交切分偏差见下）。与原计划的偏差：

1. **提交切分**：计划按 Task 0.1/0.2 分两笔提交，实际 `cache_store.py`/`store.py`/两个 service 同时承载两个 Task 的改动且文件重叠，合并为一笔功能提交（`9ef0186`，message 同时覆盖 ES 隔离与会话目录），测试单独一笔（`e80ff20`）。
2. **Task 1.1 提前并入**：删除 `_SHARED_REF_COUNTERS` 时 `seed_ref_counters` 尚有快照恢复用途，故方法保留但语义改写为「快照恢复合并会话已发编号」（原计划是删除该方法）；发号段持锁、`SNAPSHOT_VERSION` 1→2、`ref_counters` 入快照按计划落地。Phase 1 的代码改动因此与 Phase 0 同批完成。
3. **测试适配范围**：`_SHARED_REF_COUNTERS` fixture 清理涉及 7 个测试文件（计划列了 6 个，`tests/agent/artifacts/test_store_snapshot.py` 实际也引用）；全部脚本化删除，无语义改动。快照测试新增 `ref_counters` 字段断言（版本 2）。
4. **回归**：`uv run pytest -m "not integration"` → 1604 passed, 6 skipped, 22 deselected；`validate-domain` 通过。新增/重写测试 11 个（ES 会话隔离 5 + 每会话编号 5 + 快照计数器 1）。
5. **Phase 2 未执行**：集成测试（需 ES）与部署后日志烟测留待部署验证——预期新会话首个持久化结果 `result_id` 为 `$ref:<tool>:1`，同会话续接经快照接续编号。
