# 插件去 LLM 化（结构识别纯规则 + 检索 glue 收归 host）实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 插件进程零 LLM 端点与密钥。parse（docparse）删除结构识别与字体识别的两条 LLM 路径——规则引擎与 ResNet 专用模型承担全部工作；search 的查询向量化和 LLM 重排收归 host 侧（派发前注入查询向量、返回后用主模型重排）。所有 LLM 调用经 host 模型客户端发出，`plugins/plugin.env` 及插件容器 env 中 `LLM_*` 全部删除。

**Architecture:** 改造后插件只含确定性计算与专用服务调用（PPOCR OCR、ResNet 字体模型、ES 检索、MinIO transfer）。host 侧新增两个"glue"能力：① **host 注入参数机制**——工具 input schema 中标记为 host-injected 的参数（`query_embedding`）由 host 在 `ToolRegistry.execute` 派发前计算注入（与 file-ref 边界改写同缝的镜像机制），LLM 不可见不可填；② **检索重排后处理**——search 插件返回候选后、结果持久化之前，host 用主模型做一次 listwise 重排（提示词进核心模板，遵循"core 无硬编码 NL"规则）。embedding 客户端由此单点化：索引（现状已 host 侧）与查询（本计划迁入）同源，根治两侧配置分裂哑雷。

**Tech Stack:** pytest、pydantic（工具契约）、httpx/OpenAI 兼容客户端（host 侧复用现有）、Jinja2 PromptEngine（重排提示词模板）。

**关联文档:** `2026-08-17-plugin-standalone-deployment-implementation.md`（本计划修订其 env 边界：plugin.env.example 删 LLM_*）；`2026-08-27-db-backed-settings-implementation.md`（配置中心化计划，**排序在本计划之后**，完成后需小修订：B 类清单缩小、删 embedding 命名陷阱条目、检索组并入新增的 rerank 配置）。

---

## 决策记录（已与用户确认，2026-08-27）

| # | 决策点 | 结论 | 被否方案 |
|---|--------|------|----------|
| 1 | parse 的两个 LLM 调用点 | **直接取消 LLM 路径**：结构识别只留规则引擎（等价现 `rule_only`），字体识别只走 ResNet 专用模型；`FONT_MODEL_*` 等 env 保留插件侧。质量后果知情接受：低置信页纯规则输出 + warning；ResNet 未识别行无字体信息（配 `FONT_MODEL_URL` 缓解） | 反向 RPC `llm.chat` host service（管线不动仅换传输）；上移子代理循环（字面 Agent 调用，需 host 多模态改造，与会话式循环错配） |
| 2 | search 的 LLM 调用点 | **host 侧 glue**（用户确认）：query 向量 host 派发前计算并注入隐藏参数 `query_embedding`；插件返回后 host 用主模型重排。插件退化为纯 ES 检索原语，降级语义保留 | 插件内改走 llm.chat 反向 RPC；砍掉 LLM 重排只留 RRF |
| 3 | host 模型配置 | **共用主模型**，不新增 utility/vision 模型槽（重排 = 主聊天模型；embedding 沿用现有 `llm_embedding_*` 配置组） | 独立 utility 模型槽（默认=主模型） |
| 4 | 计划顺序 | **本改造先行**；完成后同步修订插件独立化与配置中心化两份计划的对应段落 | 并入设置计划 / 设置计划先行 |

**重要推论（记录在案）：** 决策 1 + 3 组合下，反向 RPC `llm.*` host service **整个不需要建设**；host 侧唯一新增 LLM 出口是检索 glue（embedding 复用 `es/embeddings.py` 客户端 + 重排走主模型客户端，后者自动纳入 OTel/Langfuse 追踪——今天插件内 LLM 调用对 host 可观测层完全不可见）。

背景盘点结论（2026-08-27 两轮探索）：8 个插件中仅 parse（经 docparse）与 search 实际调 LLM，共 4 个调用点——结构识别 `llm_client.py:311`（视觉+文本，逐页 1 次、并发 4、120s 超时 4 重试）、字体识别 `llm_client.py:455`（视觉裁剪拼图）、查询向量化 `embeddings.py:74`（每次检索 1 次）、listwise 重排 `rerank.py:176`（可选 1 次）。anydoc（PPOCR 远程 OCR）、annotate、template、check_format、check_content（纯正则）、detect_plagiarism（纯算法）零 LLM，不动。

---

## 目标设计

### 1. parse / docparse 删除面（`libs/docaudit/docparse/` + `plugins/docaudit/parse/`）

**代码删除：**
- `parsers/llm_client.py` 整文件（结构识别调用 :310-334、字体识别调用 :454-478、页面图编码 `_encode_page_image` :169-189、行裁剪拼图 :379-432、两段系统提示词 :22-148）。
- `parsers/_retry.py`（仅服务 LLM 调用；删前确认无其它消费者）。
- `classify_mode` 三态：`base.py:84` 的 env 读取、`scanned/__init__.py:433-469` 分支、`_rules_acceptable`（:58-75）、逐页 LLM 失败回退（:480-502）→ `StructureRuleEngine.classify_lines` 成为唯一结构分类器。
- 字体 LLM 兜底链：`scanned/__init__.py:311-372` 的 LLM fallback、:328-340 的"模型失败全量回退 LLM"→ ResNet 唯一；`FONT_MODEL_URL` 未配置或行未识别 → 该行无字体信息 + warning（不再有任何兜底）。
- LLM 结果 LRU 缓存（若存在）一并删除。

**分发与守卫改写：**
- 混合 PDF 分发条件（`registry.py:118,186` 按 `llm_api_key` 是否存在决定无文本页走不走扫描解析）→ 无条件对无文本页走 ScannedParser（规则版）。
- `ScannedParser` 缺 `LLM_API_KEY` 的 fail-fast（`scanned/__init__.py:177-178`）删除。

**env 处置（docparse 消费侧 `base.py:67-85`）：**

| 处置 | 变量 |
|------|------|
| 删除 | `LLM_IP`、`LLM_API_KEY`、`LLM_NAME`、`DOCPARSE_CLASSIFY_MODE`、`DOCPARSE_MAX_LLM_CONCURRENT`、`DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE` |
| 保留（专用服务，非 LLM） | `DOCPARSE_OCR_API_URL/_LANG/_ENGINE/_MAX_IMAGE_LONG_SIDE/_DESKEW`、`DOCPARSE_MAX_OCR_CONCURRENT`、`FONT_MODEL_URL/_CONF_THRESHOLD/_MARGIN_THRESHOLD`（用户指定保留） |

### 2. search 查询向量化收归 host（host 注入参数机制）

**契约**：search 工具 input schema 新增 `query_embedding: array<number>`，属性标记 `"x-host-injected": "embedding"`（通用机制：标记值 = 注入器类型；embedding 是平台级基础设施，注入器放 core）。两个消费点：① 工具 schema 暴露给 LLM 时剔除该参数（agent 契约零变化）；② `ToolRegistry.execute`（`courtier/agent/tools/registry.py:352-374`，$ref 解析后、`tool.execute` 前——与 file-ref 边界改写 `proxies.py:86-151` 同缝的镜像机制）发现标记后执行注入器。

**注入器逻辑**：工具契约含 embedding 标记参数 + `query` 参数有值 + `settings.llm_embedding_model` 非空 → host 调 embedding 客户端计算向量 → 写入 kwargs。插件契约无此标记（旧版本插件）→ 不注入，行为不变（向后兼容）。

**host embedding 客户端**：复用 `courtier/es/embeddings.py`（索引时已用它），新增单条入口 `embed_query(text)`（现有 `embed_chunks` 是批量）。失败或未配置 → 不注入 → 插件降级纯词法（现行为）。

**插件侧（`plugins/shared/search/`）**：
- 删 `embeddings.py` 整文件（`LLM_IP` httpx 客户端与配置 :26-35、:67-86）。
- `tools.py:790-805`：`embed_query` 调用改为读注入的 `query_embedding` 参数；无注入 → 跳过 kNN 臂走纯词法（现降级路径 `:800-805` 不变）。kNN+RRF 融合逻辑（`:813-876`）不动。
- `LLM_EMBEDDING_DIM` 插件侧校验删除（维度权威在 host——ES mapping 由 host `llm_embedding_dim` 决定）。

**时序代价（记录在案）**：现状词法检索与 embedding 在插件内并发；改造后 embedding 须先算完再派发（向量是参数），关键路径增加一次向量化往返（典型 100~300ms）。

### 3. search 重排收归 host（返回后、持久化前）

**挂点约束**：重排必须发生在结果**持久化与摘要之前**（否则 artifact 存的是未重排顺序、前端与 `$ref` 读取顺序错乱）。挂点选 `ToolRegistry.execute` 内 `tool.execute` 返回后、persist（`registry.py:382-422`）之前；模型调用能力由 loop 侧注入的可调用提供（`execute_tools_phase` 持有 agent backend）。loop 级后处理先例 `_annotate_search_citations`（`loop.py:152-230`，按 `actor_name == "search_documents"` 硬编码）不适用本用途（它跑在 persist 之后），但其"检索结果特判"模式可参考。

**触发**：工具结果来自 `search_documents` 且本次调用参数 `rerank=true`（键定方式与 citations 先例一致按工具名，或契约标记——Task 内定，倾向沿用工具名先例保持一致）。

**流程**：
1. 插件在 `rerank=true` 时返回 top-`rerank_fetch` 完整候选（含正文摘录），**不再自行重排**（参数语义对 agent 不变）。
2. host 取候选 → 候选预算裁剪逻辑整体从 `rerank.py` 迁入 host 模块（新 `courtier/agent/runtime/search_rerank.py`：`SEARCH_CANDIDATE_BUDGET_CHARS` 总预算、per-candidate 240-800 字符钳制、query-gram 居中证据窗，`:24-28,:50-99`）。
3. 拼 listwise 重排提示词 → **模板化**：`courtier/prompts/defaults/{locale}/` 新增核心模板（如 `search.rerank`），遵循"core 无硬编码 NL"仓库规则。
4. host 主模型 chat 一次（temperature=0，JSON 输出，经 host 模型客户端 → 自动入 OTel/Langfuse；usage 归入本次 run 的用量记录）。
5. 解析顺序（`_parse_order` 容错逻辑 :119-142 迁移）→ 重排 hits → 落 `rerank_partial` 标记语义：失败保持原序 + `rerank_partial=true`（现行为）。
6. 之后才进入 persist + summarizer（模型看到、artifact 存储、前端展示三者的顺序一致）。

**缓存语义（保持现状）**：插件检索结果缓存照旧；重排在 host 侧每次现算，rerank 路径不缓存（对应现状 `tools.py:717-719,:895-896`）。

**插件侧**：删 `rerank.py` 整文件；`tools.py:902-913` rerank 分支删除。

**配置迁移（host Settings 新增 2 字段，默认值同现状）**：`search_rerank_fetch`（现 `SEARCH_RERANK_FETCH=100`）、`search_rerank_candidate_budget_chars`（现 `SEARCH_CANDIDATE_BUDGET_CHARS=24000`）。其余 `SEARCH_*`（KNN_K/MAX_WINDOW/NEIGHBOR_WINDOW/CACHE_*）是插件检索行为参数，留插件 `plugin.yaml runtime.env`。

**模型变化（用户已确认）**：重排模型从插件 env 配的模型（现 `LLM_NAME=DeepSeek-V4-Flash`）变为 host 主模型——上线前 Task 4.2 用真实查询对比一次重排质量。

### 4. env 边界终态与文档同步

- `plugins/plugin.env.example`：删 `LLM_IP/LLM_API_KEY/LLM_NAME`、`LLM_EMBEDDING_NAME/DIM/BATCH_SIZE`、`DOCPARSE_CLASSIFY_MODE`、`DOCPARSE_MAX_LLM_CONCURRENT`、`DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE`；保留 TOKEN、MINIO_*（transfer）、ES_*、OCR、FONT_MODEL_*、ANYDOC_OCR_API_URL、LISTEN/WORKDIR、SEARCH_*（除迁移 host 的两个）。
- `courtier/.env.example`：LLM 通用段的注释修正——"内容审核 content_checker 使用"是陈旧描述（check_content 确认零 LLM），改为"Agent 对话模型与检索重排（host 使用）"。
- `AGENTS.md` §7.1/§10：恢复"插件不持 LLM 密钥"表述（MinIO transfer 凭据按插件独立化决策 6 保留为已记录例外）。
- 两份关联计划文档的修订（见"关联文档"）。
- 运维注意写入：扫描件要字体信息必须配 `FONT_MODEL_URL`。

---

## 文件职责

| 对象 | 操作 | 阶段 |
|------|------|------|
| `libs/docaudit/docparse/docparse/parsers/llm_client.py` | 整文件删除 | 0 |
| `libs/docaudit/docparse/docparse/parsers/_retry.py` | 删除（确认无其它消费者） | 0 |
| `libs/docaudit/docparse/docparse/parsers/scanned/__init__.py` | 删 classify_mode/LLM 回退/字体 LLM 兜底；fail-fast 删除 | 0 |
| `libs/docaudit/docparse/docparse/parsers/registry.py` | 混合 PDF 分发条件改写（去 llm_api_key 键控） | 0 |
| `libs/docaudit/docparse/docparse/parsers/base.py` | 删 LLM 相关 env 读取（:67-85 对应行） | 0 |
| `plugins/docaudit/parse/plugin.yaml` | runtime.env 必需变量声明去 LLM_* | 0 |
| `courtier/es/embeddings.py` | 新增 `embed_query` 单条入口 | 1 |
| `courtier/agent/tools/registry.py` | host 注入参数机制（标记识别 + 注入器 + schema 剔除） | 1 |
| `courtier/agent/runtime/search_rerank.py`（新） | 候选预算裁剪 + 顺序解析容错 + 重排编排 | 1 |
| `courtier/prompts/defaults/{locale}/`（新模板） | `search.rerank` 提示词模板 | 1 |
| `courtier/agent/tools/registry.py`（persist 前） | 重排挂点（rerank=true 触发，模型调用经注入可调用） | 1 |
| `courtier/config.py` | 新增 `search_rerank_fetch`、`search_rerank_candidate_budget_chars` | 1 |
| `plugins/shared/search/tools.py` | kNN 臂改用注入向量；rerank 透传语义；删两处 LLM 调用 | 2 |
| `plugins/shared/search/embeddings.py`、`rerank.py` | 整文件删除 | 2 |
| `plugins/shared/search/plugin.yaml` | 契约加 `query_embedding`（x-host-injected 标记）；runtime.env 去 LLM_*/迁移项 | 2 |
| `plugins/plugin.env.example`、`courtier/.env.example` | §4 边界同步 | 3 |
| `AGENTS.md`、两份关联计划文档 | §4 文档同步 | 3 |
| 对应测试文件（docparse、search、host 注入/重排） | 改写/新增 | 0-3 |

---

## Phase 0：docparse / parse 去 LLM

### Task 0.1: 结构识别 LLM 路径删除
- [x] 删 `llm_client.py`（结构部分）、`_retry.py`；`scanned/__init__.py` 固化规则引擎路径，删 classify_mode 分支/`_rules_acceptable`/逐页 LLM 回退
- [x] `registry.py` 混合分发去 `llm_api_key` 键控；`ScannedParser` fail-fast 删除
- [x] docparse 单测改写（classify_mode 用例改规则路径固定；LLM mock 用例删除）

### Task 0.2: 字体链 ResNet 唯一化 + env 清理
- [x] 删字体 LLM 兜底（`llm_client.py` 字体部分、拼图逻辑、模型失败回退）；未配置/未识别行无字体信息 + warning
- [x] `base.py` env 删除；`plugins/docaudit/parse/plugin.yaml` runtime.env 去 LLM_*；`plugin.env.example` 对应段删除
- [x] 字体链测试改 ResNet-only 语义；全量回归

## Phase 1：host 侧 glue 能力

### Task 1.1: embedding 注入机制
- [ ] `es/embeddings.py` 加 `embed_query`；`ToolRegistry.execute` host 注入参数机制（契约标记 `x-host-injected` 识别、注入器注册表、LLM 可见 schema 剔除）
- [ ] 单测：注入触发/未配置跳过/旧契约兼容/失败不注入

### Task 1.2: 重排模块与挂点
- [ ] `search_rerank.py`（预算裁剪 + `_parse_order` 容错迁移）+ `search.rerank` 模板 + Settings 两字段
- [ ] 挂点：`tool.execute` 后 persist 前，rerank=true 触发，模型调用经 loop 注入；失败保持原序 + `rerank_partial`
- [ ] 单测：重排成功/解析失败/模型失败/未触发四路径 + usage 记录

## Phase 2：search 插件改造

### Task 2.1: 契约与检索臂
- [ ] `plugin.yaml` 契约加 `query_embedding`（host-injected 标记）；`tools.py` kNN 臂改用注入向量，删 `embeddings.py` 与 DIM 校验
- [ ] rerank 透传语义：`rerank=true` 时返回 top-`rerank_fetch` 候选不自行重排；删 `rerank.py`

### Task 2.2: 集成验证
- [ ] loopback 集成（现有 `test_integration` 模式）：注入向量端到端（host 注入 → 插件 kNN 生效）、重排端到端（rerank=true → host 重排 → artifact 顺序一致）
- [ ] 降级路径集成：embedding 未配置纯词法；重排失败原序

## Phase 3：边界与文档同步 + 验收

### Task 3.1: env/文档同步
- [ ] `plugin.env.example` 终态、`.env.example` 注释修正、`AGENTS.md` §7.1/§10、两份关联计划文档修订、FONT_MODEL 运维注意
- [ ] `uv run courtier validate-domain domains/docaudit/`

### Task 3.2: 全量回归与真实会话验收
- [ ] `uv run pytest -m "not integration"` 全绿；webui `npm test`/`npm run build` 绿
- [ ] 真实验收对照总验收清单（含重排质量主模型 vs 旧插件模型对比）

---

## 提交切分（约定式）

1. `refactor(docparse): drop llm structure recognition, rule engine only`（Task 0.1）
2. `refactor(docparse): font recognition via resnet model only, drop llm fallback`（Task 0.2）
3. `feat(tools): host-injected parameter mechanism with embedding injector`（Task 1.1）
4. `feat(search): host-side listwise rerank with core prompt template`（Task 1.2）
5. `refactor(search-plugin): consume injected query embedding, drop llm clients`（Task 2.1）
6. `test(search): loopback integration for injection and rerank`（Task 2.2）
7. `docs(env): shrink plugin env boundary, sync plans and agents docs`（Task 3.1）

## 总验收

1. 插件进程零 LLM：`plugins/` 与 `libs/` 代码级 grep 无 LLM 端点调用；plugin.env / 插件容器 env 无任何 `LLM_*`
2. 扫描件 PDF 全链路（OCR → 规则结构识别 → ResNet 字体）产出 Document，无 LLM 调用；混合 PDF 无文本页走规则版扫描解析
3. `search_documents`：embedding 配置时 kNN 臂生效（注入向量日志可见），未配置纯词法——降级与现状一致
4. `rerank=true`：host 主模型重排生效、Langfuse 可见该调用与用量；解析/模型失败保持原序 + `rerank_partial`；artifact/摘要/前端顺序一致
5. embedding 单点配置：索引与查询同客户端同配置（两侧分裂与命名陷阱消除）
6. 真实检索对比：主模型重排 vs 旧插件模型重排的质量抽查（用户确认可接受）
7. 全量测试绿 + validate-domain 通过 + webui build 绿

## 明确不做的事

- 反向 RPC `llm.chat` host service（决策 1+3 下无需建设）
- utility/vision 模型配置槽（决策 3：共用主模型）
- agent 循环接手结构分类的兜底路径（用户知情接受纯规则；未来需要另立项）
- PPOCR OCR、ResNet 字体模型、ES 直连的归属变化（专用服务非 LLM，保留插件侧）
- 配置中心化/前端设置页（独立计划，本计划完成后其边界段落随之修订）
- `_annotate_search_citations` 挂点重构（仅参考其模式，不动它）

## 实施记录

**Task 0.1 完成**（结构识别 LLM 路径删除）。偏差与实测：
1. **`llm_client.py`/`_retry.py` 整文件留到 Task 0.2 删除**——字体识别的 LLM 兜底（同文件后半）在 0.1 仍在消费它，按"每提交绿"原则拆开；0.1 只删了 `structure_recognizer.py` 中全部 LLM 专属代码（`recognize_page_structure`、`_parse_header/_parse_body/_parse_footer`、合成线构建、附件断行拆分，950→597 行）。
2. `structure_recognizer.py` 保留面的划分依据：`_validate_header_slots`（版头槽位正则纠偏）与 `_build_paragraph`/`_merge_body_text_into_paragraphs` 为规则路径共用，保留并中性化 docstring 措辞。
3. 测试改写：`test_pdf_parser.py` 的 `TestRegistryDispatch`（计划遗漏的测试组）——`_pdf_dispatch` 去 `ocr_available` 参数、删"无 LLM 走 PdfParser"用例、`get_parser` 混合断言改无条件。管线测试的 `main_text==1` 断言依赖旧 LLM fake 行为，规则引擎把该短行归入 `issuing_signature`（置信 0.3），改为"内容保留在页面段落"断言（`collect_all_paragraphs`）。
4. 实测：`uv run pytest -m "not integration"` 1723 passed / 6 skipped；ruff 全绿。

**Task 0.2 完成**（字体链 ResNet 唯一化）。偏差与实测：
1. **`_retry.py` 保留**——计划写"删前确认无其它消费者"，确认结果是 `scanned/ocr_engine.py` 的 OCR 重试在用（`parallel_ocr` 的 `recognize_with_retry`），仅删了 `llm_client.py` 与 `font_image.py`（后者仅被 llm_client 消费）。
2. `FONT_MODEL_URL` 未配置时新增一条文档级告警"未配置字体识别模型（FONT_MODEL_URL），扫描页行将不带字体信息"（有 OCR 行时才发）；模型失败从"整页回退 LLM"改为"该页无字体信息 + 告警"；ResNet 未接受的行不再有任何兜底。
3. `plugin.env.example`：parse 段删 `LLM_*`/`DOCPARSE_CLASSIFY_MODE`/`DOCPARSE_MAX_LLM_CONCURRENT`/`DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE`；`LLM_IP/LLM_API_KEY/LLM_NAME` 临时移入 search 段（search 重排仍在用，Phase 2 随重排迁移一并删除）。`ParserConfig` 删 6 个 LLM 字段；parse `plugin.yaml` 注释同步。
4. 顺手修复：ruff 发现 `plugins/docaudit/parse/tools.py` 有一个本就未使用的 `logging` 导入（--fix 清理）。
5. 实测：docparse + parse 插件 482 passed；全量 `pytest -m "not integration"` 1723 passed / 6 skipped；ruff 绿。**parse 插件至此零 LLM**（结构=规则引擎、字体=ResNet、无 LLM env）。

（其余 Task 待实施；按 Task 记录偏差、实测与排障。）
