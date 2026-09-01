# 多模型接入与前端可选（模型池）— 实施方案

## 背景与目标

当前整条聊天链只有一个模型：`llm_base_url / llm_api_key / llm_model` 一组标量设置，orchestrator、subagent、rerank 共用一个客户端实例；换模型 = admin 改全局设置，所有会话一起变。

目标：admin 在管理后台维护一个**两级模型池（接入点 → 模型）**，可混布不同供应商（OpenAI-compatible）；普通用户在前端**每次运行都可选择**用哪个模型；聊天链跟随所选模型，embedding 保持独立。不重构现有设置体系与 agent 构建管线。

## 对齐结论（已与用户确认，2026-09-01）

1. **池形态**：接入点 → 模型两级结构（非平铺列表，非接入点单选）。
2. **选择粒度**：每次运行可换；缺省继承会话上次所用 → 全局默认；按轮记录模型可追溯。
3. **权限**：admin 配池（管理后台 → 系统设置，key 加密存储）；普通用户只读选用，看不到 key。
4. **附属模型**：聊天链（orchestrator / subagent / SkillTool / rerank）跟随会话所选模型；embedding 独立配置。
5. 三个细化决策点按推荐执行（用户未提出调整）：
   - **A. 池存 settings 体系**（typed JSON 字段 + 密钥映射 secret 字段），不新建 `llm_endpoints` 表——热生效、审计、admin API、UI 管线全部复用。
   - **B. per-model 覆盖范围**仅 `context_window_tokens` / `max_tokens` / `temperature` 三项；penalty / extra_body / timeout 继续走全局。
   - **C. 新增 embedding 独立端点键**（`llm_embedding_base_url / llm_embedding_api_key`，空值回退现有标量），否则聊天池与 embedding 被同一标量端点锁死。

与讨论稿的一处偏差：全局默认模型不设独立 `llm_default_model` 标量键，而是放进池结构内（`default_model_id`）——id 唯一性、default 可解析性等跨字段校验可以放进 pydantic `model_validator`，`compose_snapshot` 保存时重校验（`settings_store.py:323-337`）天然拦截坏结构。

## 侦察结论（已验证事实）

- **标量配置面**：`config.py:163-197`（llm_base_url/api_key/model/temperature/max_tokens/timeout/penalties/extra_body）、embedding 键 `config.py:200-218`、上下文窗口 `config.py:243-247`。全部经 `_setting_category`（`config.py:877-893`，`llm_`/`context_` 前缀 → `model` 类）进入 admin 设置；`_SECRET_SETTING_FIELDS`（834-843）控制 Fernet 加密；`build_settings_meta`（896-920）仅排除 Tier-0 与 `agent_runtime`，**新 `llm_*` 字段自动进入 DB 可编辑面且默认 hot**。
- **字段类型推断**：`_field_type`（`admin_settings.py:67-83`）对非标量注解一律返回 `"json"`（`llm_extra_body` 的 dict 先例）→ BaseModel 字段无需改动即渲染为 JSON。
- **secret 读写语义**：GET 对 secret 只回 `{set, tail}`（`admin_settings.py:94-97, 131-132`），不可回读 → 前端 changed-only diff（`settingsForm.ts buildUpdateBody:75-160`）+ 保存走 `compose_snapshot` 全量重校验。
- **连通性测试**：`POST /api/admin/settings/test/llm`（`admin_settings.py:303-367`）支持 body overrides（未保存先测）。
- **模型客户端**：`build_model_client`（`agent_service.py:72-120`）单实例；`build_agent:156` 下发给 OrchestratorAgent / AgentRuntime / MemoryManager；子代理复用 `self.model`（`runtime.py:519`）。`OpenAIModelBackend._build_params` 已支持 per-request `model` 覆盖。`ModelRouter` fallback 只能共用同一 endpoint（`agent_service.py:103-106` 注释明言 per-endpoint 配置不存在）。
- **运行入口**：`GET /api/sessions/run`（`sessions.py:144-343`）无任何模型参数，agent 纯按设置快照构建（280-294）；`SessionRecord.model_name` 仅作展示（`models.py:278`）；turn_messages 条目为 dict（`models.py:294-298`），`add_turn`（`session_store.py:325-355`）是按轮记录的挂点。
- **SSE**：`model_selected / model_fallback` 事件已存在（`sse_adapter.py:361-378`），前端 ChatHeader 已渲染 fallback chip。
- **rerank**：post-processor 在 app 启动时注册（`app.py:291` → `make_search_rerank_post_processor`），但调用时自取 `get_settings()`（`search_rerank.py:231-233`）→ 需要 per-run 档案通道。
- **embedding**：`embeddings.py:28, 38-41` 直接复用标量 `llm_base_url / llm_api_key`。
- **上下文窗口**：全局标量 → `_context_budget_kwargs`（`agent_service.py:295-315`）按比率切预算。
- **前端**：`InputArea.vue:46` 只读展示「模型： xxx」；`settingsLabels.ts:138-143` CATEGORY_GROUPS 已有 model 类三组（embedding/llm/context）；`SystemSettings.vue:101-137, 409-454` 的 `courtier_plugin_endpoints` 行编辑器是结构化多行编辑的现成先例。

## 总体设计

### 1. 数据模型（config.py 新增）

```python
class PoolModelConfig(BaseModel):
    id: str          # "mdl_" 前缀稳定 id —— 前端选择、运行传参、持久化都用它
    name: str        # 前端显示名
    model: str       # 发给 API 的 model 参数
    context_window_tokens: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None

class PoolEndpointConfig(BaseModel):
    id: str          # "ep_" 前缀
    name: str
    base_url: str
    enabled: bool = True
    models: list[PoolModelConfig] = []

class ModelPoolConfig(BaseModel):
    endpoints: list[PoolEndpointConfig] = []
    default_model_id: str = ""
    # model_validator：ep_/mdl_ id 各自唯一；default_model_id 可解析且其端点 enabled
```

Settings 新字段（`llm_` 前缀自动归 `model` 类、hot、进入 admin 面）：

| 字段 | 类型 | secret | 说明 |
|---|---|---|---|
| `llm_model_pool` | `ModelPoolConfig` | 否 | 池主体（含 default_model_id） |
| `llm_endpoint_keys` | `dict[str, str]` | **是** | endpoint id → api_key，整字段 Fernet 加密 |
| `llm_embedding_base_url` | `str` | 否 | 空 → 回退 `llm_base_url` |
| `llm_embedding_api_key` | `str` | **是** | 空 → 回退 `llm_api_key` |

标量 `llm_base_url / llm_api_key / llm_model / …` 全部保留：迁移播种源、embedding 回退、池为空时的兜底。

**密钥为何独立 secret 字段**：GET 对 secret 字段只回 `{set, tail}`——若密钥并入池 JSON 且整字段标 secret，URL/名称等非敏感部分也无法回显编辑。故池主体非敏感、密钥映射单独加密。

**`llm_endpoint_keys` 的 GET/PUT 特例**（`admin_settings.py` 两处小型特例，加密/审计/版本/热生效管线全部复用）：
- GET：按条目渲染 `{endpointId: {set, tail4}}`（而非整字段一个 tail）。
- PUT：合并语义——提交条目**有值 = 覆盖、缺省 = 保留旧值、显式 null = 删除**。secret 不可回读，前端无法整表重发，必须服务端合并。

### 2. 档案解析（运行入口优先级）

```
显式 modelId → 必须存在且端点 enabled，否则 400（路由级 HTTPException，
              与现有路由风格一致；errors.* 模板只用于模型可见错误，此处不用）
→ 会话 last_model_id（仍存在且启用）
→ 池 default_model_id
→ 池为空/未设置 → 标量 llm_*（完全兼容现状）
```

产出 `ModelProfile`（dataclass：`model_id, name, base_url, api_key, model, context_window_tokens, max_tokens, temperature`）；无池时为 `None` → 标量路径。解析器 `resolve_model_profile(settings, model_id, session_last_model_id)` 放 `agent_service.py`，与 `build_model_client` 同处。

### 3. 运行链路

- `GET /api/sessions/run` 增 `modelId` query 参数；解析失败在启动前 400，不静默回退。
- `build_model_client(settings, profile=None)`：profile 存在时 backend 参数取档案（timeout / penalties / extra_body 仍全局）；**profile 存在时不包 ModelRouter**（`agent_runtime.model.fallback_backends` 与池的结合留待后续，注释同步更新）。
- `build_agent(..., model_profile=None)` 透传；`_context_budget_kwargs` 用档案 `context_window_tokens` 覆盖全局窗口（不同模型窗口差数倍，必须有）。
- `build_agent` 返回的 `model_name` = 档案显示名；`model_selected` SSE 载荷增 `modelId / modelName`（`sse_adapter.py:361-369`）。
- **per-run 档案通道给 rerank**：新 contextvar——run 启动时 set 当前档案，`make_search_rerank_post_processor` 闭包内读取（注册是 app 级 `app.py:291`、消费是 per-run）；未 set 时回退 `get_settings()` 现状。
- **持久化**：`SessionRecord` 增 `last_model_id: str = ""`（run 启动时 update，供重载预选）；`add_turn` 增 `model_id / model_name` kwargs → turn_messages 条目带模型（按轮追溯 + 历史渲染）；新会话 `create()` 的 `model_name` 用本次档案显示名。

### 4. API 面

- 新增 **`GET /api/models`**（登录用户即可）：池公开视图 `[{endpointId, endpointName, models: [{id, name}]}] + defaultModelId`。**不含 base_url / key**（不对普通用户暴露接入点细节）。admin 设置接口是 `require_admin`，不能复用。
- `POST /api/admin/settings/test/llm` 扩展：body 可带 `{endpointId, modelId}`（从快照池 + 密钥映射取值）或维持现状的完整 base_url/model override——池编辑器按条目测试。
- `PUT /api/admin/settings/model`：`llm_model_pool` 结构校验由 `compose_snapshot` 的 model_validate 天然拦截（422 fail-loud）；`llm_endpoint_keys` 走合并特例。

### 5. admin UI（SystemSettings.vue + settingsLabels.ts）

- `CATEGORY_GROUPS` model 类新增组 `{key: "pool", label: "模型池", names: ["llm_model_pool", "llm_endpoint_keys"]}`。
- 两级行编辑器（参照 `courtier_plugin_endpoints` 先例）：接入点行（名称 / base_url / 启用 / Key 密码框显示「已设置(尾号xxxx)」/ 测试按钮）展开模型行（显示名 / 模型 ID / 窗口 / max_tokens / temperature）；顶部「默认模型」下拉。
- 回写：`llm_model_pool` 整体 JSON；`llm_endpoint_keys` 只发变更条目（配合合并语义）。
- 现有标量 `llm_*` 留在「LLM 接入」组，描述文案改为「兜底 / 迁移源」。

### 6. 用户选择器（webui）

- `InputArea.vue:46` 只读模型名 → 下拉触发器 + popover：按接入点分组列出启用模型、默认标记；数据源 `GET /api/models`。
- `useAgentSession` 发起 run 的 URL 拼接处附 `modelId`（当前会话选中态；切换即刻对下次运行生效）。
- 会话重载预选 `lastModelId`（session detail 增字段）；ChatHeader / 步骤展示沿用 `model_selected` 事件。
- 普通用户只见显示名，无 key / URL。

### 7. embedding 独立

- `embeddings.py`：生效 url/key = `llm_embedding_base_url || llm_base_url`、`llm_embedding_api_key || llm_api_key`；`embedding_enabled`（`embeddings.py:22-29`）同步改判定。
- embedding 不进池（对齐结论 4）；索引与查询两侧共用 `embed_chunks`，单点改。

### 8. 迁移播种

- `refresh_settings_snapshot`（`settings_store.py:379-452`）DB 模式下：池为空 && 标量 `llm_base_url + llm_model` 非空 → 写入单接入点单模型池（`ep_main / mdl_main`，default 指向之），`actor="system-migrate"`，审计留痕，一次性。
- env-only 模式不播种——解析器天然回退标量，行为不变。

## 任务拆分（每任务独立提交，conventional commits）

- **T1 config 数据模型**：三个池 pydantic 模型 + Settings 四个新字段 + `_SECRET_SETTING_FIELDS` / `_setting_category` 适配 + `model_validator`（id 唯一、default 可解析）。单测：校验器拒绝场景、`LLM_MODEL_POOL` env JSON 解析、meta 断言（分类= model、effect= hot、secret 标记）。
- **T2 设置 API 特例 + 播种**：GET per-entry mask、PUT 合并语义、`test/llm` 按条目测试、迁移播种。单测覆盖合并三态（覆盖/保留/删除）与播种幂等。
- **T3 档案解析与客户端构建**：`ModelProfile` + `resolve_model_profile` + `build_model_client(profile)` + `build_agent` 线程 + 窗口覆盖 + profile 时跳过 ModelRouter。单测：优先级链路（显式 → last → default → 标量）、停用/未知拒绝、backend 参数取档案。
- **T4 run 路由与持久化**：`modelId` 参数 + 400 路径 + `SessionRecord.last_model_id` + `add_turn` 模型字段 + `model_selected` 载荷 + `GET /api/models`（含权限：登录即可、无敏感字段）。单测 + 路由测试。
- **T5 rerank 跟随 + embedding 独立**：contextvar 通道 + post-processor 读档案 + embeddings 生效键回退。单测：未 set 回退现状、set 后走档案、embedding 双键回退。
- **T6 webui admin 池编辑器**：`CATEGORY_GROUPS` 组 + 两级编辑器 + 序列化/回写 + 测试按钮接线。node 测试：序列化往返、keys 变更 diff。
- **T7 webui 选择器**：api client `getModels` + InputArea 下拉 + `useAgentSession` 附 `modelId` + 重载预选。node 测试：选择状态、run URL 拼接。
- **T8 收尾**：`uv run pytest` 全量 + `npm test` / `npm run build` + 文档更新（根 `AGENTS.md` §5.3/§7.1、`courtier/CLAUDE.md`、`docs/operations/settings-and-secrets.md` 提模型池）+ 真机双端点切换验收记录（追加到本文件实施状态）。

## 明确不做（本期）

- per-profile fallback 链 / `ModelRouter` strategy 与池的结合（池是它的前置；`agent_runtime.model` 机制保持现状）。
- 用户私有池 / 自带 key。
- 按用途独立选模型（embedding / rerank 单独指定）。
- 池条目用量统计、成本核算、编辑器拖拽排序等。

## 验收标准

- admin 配置两个不同 base_url/key 的接入点各 ≥1 模型，保存后**无需重启**下一次运行即生效。
- 普通用户在输入区切换模型发起运行：`model_selected` 与会话详情显示所选模型；subagent 与 rerank 走同一档案（日志/遥测可见）。
- 每轮可换模型；会话重载预选上次所用；未知/停用 `modelId` 在启动前 400。
- 清空池回退标量行为（向后兼容）；旧部署升级自动播种，用户无感。
- embedding 未配独立键时行为与现状一致；配了独立键后指向新端点。
- `uv run pytest` 全绿；webui `npm test` + `npm run build` 通过。
- 每任务独立提交；实施偏差记录于本文件。

## 实施状态（滚动更新）

- 2026-09-01：需求对齐完成（见对齐结论），方案成文，待批准后按 T1 起步。
