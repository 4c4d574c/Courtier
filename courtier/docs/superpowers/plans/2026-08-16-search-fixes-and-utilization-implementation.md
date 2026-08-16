# search_documents 缺陷修复与主循环利用强化 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。Phase 间有依赖（见各 Task「依赖」行），Phase 内 Task 相互独立。

**Goal:** 修复 `search_documents` 当前的三类缺陷——(a) 安全/正确性硬伤（owner scope 跨会话泄漏、rerank 丢命中、hybrid 分页破碎）；(b) 检索质量短板（rerank 证据截断、邻块无续读、时间衰减不覆盖向量臂、两臂串行）；(c) 主循环利用断层（持久化后模型只剩 80 字、引用规则三处漂移、limit 与引用上限错配）——并补齐评测回归防线。全程不动已调优的词法检索语义（recall@10 基线 0.9688 不得回退），不破坏插件/宿主架构边界与前端 citations 协议。

**Architecture:** 改动分布四层：插件层（`plugins/shared/search/`：取数窗口、并行、融合衰减、rerank 定位、read_chunks）；宿主工具层（`ToolRegistry.clone()` 每会话克隆 + `ScopedTool` 解包）；主循环层（`loop.py` 引用表坐标、遥测属性）；提示与评测层（prompts 单一事实源收敛、`retrieval_eval.py` 扩维）。链路整体保持：`resource_service.py`（ingest）→ `courtier/es/client.py`（索引）→ `plugins/shared/search/`（查询）→ 宿主（scope 注入、引用编号）→ 前端 citations。

**Tech Stack:** Python 3.12+、uv、pytest、Elasticsearch 8.x（无新 mapping 变更、无 reindex）、OpenAI 兼容 embedding/rerank 端点（沿用现有配置注入）。

**关联文档:** `docs/superpowers/plans/2026-08-13-search-rag-retrieval-implementation.md`（上一轮 RAG 升级，本计划是其后续缺陷修复；词法查询语义、RRF、缓存、邻块机制均由该计划引入，本计划不改其设计意图）。

---

## 背景与现状问题

上一轮 RAG 升级引入的叠加层存在以下缺陷（编号贯穿全文，证据为 2026-08-16 代码审查结论）：

| # | 问题 | 证据位置 | 阶段 |
|---|------|----------|------|
| 1 | `apply_owner_scope` 在共享 `app.state.tool_registry` 上叠层包装 `ScopedTool`，内层注入覆盖外层 → **进程启动后第一个用户的可见域永久生效，后续所有用户以其身份过滤（跨用户私有块泄漏）** | `agent/api/services/agent_service.py:30-35`、`tools/scoped.py:42`、`routes/sessions.py:214` | 0 |
| 2 | rerank 解析只采纳模型返回的编号，未覆盖的候选**静默丢弃**（10 候选回 `[3,1]` 只剩 2 条，total 仍为 10） | `plugins/shared/search/rerank.py:105-109` | 0 |
| 3 | hybrid 非 rerank 路径词法臂只取 `limit` 条，融合后按 `[skip:skip+limit]` 切片 → 翻页时词法臂深排名文档从未参与融合；`total` 只取词法臂计数，hybrid 下失真 | `plugins/shared/search/tools.py:690-697,722,763` | 0 |
| 4 | rerank 候选只取 `chunk_text` 前 200 字，长 chunk 中 200 字后的相关条款被误判低相关 | `rerank.py:19` | 1 |
| 5 | 邻块只有 300 字预览且模型无任何工具读回完整 chunk，跨块条款续文引用不到 | `tools.py:68,518-543` | 1 |
| 6 | `use_time_decay` 的 gauss 只在词法臂（`function_score`），kNN 臂无衰减，hybrid 下两臂时效偏好不一致 | `tools.py:313-338,361-373` | 1 |
| 7 | 词法与 kNN 两臂串行 await（注释称 parallel 名不副实），最坏 5 层串行网络往返 | `tools.py:692-717` | 1 |
| 8 | 评测盲区：golden 集仅 32 条、132 块小库；scope 过滤、hybrid 分页、rerank 覆盖率无评测 | `scripts/retrieval_eval.py` | 3 |
| 9 | 结果持久化后模型视角仅剩 `【引用编号 N】标题｜前80字`，读不到条文内容，转述即编造风险 | `core/loop.py:210-227` | 2 |
| 10 | 引用规则三处维护：`plugin.yaml` system_prompt **从未生效**（运行时只发 `entry.py` 的短版），真正生效的是 `behavioral.yaml`；已现格式漂移 | `plugin.yaml:11-33`、`entry.py:8-19`、`prompts/defaults/zh-CN/behavioral.yaml:26-40`、`plugin/manager.py:635` | 2 |
| 11 | 工具 `limit` 上限 100 但 `_CITATION_MAX_HITS=50`，第 51+ 条命中无 `citation_index`，引用行为未定义 | `tools.py:28`、`loop.py:59` | 2 |
| 12 | 行为规则要求 1~3 次改写查询交叉验证，微压缩可能中途挤掉早期搜索正文，而引用编号跨调用累计 → "编号在、内容没了" | `behavioral.yaml:40`、`agent_service.py:247` | 2 |
| 13 | `_score` 在词法模式是 BM25 分、hybrid 是 RRF 分（≈0.016 量级），同字段两种量纲误导模型 | `tools.py:491-495` | 2 |
| 14 | 检索旋钮（KNN_K、RERANK_FETCH、NEIGHBOR_WINDOW、CACHE_TTL 等）全部进程内常量，不可部署调整 | `tools.py:73-91` | 3 |
| 15 | 120s TTL 缓存无索引写入失效，新入库文档最长 2 分钟搜不到 | `tools.py:88-91` | 3 |

**目标形态：** 每会话隔离的 scope 注入；rerank 永不丢命中；hybrid 分页语义一致且 total 诚实；rerank 证据窗口自适应并对准最相关段落；模型可按坐标读回完整 chunk；引用规则单一事实源；全部关键行为有评测回归门槛。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/agent/tools/registry.py` | `ToolRegistry.clone()` 每会话浅克隆 | 修改 | 0 |
| `courtier/agent/tools/scoped.py` | `ScopedTool.unwrapped` 解包属性 | 修改 | 0 |
| `courtier/agent/api/services/agent_service.py` | `build_agent` 克隆 registry；`apply_owner_scope` 解包后单层包装；`_SCOPE_SENSITIVE_TOOLS` 增补 | 修改 | 0/1 |
| `plugins/shared/search/rerank.py` | 余量回填 + 覆盖率门槛；自适应候选长度 + 段落定位 | 修改 | 0/1 |
| `plugins/shared/search/tools.py` | 统一取数窗口 + total_mode；两臂并行；融合层客户端衰减；rank 字段；`_MAX_LIMIT`→50；`ReadChunksTool`；环境变量旋钮 | 修改 | 0/1/2/3 |
| `courtier/agent/core/loop.py` | 引用表携带 rid/chunk 坐标；OTel 增补 rerank_partial/time_decay 属性 | 修改 | 2 |
| `plugins/shared/search/plugin.yaml` | 删除死配置 `capabilities:` 节；`runtime.env` 增补旋钮默认值 | 修改 | 2/3 |
| `plugins/shared/search/entry.py` | 删除冗余 system_prompt；注册 `read_chunks`；注册缓存失效通知 | 修改 | 1/2/3 |
| `courtier/prompts/defaults/zh-CN/behavioral.yaml` | 修 27 行格式损伤；增补 read_chunks 使用指引 | 修改 | 2 |
| `courtier/prompts/defaults/{zh-CN,en-US}/context.yaml` | 压缩提示词增补"保留引用表与坐标"规则 | 修改 | 2 |
| `libs/shared/plugin_sdk/src/courtier_plugin_sdk/runtime.py` | `on_notification()` 自定义通知注册 | 修改 | 3 |
| `courtier/plugin/manager.py`、`courtier/plugin/__init__.py` | `ProcessManager.notify` / `PluginSystem.broadcast` | 修改 | 3 |
| `courtier/agent/api/services/resource_service.py` | 批量索引成功后广播 `search.cache_clear` | 修改 | 3 |
| `scripts/retrieval_eval.py`、`scripts/retrieval_golden.jsonl` | 评测模式扩展（scope/rerank/分页）与 golden 集扩充 | 修改 | 3 |
| `tests/agent/test_agent_service_scoping.py` | 会话间 scope 隔离回归 | **新建** | 0 |
| `tests/courtier/test_tool_registry_clone.py` | clone 语义单测 | **新建** | 0 |
| `tests/plugin/test_search_plugin.py` | 窗口/回填/衰减/定位/rank/read_chunks 单测 | 修改 | 0/1/2 |
| `tests/agent/test_loop_citations.py` | 引用表坐标格式断言 | 修改 | 2 |
| `tests/courtier/test_prompt_single_source.py` | 引用规则单一事实源防漂移护栏 | **新建** | 2 |

> 宿主侧结果流约束（不变）：前端 `CitationHit`（`api/models.py:158`）字段全可选，新增字段（`rank`/`total_mode`/`rerank_partial`/`time_decay_applied`）只进模型 payload 与 OTel，不改 SSE citations 协议。

---

## Phase 0：安全与正确性（必须最先合入）

### Task 0.1: 每会话 ToolRegistry 克隆 + owner scope 单层包装

**Files:** `courtier/agent/tools/registry.py`、`courtier/agent/tools/scoped.py`、`courtier/agent/api/services/agent_service.py`、`tests/courtier/test_tool_registry_clone.py`、`tests/agent/test_agent_service_scoping.py`

**依赖:** 无（本 Phase 前置）。

- [x] **Step 1:** `ToolRegistry` 新增 `clone()`：

  ```python
  def clone(self) -> "ToolRegistry":
      """Per-session shallow copy: same tool instances, fresh bookkeeping.

      Shares policy/projector/result_store/summarizer by reference; counters
      and registrations are independent so per-run state never leaks across
      concurrent sessions.
      """
      clone = ToolRegistry(
          policy=self._policy,
          projector_registry=self._projector_registry,
          result_store=self._result_store,
          summarizer=self._summarizer,
      )
      for tool in self._tools.values():
          clone.register(tool, force=True)
      return clone
  ```

  只复制注册表与计数器，投影策略/结果存储按引用共享（`_policy_lock` 各自新建，随新实例构造）。`_producer_cache` 不复制（register 时已置 None，懒重建）。
- [x] **Step 2:** `ScopedTool` 新增解包属性：

  ```python
  @property
  def unwrapped(self) -> Any:
      """Peel any ScopedTool chain down to the original tool."""
      inner = self._inner
      while isinstance(inner, ScopedTool):
          inner = inner._inner
      return inner
  ```
- [x] **Step 3:** `build_agent`（`agent_service.py`）在构建 `AgentRuntime`/`DomainActivator`/`OrchestratorAgent` 前：

  ```python
  if tool_registry is not None:
      session_registry = tool_registry.clone()
  # AgentRuntime / DomainActivator / OrchestratorAgent 全部改传 session_registry
  ```

  域激活状态跨请求本就由 `active_domains` 重放（`agent_service.py:210`），会话级克隆不破坏该机制；admin 接口继续读 `app.state.tool_registry` 基座（裸 ProxyTool，维持 `_QUERY_UNSET` 内部语义）。
- [x] **Step 4:** `apply_owner_scope` 改为解包后单层包装：

  ```python
  inner = registry.get(name)
  if isinstance(inner, ScopedTool):
      inner = inner.unwrapped        # 防御性：永远回到原始 ProxyTool
  registry.register(ScopedTool(inner, {"_owner_scope": owner_id}), force=True)
  ```
- [x] **Step 5:** 单测 `test_tool_registry_clone.py`：clone 后 (a) `get` 返回同一工具实例；(b) 副本 `register(force=True)` 不影响基座条目；(c) 副本 `reset_run_state()` 不清基座计数。
- [x] **Step 6:** 单测 `test_agent_service_scoping.py`：同一基座连续 `build_agent` 两次（owner_id=1、2，stub search 工具记录每次 execute 收到的 `_owner_scope`）→ (a) 各 agent 执行时各见其 owner；(b) 构建 N 次后注册表内 `search_documents` 的 `ScopedTool` 嵌套深度恒为 1；(c) `asyncio.gather` 并发构建 + 交错执行无交叉污染。

**验收:** 上述测试全绿；全量 `uv run pytest -m "not integration"` 无回归。提交信息注明顺带修复的同类问题：`_attach_skill_callbacks`（`orch.py:272`）在共享 registry 上并发覆盖 SkillTool 回调、`reset_run_state` 跨会话互清计数——克隆后天然隔离。

### Task 0.2: rerank 余量回填 + 覆盖率门槛

**Files:** `plugins/shared/search/rerank.py`、`plugins/shared/search/tools.py`、`tests/plugin/test_search_plugin.py`

**依赖:** 无。

- [x] **Step 1:** `rerank_hits` 返回签名改为 `tuple[list[dict], bool]`（hits, partial）：

  ```python
  ordered = [by_index[n] for n in order]
  ranked = set(order)
  remainder = [hit for i, hit in enumerate(hits, 1) if i not in ranked]
  if len(order) * 2 < len(hits):      # 覆盖率 < 50%：模型输出不可信，整体保持原序
      return list(hits), True
  return ordered + remainder, bool(remainder)
  ```
- [x] **Step 2:** `tools.py` rerank 分支改为解包元组：`ordered, partial = await rerank_hits(...)`；`cleaned["rerank_partial"] = partial`。异常分支（保持原序）设 `cleaned["rerank_partial"] = True`，语义统一为"重排未完全生效"。
- [x] **Step 3:** `loop.py` `_set_search_tool_attributes` 增补 `tool_span.set_attribute("retrieval.rerank_partial", bool(data.get("rerank_partial")))`。

**验收:** 单测：10 候选模拟模型回 `[3,1]` → 断言返回 10 条、前 2 为 3/1、余量原序殿后、`rerank_partial=True`；回覆盖率 30% 的列表 → 断言整体原序；回全量 `[5,2,9,...]` → `rerank_partial=False`。

### Task 0.3: hybrid 统一取数窗口 + total 语义

**Files:** `plugins/shared/search/tools.py`、`tests/plugin/test_search_plugin.py`

**依赖:** 无。

- [x] **Step 1:** 定义窗口（旋钮 `SEARCH_MAX_WINDOW`，见 Task 3.1，默认 200）：

  ```python
  window = min(max(skip + limit, _KNN_K), _MAX_WINDOW)
  if rerank:
      lex_skip, lex_limit = 0, min(window, _RERANK_MAX_FETCH)   # _RERANK_MAX_FETCH = 100
  else:
      lex_skip, lex_limit = 0, window
  ```
- [x] **Step 2:** `_build_knn_query` 增加 `k: int` 参数（`num_candidates = max(_KNN_NUM_CANDIDATES, k)`）；hybrid 调用处传 `k=max(_KNN_K, window)`、`limit=window`。两臂同窗口，融合后照旧 `[skip:skip+limit]` 切片——第 N 页的融合名单包含词法臂 top(skip+limit)。
- [x] **Step 3:** total 语义按模式显式化，响应恒带 `total_mode`：
  - 词法模式：`total = _extract_total(raw)`、`"total_mode": "exact"`（现状不变）；
  - hybrid 模式：`total = len(fused)`、`"total_mode": "window"`；
  - rerank 模式：随其底层模式（fused 或 lexical 窗口）。
- [x] **Step 4:** 工具 `description` 与 `total` 参数说明更新："hybrid/重排模式下翻页深度受融合窗口上限（默认 200）约束，更深分页返回空页，请改用 document_id/doc_type/tags 过滤缩小范围；hybrid 下 total 为窗口内融合候选数（total_mode=window）"。
- [x] **Step 5:** 单测（mock `search_chunks`）：skip=0/10/20 三页断言 (a) 每页词法臂取数均为当页 `skip+limit`；(b) 三页并集无重复无遗漏；(c) `total_mode` 各模式正确；(d) skip 超窗口返回空 hits 不报错。

**验收:** 分页一致性测试全绿；`retrieval_eval.py` 第 1 页指标不低于基线（窗口取数只会变多不会变少）。

---

## Phase 1：检索质量

### Task 1.1: 词法臂与 embedding 并行取数

**Files:** `plugins/shared/search/tools.py`

**依赖:** Task 0.3（窗口参数定型后再重排执行序）。

- [x] **Step 1:** 非 rerank 分支重构执行序（kNN 依赖向量只能后置）：

  ```python
  hybrid_planned = embedding_config() is not None        # 纯配置检查，无网络
  coros = [asyncio.to_thread(search_chunks, query_body=es_body, skip=lex_skip, limit=lex_limit)]
  if hybrid_planned:
      coros.append(embed_query(query))                    # embedding 与词法并行
  results = await asyncio.gather(*coros, return_exceptions=True)
  # embed 失败按现状降级 lexical（warning），词法失败返回错误 ToolResult
  ```
- [x] **Step 2:** 拿到向量后发起 kNN（`asyncio.to_thread`），随后融合。最坏路径从 5 层串行往返降为 4 层（embed∥词法 → kNN → rerank → 邻居）。
- [x] **Step 3:** es 客户端并发安全说明：`es_client.py` 单例线程安全（`Elasticsearch` 客户端连接池复用），`asyncio.to_thread` 两路并发无共享可变状态；在代码注释注明。

**验收:** 现有插件测试全绿；mock 层用 `threading.Barrier(2)` 断言词法与 embed 重叠执行。

### Task 1.2: 融合层统一客户端时间衰减

**Files:** `plugins/shared/search/tools.py`、`tests/plugin/test_search_plugin.py`

**依赖:** Task 1.1（执行序定型）。

- [x] **Step 1:** 新增纯函数（与 ES gauss 同形：`decay^((age/scale)^2)`）：

  ```python
  _TIME_DECAY_SCALE_DAYS = 730.0   # 与 _TIME_DECAY_SCALE "730d" 对应

  def _decay_multiplier(publish_date: str | None, now: date) -> float:
      if not publish_date:
          return 1.0                                   # 无日期=中性，与现行为一致
      d = date.fromisoformat(str(publish_date)[:10])   # ES date 字段取日期部分
      age = max((now - d).days, 0)
      return _TIME_DECAY_DECAY ** ((age / _TIME_DECAY_SCALE_DAYS) ** 2)
  ```
- [x] **Step 2:** 衰减归属规则（单一化，消除两臂不一致）：
  - embedding **未配置**（永远纯词法）：维持现状服务端 gauss（`_build_es_query(use_time_decay=True)`）；
  - embedding 已配置（hybrid 规划）：`_build_es_query` 传 `use_time_decay=False`（词法臂不带 gauss，避免双重衰减），融合后对每条 hit 的 `_rrf` 乘 `_decay_multiplier`，再参与 `_fusion_sort_key` 排序——实现上给 `_rrf_fuse` 加 `decay_fn: Callable[[dict], float] | None` 参数，排序前逐 hit 应用；
  - embedding 配置了但本次失败（降级纯词法）：该次调用无衰减，响应加 `"time_decay_applied": False` 并记 warning，**不重发查询**（降级路径重发得不偿失）。衰减生效时置 `True`。
- [x] **Step 3:** `loop.py` OTel 增补 `retrieval.time_decay` 属性（读 `time_decay_applied`）。
- [x] **Step 4:** 单测：(a) 新旧文档混合的 fused 列表，断言老文档 `_rrf` 被压低且排序下移、无日期文档乘 1.0 不受惩罚；(b) 词法单臂模式查询体仍含 `function_score`（现状回归）；(c) hybrid 规划时词法臂查询体不含 `function_score`。

**验收:** 新旧混排 golden 用例排序符合预期（见 Task 3.3 新增用例）；纯词法部署行为零变化。

### Task 1.3: rerank 自适应候选长度 + 段落定位

**Files:** `plugins/shared/search/rerank.py`、`tests/plugin/test_search_plugin.py`

**依赖:** Task 0.2（回填语义定型后改证据窗口）。

- [x] **Step 1:** 自适应长度（总预算守恒，候选越多单条越短）：

  ```python
  _CANDIDATE_BUDGET_CHARS = 24_000   # SEARCH_CANDIDATE_BUDGET_CHARS，见 Task 3.1
  _CANDIDATE_MIN_CHARS = 240
  _CANDIDATE_MAX_CHARS = 800

  def _candidate_chars(n: int) -> int:
      return min(_MAX, max(_MIN, _BUDGET // max(n, 1)))   # 50 候选≈480 字/条
  ```
- [x] **Step 2:** 段落定位——按查询词元对句子打分，取最相关句子为中心的窗口（确定性、纯 Python、零额外网络）：

  ```python
  _SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；！？\n])")

  def _query_grams(query: str) -> set[str]:
      """引号短语整句参与 + 自由文本 bigram（复用 tools._parse_query，插件内无循环导入：
      tools.py 只在函数体内 import rerank）。"""
      phrases, free = _parse_query(query)
      grams = set(p for p in phrases if p)
      cleaned = re.sub(r"[\s，。；、！？·\"'“”‘’「」]", "", free)
      grams |= {cleaned[i:i+2] for i in range(len(cleaned) - 1)}
      return grams

  def _best_window(grams: set[str], text: str, chars: int) -> str:
      """最相关句为中心的 chars 字窗口；无词元重叠（如纯 kNN 命中）退回首段。"""
      if len(text) <= chars:
          return text
      best_score, best_start, offset = 0, 0, 0
      for sent in _SENTENCE_SPLIT_RE.split(text):
          if sent.strip():
              score = sum(1 for g in grams if g in sent)
              if score > best_score:
                  best_score, best_start = score, offset
          offset += len(sent)
      if best_score == 0:
          return text[:chars]
      start = max(0, best_start - chars // 4)        # 命中句前置 1/4 上下文
      end = min(len(text), start + chars)
      start = max(0, end - chars)
      prefix, suffix = ("…" if start > 0 else ""), ("…" if end < len(text) else "")
      return prefix + text[start:end] + suffix
  ```
- [x] **Step 3:** `_prompt` 组装改为：`text = _best_window(grams, hit.get("chunk_text") or "", chars)`，`chars = _candidate_chars(len(hits))`；prompt 中注明"候选为原文节选，省略号表示截断"。
- [x] **Step 4:** 单测：(a) 2000 字 chunk、相关条款在约 400 字处，断言 prompt 包含该条款文本且窗口以之为中心；(b) 无重叠（grams 与文本无交集）退回首段；(c) 10 候选 vs 50 候选的单条长度符合预算公式；(d) 窗口加省略号边界正确。

**验收:** 长 chunk golden 用例（Task 3.3 新增）重排 recall@5 不低于头窗口基线；`retrieval_eval.py --rerank` 总体 recall@10 ≥ 基线。

### Task 1.4: `read_chunks` 工具（坐标级全文回读）

**Files:** `plugins/shared/search/tools.py`、`plugins/shared/search/entry.py`、`courtier/agent/api/services/agent_service.py`、`tests/plugin/test_search_plugin.py`

**依赖:** Task 0.1（scope 注入正确性前提）。

- [x] **Step 1:** `tools.py` 新增 `ReadChunksTool`：

  ```python
  name = "read_chunks"
  description = "按 resource_id+chunk_no 坐标取回完整文档块原文（含跨块条款续文）。
      坐标来自 search_documents 结果的 hits/neighbors 字段。最多 10 个坐标。"
  parameters = {
      "type": "object",
      "properties": {
          "chunks": {"type": "array", "maxItems": 10, "items": {"type": "object",
              "properties": {"resource_id": {"type": "integer"},
                             "chunk_no": {"type": "integer"}},
              "required": ["resource_id", "chunk_no"]}},
          "with_neighbors": {"type": "boolean",
              "description": "同时返回每个坐标 ±1 相邻块，默认 false"},
      },
      "required": ["chunks"],
  }
  skip_persist = True    # 关键：结果不走宿主 $ref 持久化，否则又变回摘要，循环依赖
  ```
- [x] **Step 2:** `execute` 实现：坐标去重、上限校验 → 每坐标一条 `bool.must: [term resource_id, term chunk_no]`（`with_neighbors` 时改 `range chunk_no ±1`）→ 叠加 `_owner_scope` 过滤（`kwargs.get("_owner_scope", _QUERY_UNSET)`，同主查询语义）→ 一次 ES 查询（`limit = len(coords) * 3`）→ 返回：

  ```json
  {"chunks": [{"resource_id": 1, "chunk_no": 5, "title": "...", "doc_type": "...",
               "chunk_text": "每块截 3000 字", "publish_date": "..."}],
   "missing": [坐标列表]}   // 查询命中数 < 请求数时列出未找到/不可见坐标
  ```

  总量封顶 30_000 字符（超出截断最后一块并加 `"truncated": true`）。不可见块因 scope 过滤自然落进 `missing`，**不区分"不存在"与"无权限"**（避免存在性泄漏）。
- [x] **Step 3:** `entry.py` `_setup_handlers` 增 `self.register_tool(ReadChunksTool())`；`agent_service.py` 的 `_SCOPE_SENSITIVE_TOOLS` 改为 `("search_documents", "read_chunks")`。
- [x] **Step 4:** `behavioral.yaml` 检索指引（zh-CN 与 en-US 同步）增补一条："需要完整条文原文或邻块续文时，用 `read_chunks` 传入 hits/neighbors 中的 `resource_id`+`chunk_no` 坐标取回；禁止凭预览转述条文原文"。
- [x] **Step 5:** 单测：坐标命中/越界/去重/上限拒绝；scope 过滤下他人私有块进 `missing`；`with_neighbors` 返回 ±1；`skip_persist` 标记经注册 payload 透传到宿主 ProxyTool。

**验收:** 集成测试（`pytest.mark.integration`，需 ES）：搜索 → 结果持久化 → `read_chunks` 按 rid/chunk 回读完整原文的端到端链路。

---

## Phase 2：主循环利用检索结果

### Task 2.1: 引用表携带 rid/chunk 坐标

**Files:** `courtier/agent/core/loop.py`、`tests/agent/test_loop_citations.py`

**依赖:** 无（与 Task 1.4 组合形成"编号 + 坐标 + 回读"闭环，但可先行合入）。

- [x] **Step 1:** `_annotate_search_citations` 持久化分支的引用表行（`loop.py:218`）改为：

  ```python
  compact.append(
      f"【引用编号 {offset + i + 1}】{title}｜rid={rid},chunk={cno}｜{chunk[:80]}"
  )
  ```

  `resource_id`/`chunk_no` 取自 hit（缺省时该段省略坐标）。80 字预览保留——职责是编号对齐不是供阅读，全文走 `read_chunks`。
- [x] **Step 2:** 同函数 inline 分支无需改动（hits 全量携带两字段）；`_build_citations_payload` 不动（前端协议不变）。
- [x] **Step 3:** 测试断言新格式（含坐标、缺坐标时格式回退）。

### Task 2.2: 引用规则单一事实源收敛

**Files:** `plugins/shared/search/plugin.yaml`、`plugins/shared/search/entry.py`、`courtier/prompts/defaults/zh-CN/behavioral.yaml`、`tests/courtier/test_prompt_single_source.py`

**依赖:** 无。

- [x] **Step 1:** 删除 `plugin.yaml` 整个 `capabilities:` 节（死配置：运行时注册完全由 `entry.py` 的 `register_tool` 驱动，SDK `_collect_capabilities` 不读 manifest，宿主只消费 register notification）。manifest 保留 name/version/api/description/runtime。
- [x] **Step 2:** `entry.py` `register_capabilities` 改回返回 `[]`（删除短版 system_prompt——查询改写建议已在 `behavioral.yaml:40`，两份只会漂移）。
- [x] **Step 3:** `behavioral.yaml` 修掉 27 行两条规则挤成一行的格式损伤（"…[[0]] 是无效标记" 与 "【编号来源】…" 拆回两个 `·` 列表项）；en-US 版本同步核对。
- [x] **Step 4:** 防漂移护栏 `test_prompt_single_source.py`：(a) 进程内拉起 SearchPlugin，断言 register payload 的 `system_prompt == ""` 且 capabilities 仅含两个工具；(b) PromptEngine 渲染 `behavioral` bundle，断言含 `citation_index`、`【引用编号`、`read_chunks` 关键词。未来往 plugin.yaml 塞规则直接红。

### Task 2.3: limit 与引用上限对齐（100 → 50）

**Files:** `plugins/shared/search/tools.py`

**依赖:** Task 0.3（窗口公式已按上限参数化）。

- [x] **Step 1:** `_MAX_LIMIT = 50`；工具 description 增"最多返回 50 条"。`_CITATION_MAX_HITS`（`loop.py:59`）保持 50——每条命中恒有 `citation_index`，未定义行为消除。
- [x] **Step 2:** 回归：现有超限用例参数改 50 边界；`retrieval_eval.py --top-k 10` 不受影响。

### Task 2.4: 压缩保留引用表与坐标

**Files:** `courtier/prompts/defaults/zh-CN/context.yaml`、`courtier/prompts/defaults/en-US/context.yaml`

**依赖:** Task 2.1（坐标格式先行）。

- [x] **Step 1:** `context.compact_prompt` 与 `context.compact_merge_prompt` 模板各增补一条规则："压缩工具结果时，必须原样保留 search_documents 结果中的【引用编号 N】引用表及其 rid/chunk 坐标，不得摘要、改写或丢弃；后续回答依赖这些编号做 [[n]] 引用定位"。en-US 镜像同步。
- [x] **Step 2:** 不改 `context_recent_tool_results_tokens` 默认值（留作部署调优旋钮）；配合 read_chunks 后压缩丢正文不再致命。

### Task 2.5: hits 显式 rank 字段

**Files:** `plugins/shared/search/tools.py`

**依赖:** 无。

- [x] **Step 1:** rank 是页内序，须在最终列表定型后赋值（避免缓存/重排路径下错位）：`execute` 返回前统一 `for i, hit in enumerate(cleaned.get("hits") or [], 1): hit["rank"] = i`。缓存命中路径同赋值（缓存键含 skip/limit，页内序稳定）。neighbors 不赋 rank。
- [x] **Step 2:** 工具 description 增注："相关性以 rank 为准（页内 1 起序号），`_score` 跨模式不可比仅供排障"。
- [x] **Step 3:** 单测：hybrid 切页、rerank 重排、缓存命中三条路径的 rank 连续正确。

---

## Phase 3：工程化与评测防线

### Task 3.1: 检索旋钮环境变量化

**Files:** `plugins/shared/search/tools.py`、`plugins/shared/search/rerank.py`、`plugins/shared/search/plugin.yaml`

**依赖:** Phase 0/1 参数定型后统一收口。

- [x] **Step 1:** `tools.py` 增 `_env_int(name, default)` 辅助，常量改为启动时读取：`SEARCH_KNN_K`(50)、`SEARCH_MAX_WINDOW`(200)、`SEARCH_RERANK_FETCH`(100)、`SEARCH_NEIGHBOR_WINDOW`(2)、`SEARCH_CACHE_TTL_S`(120)、`SEARCH_CACHE_MAX_ENTRIES`(256)；`rerank.py`：`SEARCH_CANDIDATE_BUDGET_CHARS`(24000)。模块级读取一次（与现有常量同生命周期）。
- [x] **Step 2:** `plugin.yaml` `runtime.env` 增补上述默认值（**字面量**，不走 `${ENV:}` 引用，不涉及宿主白名单）；注释说明各旋钮含义。部署改参无需发版。

### Task 3.2: 索引写入后的缓存失效广播

**Files:** `libs/shared/plugin_sdk/src/courtier_plugin_sdk/runtime.py`、`courtier/plugin/manager.py`、`courtier/plugin/__init__.py`、`plugins/shared/search/entry.py`、`courtier/agent/api/services/resource_service.py`

**依赖:** 无。

- [x] **Step 1:** SDK `PluginRuntime` 增 `on_notification(method)` 装饰器（注册进 `_notification_handlers: dict[str, Callable]`）；`_handle_notification` 在内置分支前先查自定义表，命中则异步调度（`create_task` + 异常日志）。
- [x] **Step 2:** `entry.py` 注册：`@self.on_notification("search.cache_clear")` → `tools._cache_clear()`。
- [x] **Step 3:** 宿主 `ProcessManager` 增 `async def notify(self, plugin_name, method, params=None)`（状态非 ACTIVE 时静默跳过）；`PluginSystem` 暴露 `async def broadcast(self, method, params=None)`（遍历存活插件逐个 notify，单插件失败 warning 不中断）。
- [x] **Step 4:** `resource_service.py` 批量索引成功返回前调用 `broadcast("search.cache_clear")`（plugin_system 经参数注入；不可用时跳过并 debug 日志，**不影响入库主流程**）。TTL 120s 保留兜底漏发通知。
- [x] **Step 5:** 单测：SDK 通知分发（sync/async handler、未知方法忽略）；宿主 broadcast 对停止插件跳过；resource_service 注入缺失不抛错。

### Task 3.3: 评测扩展与回归门槛

**Files:** `scripts/retrieval_eval.py`、`scripts/retrieval_golden.jsonl`

**依赖:** Phase 0-2 全部合入后执行基线重测。

- [x] **Step 1:** 参数扩展：`--rerank`（对每条 query 以 rerank=true 跑第二遍）、`--scope N`（以 `_owner_scope=N` 跑，golden 条目可标 `"visibility": "private", "owner_id": N`）、`--pagination`（每条 query 取 skip=0/10/20 三页，断言并集无重复无遗漏、页间 rank 不回跳）。
- [x] **Step 2:** golden 集从 32 条扩到 60+：新增长 chunk 用例（条款在 200 字后，守 Task 1.3）、跨块条款用例（期望命中在邻块，守 Task 1.4）、新旧文档混排用例（守 Task 1.2）、私有块 scope 用例（守 Task 0.1，断言私有块仅属主可见）。
- [x] **Step 3:** 门槛写入脚本输出摘要并按惯例存档到本文档同目录：**recall@10 ≥ 0.9688（现基线，不得回退）**；rerank 模式各 query 返回条数 ≥ 词法模式（守 Task 0.2）；scope 用例通过率 100%；分页一致性 100%。
- [x] **Step 4:** rerank 覆盖率样本回放：构造模型只回部分编号的 mock（单测已覆盖），eval 层以 `--rerank` 实测 `rerank_partial` 比例，输出到摘要表（可观测，不设门槛）。

---

## 提交切分（约定式，逐 Task 一提交）

```
fix(security): per-session tool registry clone to isolate owner scope        # 0.1
fix(search): append unranked remainder in LLM rerank                          # 0.2
fix(search): unified fetch window for hybrid pagination + total_mode          # 0.3
perf(search): parallel embedding and lexical retrieval arms                   # 1.1
feat(search): client-side time decay in RRF fusion                            # 1.2
feat(search): adaptive rerank candidate window with segment localization      # 1.3
feat(search): read_chunks tool for coordinate-based full chunk retrieval      # 1.4
feat(loop): citation table carries rid/chunk coordinates                      # 2.1
refactor(prompts): single-source citation rules, drop dead plugin.yaml caps   # 2.2
fix(search): align max limit with citation cap at 50                          # 2.3
feat(prompts): preserve citation tables across context compaction             # 2.4
feat(search): explicit rank field on search hits                              # 2.5
chore(search): env-tunable retrieval knobs                                    # 3.1
feat(plugin): cache invalidation broadcast on reindex                         # 3.2
test(search): eval modes for scope/rerank/pagination + golden set growth      # 3.3
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；新增单测覆盖上表每个 Task 的验收项。
2. `uv run python scripts/retrieval_eval.py` 对照存档基线：recall@10 ≥ 0.9688，MRR/NDCG 不回退；`--rerank`/`--scope`/`--pagination` 模式全过。
3. 集成测试（标记 integration，需 ES/LLM 环境）：并发会话 scope 隔离、搜索→持久化→read_chunks 回读两条端到端链路。
4. 行为核验：两个不同 owner 的会话并发搜索，各自仅见 public+自己私有块（Task 0.1 的最终验收以人工/集成双确认）。

## 明确不做的事

- 不动索引 analyzer/mapping 与分词（属上轮计划 Phase 0/1 范畴，需 reindex，另行立项）。
- 不引入 ES `rank.rrf`（商业许可），融合维持客户端 RRF。
- 不改前端 citations 协议（`api/models.py` 的 `CitationHit` 结构与 SSE 事件不变）。
- 不改词法查询语义（bigram + AND/MSM + 短语信号 + 同义扩展 + tie-break 排序全部保持）。
- `context_recent_tool_results_tokens` 等上下文预算默认值不动（留部署调优）。

---

## 实施记录（2026-08-16）

全部 15 个 Task 已按提交切分完成并逐项提交。与原计划的偏差与实测数据：

1. **Task 3.3 golden 集规模**：原计划 32→60+，实际 16→21。语料库仅 14 个资源（132 块），
   每条 golden 需按真实语料核实期望命中，60+ 需要先扩充语料；时间衰减用例因语料
   无 `publish_date` 数据暂无法构造（部署补充日期数据后追加）。
2. **Task 1.3 导入隔离**：计划草稿中 rerank.py 顶层 `from tools import _parse_query`
   在共享进程测试运行下会被其他插件的同名 `tools` 模块遮蔽（tests/plugin/conftest.py
   有说明），改为 rerank 自带短语提取、保持零插件内依赖；entry.py 的 `_cache_clear`
   改为模块级绑定（同一原因）。
3. **评测基线（本地 ES，21 条 golden，词法模式）**：
   `recall@10=0.9762 / MRR=1.0 / NDCG@10=2.6175`，高于上一轮 0.9688 门槛；
   三门槛全过：`--rerank`（0 条数回退；**rerank_partial 率 0.90**——部署端点 90% 的
   重排返回部分编号，印证 Task 0.2 回填修复的必要性）、`--scope 7`
   （scoped 0.9286 vs unscoped 0.9762，容差内）、`--pagination`（21 查询 0 违规）。
4. **Task 1.4 集成测试**：`tests/plugin/test_search_read_chunks_integration.py`
   （integration 标记），搜索→坐标→read_chunks 全文回读链路已验证。
