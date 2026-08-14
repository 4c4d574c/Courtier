# search_documents 检索升级为 RAG 级检索器 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [ ]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 把 `search_documents` 从"纯词法 BM25 检索"升级为 RAG 级检索组件：先修复当前中文检索的精度硬伤（P0），再引入向量混合检索与重排（P1），最后补齐评测、缓存、时间衰减等工程化能力（P2）。全程保持插件/宿主架构边界与引用标注机制不变。

**Architecture:** 搜索链路保持现状分层：`resource_service.py`（ingest）→ `courtier/es/client.py`（索引）→ `plugins/shared/search/`（查询插件）→ 宿主（`ScopedTool` 权限注入、`loop.py` 引用编号）→ 前端 citations。本方案在此链路上做四类改动：索引版本化与迁移工具（Phase 0）、词法查询语义与中文分词（Phase 1）、向量+重排（Phase 2）、评测与工程化（Phase 3）。

**Tech Stack:** Python 3.12+, Elasticsearch 8.19.6（无插件，原生支持 dense_vector/knn/RRF）、uv、httpx、pytest。

**关联文档：** `docs/superpowers/specs/2026-05-22-search-tool-improvement-design.md`（查询接口设计 spec，本计划沿用其参数契约）。

---

## 背景与现状问题（实测证据）

当前 `search_documents`（`plugins/shared/search/tools.py`）是单阶段 ES 查询：引号内文本 → `match_phrase`（slop 0，仅 `fields[0]`）；自由文本 → `multi_match`（`best_fields`，默认 OR 语义，≤4 字符加 `fuzziness: AUTO`）；filter 侧支持 `document_id`/`doc_type`/`tags`/可见性 scope。索引映射（`courtier/es/client.py:12-41`）对 `chunk_text`/`title` 使用默认 `standard` analyzer。

本地 ES（`courtier_chunks`，132 块）实测：

| 检查项 | 结果 |
|--------|------|
| `_analyze` "各单位要落实安全生产主体责任" | 单字切分：各/单/位/要/落/实/…（standard 分词器把中文按字切） |
| 搜「安全生产主体责任」（8 字） | 命中 **132/132**（全库），top1 不含完整短语 |
| top 命中得分 | 大量并列 4.80（排序不确定） |

根因：(1) 中文被切成单字后 `multi_match` 的 OR 语义使任意单字命中即召回，多字查询≈全库召回；(2) 无短语主信号、无字段权重、无 tie-break 排序。作为 RAG 的 R 端，精度≈随机。

**目标形态：** 引号短语精确匹配 + 分词级 BM25（bigram）加权检索 + 可选向量双路 RRF 融合 + 可选 LLM 重排 + 邻块上下文 + 确定性排序，全过程保持可见性 scope 过滤与 `[[n]]` 引用编号机制。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/es/client.py` | 索引映射（analysis 设置、dense_vector）、init_index alias 化、写索引名解析 | 修改 | 0/1/2 |
| `scripts/reindex_chunks.py` | 版本化重建 chunks 索引、原子切 alias、回滚 | **新建** | 0 |
| `plugins/shared/search/tools.py` | 查询语义修正、混合检索/RRF、邻块扩展、重排、缓存、时间衰减 | 修改 | 1/2/3 |
| `plugins/shared/search/es_client.py` | search 调用透传（body 已含 knn/rank，结构无需变） | 修改（微调） | 2 |
| `plugins/shared/search/embeddings.py` | 插件侧 embedding 客户端（查询向量化） | **新建** | 2 |
| `plugins/shared/search/rerank.py` | LLM listwise 重排 | **新建** | 2 |
| `plugins/shared/search/plugin.yaml` | runtime.env 增补 `LLM_EMBEDDING_NAME` 等；system_prompt 增补查询改写指引 | 修改 | 2 |
| `plugins/shared/search/pyproject.toml` | 增补 `httpx` 依赖 | 修改 | 2 |
| `courtier/plugin/manager.py` | `_ALLOWED_MANIFEST_ENV_VARS`、`_SETTINGS_ENV_FALLBACK` 增补 embedding 变量 | 修改 | 2 |
| `courtier/config.py` | 新增 embedding/重排/衰减相关 Settings | 修改 | 2/3 |
| `courtier/agent/api/services/resource_service.py` | 入库时批量向量化（失败降级）、切块重叠窗口 | 修改 | 1/2 |
| `courtier/prompts/defaults/zh-CN/behavioral.yaml` | 增补"多查询变体改写"指引 | 修改 | 2 |
| `courtier/agent/core/loop.py` | 引用 payload 透出邻块（可选）、tool_span 检索指标 | 修改 | 1/3 |
| `courtier/agent/telemetry/tracer.py` | 检索属性记录辅助 | 修改 | 3 |
| `scripts/retrieval_eval.py` | 离线检索评测（recall@k/MRR/NDCG） | **新建** | 3 |
| `scripts/retrieval_golden.jsonl` | 黄金查询集（公文查询 → 期望 resource_id/命中块） | **新建** | 3 |
| `tests/plugin/test_search_plugin.py` | `_build_es_query` 语义/权重/排序/邻块单元测试 | 修改 | 1/2 |
| `tests/agent/test_loop_citations.py` | 邻块透出后的引用 payload 兼容测试 | 修改 | 1 |

> 宿主侧结果流约束（不变）：`ResultSummarizer` 内联阈值 1500 字符、超限强制持久化并由 `loop.py` 生成 `【引用编号 N】` 摘要表；`_CITATION_MAX_HITS=50`。新增字段（`_score`/`neighbors`）只影响给模型的 payload 与 `_INCLUDE_FIELDS`，前端 `CitationHit` 字段全可选，不破坏。

---

## Phase 0：索引迁移基础设施（前置）

无此阶段无法安全落地后续任何 mapping 变更（当前 `init_index` 仅 create-if-missing，`courtier/es/client.py:66-73`；仓库无任何 reindex 工具，MySQL 无 chunk 文本，MinIO 原件是唯一重建源）。

### Task 0.1: 索引版本化 + alias

**Files:** `courtier/es/client.py`

- [ ] **Step 1:** `INDEX_MAPPING` 增加 `settings.analysis`（见 Phase 1 Task 1.2，本阶段先只加结构占位或直接随 1.2 落地）；定义真实索引命名 `{es_index_chunks}_v{n}`，`ES_INDEX_CHUNKS` 保持指向 alias 名（默认 `courtier_chunks`，配置语义不变）。
- [ ] **Step 2:** `init_index()` 改为：alias 存在 → 校验指向真实索引、no-op；不存在 → 创建 `{alias}_v1` + `PUT {alias}_v1/_alias/{alias}`（原子挂载）。首次部署即走 alias，存量环境由 Task 0.3 脚本完成迁入。
- [ ] **Step 3:** 新增 `_write_index_name()`：`GET {alias}/_alias` 解析当前真实索引名，进程内 60s TTL 缓存；`bulk_index_chunks`/`update_chunk`/`delete_by_*` 的写路径改走该函数（读路径经 alias 天然路由）。切 alias 后无需重启应用，最多 60s 内写请求落在旧索引——重灌期间以 `--check-only` 把关，切 alias 前必须完成全量灌入。

### Task 0.2: reindex 脚本

**Files:** `scripts/reindex_chunks.py`（新建）

- [ ] **Step 1:** CLI（argparse）：`--target-version v2`（缺省自动取当前版本+1）、`--check-only`（只校验不灌入）、`--swap-to <v>`（原子切 alias）、`--rollback`（切回上一版本）、`--keep-old`（保留旧索引）。
- [ ] **Step 2:** 数据源：MySQL `resources` 表（复用 `resource_service` 的 repo/模型）逐行读取；MinIO bucket `courtier-resources` 按 `{md5}/{filename}` 下载原件到临时目录。
- [ ] **Step 3:** 重建逻辑直接复用 `resource_service._extract_text` / `_split_chunks`（避免双实现漂移，脚本以 `sys.path` 引入 app 包）；Phase 2 上线后同步调用向量化。
- [ ] **Step 4:** 校验：逐 resource 比对 `chunk_count`/`char_count` 与 MySQL 行一致；全量块数与总数核对；MinIO 原件缺失的 resource 记录 gap 清单（upload 是 best-effort，`resource_service.py:155-165`）。
- [ ] **Step 5:** `--swap-to`：`_aliases` 单次原子操作 `{add: {index: vN, alias}, remove: {index: vM, alias}}`，随后可选删旧索引；`--rollback` 反方向操作。写入 `scripts/` 的独立 uv 运行说明（`uv run python scripts/reindex_chunks.py ...`，依赖根环境）。

**验收：** 本地 132 块环境跑通 `--target-version v2` + `--swap-to v2` + `--rollback` 全流程，块数与 char_count 校验通过，读写正常。

---

## Phase 1（P0）：词法检索修复

### Task 1.1: 查询语义修正（先于 reindex，立即可用）

**Files:** `plugins/shared/search/tools.py`（`_build_es_query`）

- [ ] **Step 1:** 自由文本 `multi_match` 增加 `operator: "and"`；当自由文本按空白切分后词数 ≥ 4 时改用 `minimum_should_match: "70%"`（防止长查询 AND 过严零召回）。单字查询（≤1 字符分词）维持 `fuzziness: AUTO` 现有逻辑。
- [ ] **Step 2:** 短语主信号：自由文本整体额外生成一条 `match_phrase`（`chunk_text`，`slop: 2`）放入 `bool.should`（boost 2.0），`multi_match` 保留在 `must`——有短语命中的文档排序显著靠前，无短语命中仍可召回。
- [ ] **Step 3:** 确定性排序：查询体增加 `"sort": [{"_score": "desc"}, {"publish_date": {"order": "desc", "unmapped_type": "date"}}, {"_id": "asc"}]`；`_clean_response` 每个 hit 透出 `_score`（`hit.get("_score")`），`_INCLUDE_FIELDS` 不涉及（`_score` 单独取）。
- [ ] **Step 4:** 字段权重：`multi_match` 的 `fields` 改为 `["chunk_text^1", "title^3"]`（用户未传 `search_fields` 时）。

**验收数据（本地对照）：** 同款 8 字查询「安全生产主体责任」在 132 块库上：命中数显著下降、top10 全部包含完整短语或短语主信号命中、`_score` 无并列。

### Task 1.2: CJK bigram 分词（需 reindex）

**Files:** `courtier/es/client.py`（mapping），经 Task 0.2 生效

- [ ] **Step 1:** `chunk_text`/`title` 改用内置 **`cjk` analyzer**（实施时修正：ES 8.19 无独立 `cjk` tokenizer、`cjk_bigram` filter 对 standard 单字 token 不生效，故不自定义 analyzer，直接 `{"type": "text", "analyzer": "cjk"}`；实测产出重叠 bigram）。
  - **同义词改为查询端展开**（实施时修正：bigram 分词下 ES token 级 synonym filter 无法匹配多字中文同义词）：`plugins/shared/search/tools.py` 的 `SYNONYM_MAP` 把命中词的同义等价词（安监局↔安全生产监督管理局、通知/印发/转发、办法/规定、批复/复函 等）作为 `should` 短语加权子句加入查询——扩大召回但不强制命中；词表维护零 reindex/零索引 close。
- [ ] **Step 2:** 索引与查询同构（同一 analyzer），无需 search_analyzer。
- [ ] **Step 3:** 查询端无需改 analyzer 引用（match/multi_match 自动用字段 search_analyzer）；确认 `match_phrase` 在 bigram 上语义为"有序相邻 bigram 序列"（子串匹配），`slop` 单位变为 bigram 步数，Task 1.1 的 slop 取值在 reindex 后需回归验证（slop 2 的意图 = 允许 2 词位移）。
- [ ] **Step 4:** 同步按 Task 0.2 runbook 重建索引；`standard`→bigram 后索引体积约增 50-100%（中文文本字段），132 块量级无感，生产量级在 runbook 中评估。

### Task 1.3: 邻块上下文扩展

**Files:** `plugins/shared/search/tools.py`、`courtier/agent/core/loop.py`（透出）

- [ ] **Step 1:** `execute` 主查询返回后，对 top hits（`limit` 条）收集 `(resource_id, chunk_no)`，发第二条 ES 查询：`bool.should` 每个 hit 一条 `{term: resource_id} + {range: chunk_no: [n-2, n+2]}`，`size = limit * 4`；命中按 `(resource_id, chunk_no)` 映射回主 hit 的 `neighbors`（剔除自身，每条截 300 字符，含 `chunk_no`/`title`）。
- [ ] **Step 2:** 邻块查询同样带可见性 filter（与主查询同 scope）；邻块查询失败仅记日志、返回空 `neighbors`（不失败整个调用）。
- [ ] **Step 3:** `loop.py` `_build_citations_payload` 保持现有字段（前端契约不动）；`neighbors` 仅进入模型侧 payload。若后续要在引用卡片展示邻块，另行加字段（前端可选字段，兼容）。

**Files:** `courtier/agent/api/services/resource_service.py`

- [ ] **Step 4:** `_split_chunks` 增加重叠窗口（`overlap=100` 字符、段边界保持），并写入 `paragraph_index = chunk_no`（当前不写该字段，搜索/引用侧多处引用它；正式的结构化切块依赖 docparse 结构信息，不在本期）。

### Task 1.4: 测试

- [ ] 扩充 `tests/plugin/test_search_plugin.py`（沿用 `tests/agent/api/test_search_scope.py` 加载插件模块、直接断言 `_build_es_query` 返回体的模式）：OR→AND/minimum_should_match、短语主信号、sort tie-break、title boost、邻块二级查询体、scope filter 在邻块查询中的存在性。
- [ ] `tests/agent/test_loop_citations.py` 增加：带 `neighbors` 的 hit 不破坏 citation payload。

**Phase 1 DoD：** 132 块实测 top10 短语覆盖、无得分并列；bigram reindex 全流程可回滚；邻块返回正确；`uv run pytest tests/plugin/test_search_plugin.py tests/agent/test_loop_citations.py tests/agent/api/test_search_scope.py` 全绿。

---

## Phase 2（P1）：语义检索（复用现有 LLM 端点）

### Task 2.1: 向量基础设施

**Files:** `courtier/config.py`、`courtier/plugin/manager.py`

- [ ] **Step 1:** `config.py` 新增：`llm_embedding_model: str = ""`（env `LLM_EMBEDDING_NAME`，空 = 向量检索关闭）、`llm_embedding_dim: int = 1024`（env `LLM_EMBEDDING_DIM`）、`llm_embedding_batch_size: int = 25`、`search_time_decay_enabled: bool = False`（Phase 3 用）。
- [ ] **Step 2:** `manager.py` `_ALLOWED_MANIFEST_ENV_VARS` 增补 `LLM_EMBEDDING_NAME`/`LLM_EMBEDDING_DIM`，`_SETTINGS_ENV_FALLBACK` 增补对应映射（复用 `LLM_IP`/`LLM_API_KEY` 既有白名单，`manager.py:99-101`）。

**Files:** `plugins/shared/search/embeddings.py`（新建）、`plugin.yaml`、`pyproject.toml`

- [ ] **Step 3:** `embeddings.py`：httpx AsyncClient POST `{LLM_IP}/embeddings`（OpenAI-compatible），入参 `{model, input: list[str]}`，返回 `list[list[float]]`；30s 超时、1 次重试；`embed(texts) -> list[vec]`（内部按 `LLM_EMBEDDING_BATCH_SIZE` 分批）。环境变量缺失或模型为空时抛 `EmbeddingUnavailable`，调用方降级。
- [ ] **Step 4:** `plugin.yaml` `runtime.env` 增补 `LLM_EMBEDDING_NAME`/`LLM_EMBEDDING_DIM`（`${ENV:VAR}` 通道）；`pyproject.toml` 依赖增 `httpx>=0.27`（Docker 镜像根环境已含 httpx——`openai` 后端依赖引入，实施时验证 `uv.lock`；根 `pyproject.toml` 无需改动）。

### Task 2.2: 入库向量化

**Files:** `courtier/agent/api/services/resource_service.py`

- [ ] **Step 1:** 建 chunk 后按批调用 embedding（复用与插件同构的客户端逻辑，宿主侧新建 `courtier/es/embeddings.py` 或复用 `httpx` 直连，避免与插件代码互依赖）；向量写入 chunk body `chunk_vector`。
- [ ] **Step 2:** 失败降级：embedding 不可用/超时 → chunk 不含 `chunk_vector` 字段照常入库（词法可用），ingest 不失败、记 warning；重试 1 次。
- [ ] **Step 3:** 不阻塞 ingest 主链路：embedding 与 bulk 分段并行（asyncio.gather，控制并发上限）。

### Task 2.3: mapping 增 dense_vector

- [ ] `INDEX_MAPPING` 增 `chunk_vector: {"type": "dense_vector", "dims": 1024, "index": true, "similarity": "cosine"}`（dims 取 `llm_embedding_dim`；旧数据无此字段的文档 kNN 自然跳过，RRF 只融词法路）。随 Task 1.2 同一次 reindex 生效（Phase 0 脚本已具备）。

### Task 2.4: 混合检索（BM25 + kNN + RRF）

**Files:** `plugins/shared/search/tools.py`

- [ ] **Step 1:** `_build_es_query` 增加可选 `query_vector` 参数；`execute` 在构建前调用 `embeddings.embed([query])`，失败/未配置则 `query_vector=None`（纯词法路径）。
- [ ] **Step 2:** query_vector 存在时组装 hybrid：**客户端 RRF 融合**（实施时修正：本地 ES 为 basic license，`rank.rrf` 报 `non-compliant for [Reciprocal Rank Fusion (RRF)]`，故改为词法查询 + 独立 kNN 查询（同 filter）在插件内做 reciprocal rank fusion）；kNN 臂失败自动降级纯词法。

- [ ] **Step 3:** 响应透出融合后 `_score`（RRF 分数）与结果级 `mode` 字段（`hybrid`/`lexical`，实施时修正：逐命中 `matched_by` 近似标注信息量低，改为结果级模式标注）；kNN filter 必须与词法 filter 同 scope（权限正确性关键点，测试覆盖）。

### Task 2.5: LLM listwise 重排

**Files:** `plugins/shared/search/rerank.py`（新建）、`tools.py`

- [ ] **Step 1:** 工具参数增 `rerank: boolean`（默认 `false`，v1 仅显式开启）。
- [ ] **Step 2:** `rerank=true` 时：内部按 `size=50` 取粗排（ES 侧，走 RRF 或词法），将编号命中的 `title + chunk_text[:200]` 组装 listwise prompt（system：按与查询相关性输出序号数组 JSON；temperature 0），调用 `{LLM_IP}` chat completions（`LLM_NAME` 模型，env 白名单已具备）；解析失败/超时/空输出 → 原序降级并记 warning；精排后截取 `limit` 返回。
- [ ] **Step 3:** 引用编号与重排顺序一致（重排后的顺序即返回 hits 顺序，citation 机制无需改动）；`rerank=true` 时不走 Phase 3 的结果缓存（LLM 非确定性）。

### Task 2.6: 查询改写（纯 prompt 层）

**Files:** `plugins/shared/search/plugin.yaml`（system_prompt）、`courtier/prompts/defaults/zh-CN/behavioral.yaml`

- [ ] **Step 1:** 两处增补指引："检索前把用户问题改写为 1~3 个公文术语化查询（术语全称/简称、近义表述各一条），分别调用 `search_documents` 交叉验证；多轮调用时引用编号跨调用连续累计（既有规则）。"
- [ ] **Step 2:** 保持指引为建议性（模型自主决定），不新增工具参数；改写效果由 Phase 3 评测集间接验证。

### Task 2.7: 测试

- [ ] hybrid 查询体断言（knn filter 与可见性 scope 一致）、`query_vector=None` 降级路径、embedding 失败降级、rerank 解析失败降级、rerank 截断/顺序、引用 payload 与重排顺序一致性。
- [ ] embedding 客户端与 rerank 用 mock HTTP（respx 或 monkeypatch httpx），不真调 LLM。

**Phase 2 DoD：** 混合检索在含向量与无向量混合数据上均可用；rerank 失败不劣化；单测全绿；查询改写指引生效（人工抽验）。

---

## Phase 3（P2）：工程化

### Task 3.1: 检索评测

**Files:** `scripts/retrieval_eval.py`、`scripts/retrieval_golden.jsonl`（新建）

- [ ] **Step 1:** 黄金查询集格式（每行 JSON）：`{"query": "安全生产主体责任由谁落实", "doc_type": null, "expected_resource_ids": [...], "expected_chunk_substrings": ["主要负责人"]}`；首版 ≥ 30 条覆盖公文典型问法（含简称/全称、口语化改写、跨文档问题）。
- [ ] **Step 2:** `retrieval_eval.py`（对标 `courtier/courtier/benchmarks/long_session_memory_benchmark.py` 的 argparse+asyncio 独立脚本风格）：按环境连 ES（读 `.env`），对每条 query 跑 `_build_es_query`+`search_chunks`，计算 **recall@10**、**MRR**、**NDCG@10**；`--compare` 支持新旧查询体 A/B 输出对照表；基线数据（升级前 query 语义）写入文档存档。
- [ ] **Step 3:** 输出 JSON 报告 + 摘要表；作为每次检索改动的回归门槛（recall@10 不得低于基线）。

### Task 3.2: 查询结果缓存

**Files:** `plugins/shared/search/tools.py`

- [ ] **Step 1:** 插件进程内 TTL LRU（`OrderedDict` + 锁，无新依赖）：key = `sha256(owner_scope|query|doc_type|tags|search_fields|skip|limit)`，TTL 120s，容量 256；只缓存**重排前**的粗排结果（rerank 每次重算），`total/took_ms` 一并缓存并在响应中标注 `cached: true`。
- [ ] **Step 2:** 缓存未命中才查 ES/embedding；命中后 rerank（如请求）与邻块扩展照常执行。引用编号机制与缓存正交，无需改动。

### Task 3.3: 时间衰减

**Files:** `plugins/shared/search/tools.py`、`courtier/config.py`

- [ ] **Step 1:** 工具参数增 `use_time_decay: boolean`（默认 `false`）；开启时词法查询包 `function_score`（`gauss: publish_date`，`scale: 730d`，`decay: 0.5`），新规优先。
- [ ] **Step 2:** 与 RRF 组合时衰减作用于词法路分数（kNN 路不加权，保持简单）；测试断言 function_score 包裹结构。

### Task 3.4: 可观测

**Files:** `courtier/agent/core/loop.py`、`courtier/agent/telemetry/tracer.py`

- [ ] **Step 1:** `tool_span`（`loop.py:752` 附近）为 `search_documents` 增属性：`gen_ai.tool.hits`（命中数）、`retrieval.mode`（`lexical`/`hybrid`）、`retrieval.rerank`（bool）、`retrieval.cached`（bool）——走既有 OTel → Langfuse 链路。
- [ ] **Step 2:** `telemetry/metrics.py` 增加检索时延 histogram（可选，随 Prometheus 现有模式）。

**Phase 3 DoD：** 评测脚本跑通并产出基线报告；缓存命中可观测（took_ms/cached 标注）；衰减开关有效；单测全绿。

---

## 迁移与上线 Runbook

按顺序执行（生产）：

1. **预检**：确认 MinIO 原件覆盖率（`--check-only` 产出 gap 清单）；估算 bigram 索引体积。
2. **建 v2 回灌**（不影响线上）：`uv run python scripts/reindex_chunks.py --target-version v2`（Phase 2 上线后自动含向量化）。
3. **校验**：逐 resource 块数/字符数核对；对 v2 直接跑 Phase 1/3 验收查询与 `retrieval_eval.py`。
4. **冒烟**：`GET {alias}_v2/_count`、临时脚本直查 v2 验证 top10 短语覆盖。
5. **原子切换**：`uv run python scripts/reindex_chunks.py --swap-to v2`（`_aliases` 单操作原子切换，读路径即时生效；写路径经 60s TTL 缓存切换，切换后观察一个 TTL 周期）。
6. **回滚**：`--rollback` 切回 v1；确认无异常后 `--keep-old` 之外的旧索引删除。
7. **无向量旧数据**：kNN 自然跳过、RRF 仅词法路；随 Phase 2 重新 reindex 一次补齐向量（或接受混合降级）。

## 风险与取舍

| 风险/取舍 | 说明 | 缓解 |
|-----------|------|------|
| bigram 索引膨胀 | 中文字段 token 数大幅上升 | 生产先 `--check-only` 评估体积；length filter 2-16 控上限 |
| 同义词 filter 更新需 close/open 索引 | search-time synonym 仍需短暂索引维护 | 词表走常量维护、变更低频；bigram 已兜底简称召回 |
| embedding 费用/时延 | 每 resource 全量向量化一次 + 每次查询 1 次 | 批量调用、失败降级词法、查询缓存 |
| LLM 重排时延 | 每次开启多 1~3s 一次 LLM 调用 | 默认关闭、显式开启、超时降级 |
| 元数据缺失 | `resource_service` 不写 `paragraph_index`/`document_id`/`source_id`（legacy 字段），正式结构化切块依赖 docparse（未接入 resource 链路） | 本期 `paragraph_index=chunk_no` 占位、邻块用 `chunk_no`；章/条级语义切块列为后续项 |
| 权限正确性 | kNN filter 遗漏 scope 会越权召回 | 测试断言 knn filter 与词法 filter 同 scope |
| 切 alias 窗口 | 写路径 60s TTL 内可能落旧索引 | 重灌完成后再切、切换后观察一周期；极端场景接受 60s 静默窗口 |

## 开放问题

- Embedding 模型名与维度需按实际 DashScope 账号确认（候选 `text-embedding-v3`，1024 维；确认后再定 `llm_embedding_dim` 默认值）。
- `rerank` 与 `use_time_decay` 是否后续改为全局默认开启（待评测集数据支撑）。
- 评测集黄金数据规模与维护方式（是否纳入 CI 离线断言）。
- 生产 ES 是否有其他插件/索引占用（`courtier_results` 独立不受影响，已确认）。

## 验收标准汇总

- **Phase 0**：本地跑通 v2 建库 → 切换 → 回滚全流程，块数/字符数校验通过。
- **Phase 1**：8 字查询 top10 短语覆盖、全库命中率显著下降、`_score` 无并列；bigram reindex 生效；邻块返回正确；相关单测全绿。
- **Phase 2**：hybrid 在混合数据上可用、可见性过滤不泄漏；rerank 失败不劣化；embedding 不可用时自动降级词法。
- **Phase 3**：评测基线报告产出且后续改动回归不劣化；缓存/衰减可观测；单测全绿。
- **全局**：`[[n]]` 引用编号机制（跨调用连续编号、`[[0]]` 禁止）全程不回归，`tests/agent/test_loop_citations.py` 保持全绿。
