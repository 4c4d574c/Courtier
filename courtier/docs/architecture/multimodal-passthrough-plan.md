# 多模态直通（图片/音频/视频原生内联）— 实施方案

## 背景与目标

当前整条聊天链是纯文本契约：`ChatMessage.content: str | None` 贯穿 protocol/state/序列化/压缩/token 记账；上传白名单虽含图片，但图片只能走 OCR 文字提取（外部 HTTP 服务），音频/视频完全不支持。目标：用户上传图片/音频/视频后，文件以**多模态 content part 直通**进入聊天模型上下文（不走"感知即工具"路线），由 vLLM/SGLang 部署的多模态模型原生理解。范围覆盖图片、音频、视频三模态，以及 **skill/子代理链路的直通**。

核心设计原则：**base64 字节绝不进入 agent 状态、压缩、持久化、事件流**——内部消息只流转轻量媒体引用（file_id + kind），仅在 `openai_backend` 发请求的边界物化为 data URI。

## 对齐结论（已与用户确认，2026-09-03）

1. **路线**：直通（content part 内联进聊天模型上下文），放弃此前讨论的"感知即工具"路线。
2. **能力判定**：池条目 `PoolModelConfig` 增 `modalities` 静态声明字段，作为**唯一**门控依据；不做管理台探测按钮；不做运行期错误学习兜底。
3. **目标 runtime**：仅 vLLM / SGLang 的统一 OpenAI 兼容接口（`image_url` / `input_audio` / `video_url`），**不做多厂商方言适配表**（dashscope/glm 原生 API 不支持）。
4. **视频**：只做原生 `video_url` 方言；**不做抽帧路线**。
5. **传输形态**：只做 base64 data URI + host 侧图片归一化；**不做 presigned URL 模式**。
6. **附件模型**：从会话级单 `fileId` 升级为**消息级多附件**（图片 ≤4、音频 ≤1、视频 ≤1），兼容旧 `fileId` 参数。
7. **门控行为**：所选模型缺模态时**前端禁发 + 后端 400**，不静默降级、不自动换模型。
8. **ffmpeg 归属**：media 插件（`plugins/shared/media/`），职责收敛为 **probe**（真实格式/时长/分辨率），无缩略图、无抽帧、无音轨提取。
9. **范围**：图片/音频/视频三模态全做（A/B/C/D 分期推进），且 **skill/子代理链路也要直通**（媒体进 SubAgent 任务消息）。

## 侦察结论（已验证事实）

**契约与序列化**
- `protocol.py:30-32` `ChatMessage.content: str | None`；`state.py:34-38` 同型；反序列化 `protocol.py:100` 直接 `content=d.get("content")`，无 part 分支。
- 报文组装**单路径**：`openai_backend.py:62-88` `_build_params` → `_message_to_openai` 逐条转换，流式/非流式共用；HTTP 客户端是 OpenAI SDK `AsyncOpenAI`（:47-51）。
- assistant / tool 消息恒为纯文本（模型响应只有文本）→ **只有 user 消息需要 part 形态**，改动面减半。

**历史与回放**
- 会话详情不回 `messages_json`，前端历史完全走 `turns`（`models.py:371-416` `_build_turns`，条目 `{message:{role,text,timestamp,fileName,fileId,modelId,modelName}, steps, conclusion}`）。
- `messages_json` 的存取在 `stream_service.serialize_messages/deserialize_messages`（:20/:39）+ `reconstruct_state`（:68）；编辑撤销裁剪 `compute_turn_truncation`（`session_service.py:227-253`）同样走 deserialize。
- 首条 user 消息构造：续跑 `base.py:517-522` 追加，新建 `AgentState.initial`（`state.py:80-93`）。

**附件与文件**
- 上传白名单/大小来自 `shared/file-upload-limits.json`（`file_service.py:27-38`），MIME 表 :40-56，magic-byte 校验 :60-78 已有；**音频/视频不在白名单**；`FileInfo`（`file_store.py:15-21`）无 kind/模态字段。
- **全仓无 uploads 删除/清理逻辑**（file_index.json 只增不减）→ 媒体引用不会悬空，无需保留策略改动。
- `file_path` 注入链：`sessions.py:307-309` → `run_manager.py:636-639` → `base.py:503` → `pipeline.py:163`（未消费 context 键落「# 任务上下文」段 :214-222）；强制首工具 `base.py:590-618`。
- 超长任务落盘 `$ref`（`sessions.py:76-104`）是既有"大 payload 落盘 + 引用"先例。

**模型池**
- `PoolModelConfig`（`config.py:102-128`）无 capability 字段；`GET /models`（`sessions.py:668-700`）只下发 `{id, name}`；解析 `resolve_model_profile`（`agent_service.py:50-103`）。
- settings 分类映射 `_setting_category`（`config.py:1046-1068`）：`media_*` 前缀现会落 advanced → 需加映射；前端分组 `settingsLabels.ts:168-195`。

**SkillTool / 子代理**
- 「# 输入数据」拼接：`skill.py:123-140` `_render_data_section`，`final_task` 拼接 `skill.py:228`；$ref 解析在 SkillTool 校验**之前**（`registry.py:393-395`）；校验 `skill.py:217` + `_render_validation_error` :142-158；spawn 派发 `skill.py:262-268`（context 带 file_path），inline 模式 `_execute_inline` :355-442。
- input_model 是 **pydantic 模型文件**（如 `domains/docaudit/skills/schemas/content_audit.py`），data 字段经 `_merge_typed_parameters`（`skill.py:102-121`）投影进工具 schema；字段名集 `subagent/base.py:16` `data_field_names`。

**guardrails / refusal（好消息：基本不用动）**
- refusal 检测（`loop_phases.py:207-210` `detector.match(response.content)`）与 output 层 `GuardContext.response_text`（loop.py:677-684）扫描的都是 **assistant 输出（恒纯文本）**，不受影响。
- input 层只传结构化 messages（loop.py:374-382）；全仓无 prompt 注入扫描。
- token 记账 `context_manager.py:711-726` 纯文本 CJK 启发式；微压缩 :310-330 替换旧 tool 结果；全量压缩 LLM 摘要 :54-71——三者需媒体分支。

**前端**
- 用户气泡 `UserMessage.vue`（`ChatMessage.vue:2` 按 type 分发）；历史经 `loadSession` → `session.turns`（`useAgentSession.ts:326`）；`FilePreviewPanel` 仅 `ChatLayout.vue:51` 使用；`InputArea.vue:30` accept 镜像 `constants/fileUpload.ts`；模型选择器 `useModelPool.ts:15-18` 仅 `{id,name}`。

**vLLM part 格式（源码级确认，main 分支）**
- 图片：`{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}`（OpenAI Vision 兼容）。
- 音频：`{"type":"input_audio","input_audio":{"data":"<base64>","format":"wav"}}`。
- 视频：`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}}`——`MediaConnector.load_from_url` 对 `data:` URI 通用解析（connector.py:373/418），`fetch_video` docstring 明写 "Load video from an HTTP or base64 data URL"。**整段视频 base64 可行**。
- 服务端帧采样由 vLLM 启动参数（`media_io_kwargs` 等）控制，host 侧无需干预。

## 总体设计

### 1. 消息契约（唯一的核心变更）

```python
# 内部逻辑形态（protocol.py 新增）
class MediaPart:            # dataclass
    kind: Literal["image", "audio", "video"]
    file_id: str
    name: str               # 占位标记/前端显示用

# ChatMessage.content / Message.content 类型扩展
content: str | list[TextPart | MediaPart] | None
```

约束：**仅 user 消息**允许 list；assistant/tool/system 恒 `str`（构造处断言）。提供两个 helper 供全链路使用：`content_to_plain_text(content)`（text parts 拼接 + 媒体转"[附件: name]"占位，供 refusal/摘要/占位场景）与 `iter_media_parts(content)`。旧持久化数据（纯 str content）天然兼容。

### 2. 能力声明与门控

- `PoolModelConfig.modalities: list[Literal["vision","audio","video"]] = []`（空 = 纯文本）；`ModelProfile` 透传；标量 `llm_*` 回退路径**视为纯文本**（带媒体附件 run → 400 提示改用池模型）。
- 门控点三处：① 前端发送前禁用 + 提示；② run 端点启动前校验（附件模态 ∉ profile.modalities → 400，`errors.media_model_unsupported` 中英模板）；③ SkillTool 对携带媒体字段的调用校验当前 run 模型（fail-closed）。
- **历史媒体降级规则（设计决定，可否决）**：续跑/换模型后，物化时发现当前模型缺该模态且媒体在**历史轮**中 → 剥离该 part、以"[附件: name（当前模型不支持图片/音频/视频）]"文本占位，不报错（否则会话被锁死）；**新附件**一律严格 400。这与"不静默降级"不冲突：降的是历史上下文呈现，不是用户请求。

### 3. 线级物化（openai_backend）

- **MediaResolver 注入**：核心 backend 不直接读 uploads——`OpenAIModelBackend` 构造时注入 `resolver(file_id, kind) -> (bytes, mime)` 回调，`build_model_client` 处用 `FileService` 绑定（保持核心/API 分层）。
- `_message_to_openai` 物化分支（按模态分三期落地，见任务拆分）：
  - image → Pillow 归一化（长边 > `media_image_max_edge` 或超限 → 缩放 + JPEG 重编码）→ `data:image/jpeg;base64,...`；
  - audio → `{"type":"input_audio","input_audio":{"data":...,"format":"wav|mp3"}}`；
  - video → `data:video/mp4;base64,...`（大小/时长上限在物化前硬校验，超限 `errors.*` 明确报错）。
- 依赖新增：Pillow 入 host `pyproject.toml`。

### 4. 记账与压缩

- `_compute_message_tokens`：list content = text parts 走现有 CJK 启发式 + 每个 media part 走固定估算常量（settings，见 §8；默认值实现时以真实 usage 校准）。
- 微压缩：对被压缩的 user 消息**剥离 media part**、保留 text part（与现有旧 tool 结果占位替换同层）；全量压缩的摘要视图经 `content_to_plain_text` 呈现（摘要模型可能无视觉能力）。多轮重发媒体的 token 成本由此阀门控制。

### 5. 附件模型与上传链路

- `GET /api/sessions/run` 增 `fileIds`（逗号分隔）参数，兼容旧 `fileId`（视为单元素）；校验：类型白名单、数量（图 ≤4 / 音 ≤1 / 视 ≤1，settings）、模态 vs 模型（§2）、大小/时长。
- `file-upload-limits.json` 分级：`allowed_extensions` 扩音频（mp3/wav/m4a/flac）与视频（mp4/mov/webm/mkv），per-kind 大小上限；magic bytes 扩展（ftyp/EBML/RIFF/fLaC/ID3 等）。
- `FileInfo` 增 `kind` 与 probe 元数据（`duration_seconds` / `width` / `height`）；file_index.json 读入时对旧记录按扩展名推断 kind。
- **media 插件**（`plugins/shared/media/`，独立 venv + plugin.yaml）：工具 `probe_media(file_path)` → ffprobe 出真实格式/时长/分辨率/编码。上传路由对媒体类型在 magic-byte 校验后**同步调用 probe**（经 PluginSystem 现有连接），元数据落 FileInfo；插件不可达 → 媒体类型上传 400（fail-closed），文档类型不受影响。
- 强制首工具 / file_path 注入让位：仅 document kind 附件保留现有强制 parse 行为；媒体附件不注入完整路径、不强制解析工具，改为在 user 消息 text part 末尾追加附件清单行（`[附件: photo.jpg (图片)]`，模型由此知道附件数量与名称）。

### 6. skill / 子代理直通

- 技能 input schema 增媒体字段类型：新增 `MediaRef` 值对象（kind 声明在字段定义处，如 `image: MediaRef = Field(kind="image")`）；`_merge_typed_parameters` 投影为工具 schema 时呈现为 string 参数（orchestrator 填 file_id，description 注明引用当前会话附件）。
- `SkillTool.execute`：媒体字段**不进** `_render_data_section` 文本段（仅列名称行），校验通过后转为 MediaPart 追加到 spawn 的 task 消息 content（list 形态）；inline 模式同样把媒体 parts 注入指令消息。校验失败路径：文件不存在 / kind 不匹配 / 模型缺模态 → `errors.skill_media_*` 模板。
- `data_field_names` / 校验错误渲染（`_render_validation_error`）对 MediaRef 字段兼容。

### 7. 持久化 / 回放 / API 面

- `serialize_messages/deserialize_messages`、`reconstruct_state`、`compute_turn_truncation` 对 list content 往返（引用 dict 形态，无字节）。
- `turns[].message` 增 `attachments: [{fileId, kind, name}]`（兼容旧 `fileName/fileId` 单文件字段，两者并存）；`session_store.create/add_turn` 写入。
- 前端媒体气泡渲染复用历史文件预览的已鉴权取回机制（fetch blob → objectURL），`<img>` / `<audio controls>` / `<video controls>`。

### 8. settings 键清单（`media_` 前缀 → 挂 **model** 分类，`_setting_category` 加一行前缀映射；前端 model 组自动收纳）

| 键 | 默认 | 说明 |
|---|---|---|
| `media_max_image_bytes` | 20MB | 归一化前图片上限 |
| `media_max_audio_bytes` / `media_max_audio_seconds` | 25MB / 1800 | 音频上限 |
| `media_max_video_bytes` / `media_max_video_seconds` | 50MB / 300 | 视频上限（base64 报文 ≈ 1.37×） |
| `media_max_images_per_message` | 4 | 每消息图片数 |
| `media_image_max_edge` / `media_image_jpeg_quality` | 2048 / 85 | 归一化参数 |
| `media_image_token_estimate` | 1024 | 记账常量 |
| `media_audio_tokens_per_second` | 40 | 记账常量（实现时校准） |
| `media_video_tokens_per_second` | 200 | 记账常量（实现时校准） |

## 任务拆分（每任务独立提交，conventional commits）

- **T0 真机 pinning**（不改码）：对实际部署的 vLLM/SGLang 各发三组 curl（image data URI / input_audio / video_url data URI），确认 part 字段与版本支持；结论回填本文档实施状态。**若部署版本的 video data URI 不被支持 → 停下升级 vLLM 或重议 URL 模式（风险 1）**。
- **T1 契约层**：MediaPart/TextPart + protocol/state 类型扩展 + 两个 helper + 序列化往返（内部形态，物化留 T4）+ 旧数据兼容。单测：往返、纯 str 兼容、非 user 消息断言。
- **T2 能力声明与门控**：`modalities` 字段 + `ModelProfile` 透传 + `GET /models` 扩展 + run 端点 400 + `errors.media_model_unsupported` 模板（中英）。单测：解析、400、locale 渲染。
- **T3 上传与 probe**：白名单/分级大小/magic bytes + `FileInfo` kind/probe 元数据 + `fileIds` 多附件参数（数量校验）+ media 插件（probe_media）+ 上传链路接入与 fail-closed + dev-plugins/compose 挂载。单测：白名单/魔数/kind 推断/多附件校验/probe mock；插件单测用样例媒体文件。
- **T4 图片物化**：MediaResolver 注入 + image 分支（Pillow 归一化 + data URI）+ 附件清单行注入 text part + 强制首工具/file_path 让位。单测：归一化触发、data URI 正确、超限报错。
- **T5 记账与压缩**：token 估算媒体分支 + 微压缩剥离 + 摘要占位视图 + `media_token_*` settings。单测：估算、剥离后预算回落、占位文本。
- **T6 持久化与回放**：serialize/deserialize/reconstruct_state/truncation 的 list 往返 + `turns[].message.attachments` + create/add_turn 写入。单测：往返、旧 turns 兼容、裁剪含媒体轮次。
- **T7 图片端到端 webui（A+B 验收点）**：模型选择器模态徽标 + 禁发校验 + 多选上传 + UserMessage 媒体气泡渲染 + fileUpload.ts 同步。node 测试 + build。**真机验收：图片上传 → 模型回答引用图片内容 → 历史回放 → 续跑重建 → 换纯文本模型续跑（历史媒体降级占位）**。
- **T8 音频（C 期）**：input_audio 物化分支 + 时长上限 + 前端 audio 气泡 + 上传 accept 扩展。验收：音频上传 → 模型转写/理解 → 回放。
- **T9 视频（D 期）**：video_url 物化分支 + 大小/时长上限 + 前端 video 气泡。验收：短视频上传 → 模型理解 → 回放。
- **T10 skill/子代理直通**：MediaRef 类型 + `_merge_typed_parameters` 投影 + SkillTool 媒体 parts 注入（spawn/inline 两模式）+ 校验链 + `errors.skill_media_*` + docaudit 试点技能一个（input_model 含 MediaRef 字段）。单测：spawn 任务消息含 parts、各校验失败路径、inline 模式。
- **T11 收尾**：全量 `uv run pytest` + `npm test`/`build` + 真机三模态回归（含换模型降级路径）+ 文档同步（AGENTS.md §5.3/§7.1、CLAUDE.md、settings-and-secrets.md、本计划实施状态）。

## 明确不做（本期）

- "感知即工具"路线的 `analyze_image`/`transcribe_audio`/`analyze_video` agent 工具（路线已定直通；`probe_media` 是上传校验服务，不是 agent 工具）。
- presigned URL / HTTP URL 传输模式；抽帧、音轨提取、缩略图（视频只走原生方言）。
- 管理台探测按钮、运行期错误学习、多厂商方言表（dashscope/glm 原生 API）。
- assistant/tool 消息携带媒体（模型响应恒文本）；工具结果返回图片。
- 媒体内容的提示注入扫描（记录为已知安全边界：图片/音视频内的指令内容绕过文本 guardrail，由 provider 侧模型策略兜底）。
- 资源库上传（`ResourceLibraryView` accept 不动）；标量 `llm_*` 回退路径的模态支持。

## 风险与边界

1. **部署版本支持**（最大风险）：video data URI 已在 vLLM main 源码确认，但实际部署版本的 `video_url`/`input_audio` 支持度以 T0 为准；SGLang 的 part 字段名同样 T0 实测。不支持 → 升级 runtime 或重议视频 URL 模式（需再拍板）。
2. **请求体大小**：视频 base64 ≈ 1.37 倍原始体积进单个 JSON 报文（50MB 默认上限 ≈ 68MB 报文）；vLLM 侧 uvicorn 无默认 body 上限，但反代（若有）需同步放宽；上限全部 settings 可调。
3. **归一化副作用**：Pillow 重编码对透明通道/EXIF 的处理在 T4 实现时明确（默认保留 PNG 小图不重编码）。
4. **多轮 token 成本**：历史媒体每轮重发是真实成本，压缩剥离（§4）为主阀门；估算常量偏差通过 usage 校准。
5. **guardrail 边界**：媒体内注入内容不做扫描（见明确不做），文档记录。

## 验收标准

- 三模态各自：上传（含 probe 元数据）→ 内联直通 → 模型回答体现真实媒体理解 → 历史回放渲染 → 续跑重建一致。
- 纯文本会话零行为差异：无附件的消息 content 恒为 str，改动前后序列化逐字节一致；`uv run pytest` 全量绿、`npm test`/`build` 通过。
- 门控：缺模态模型 + 新附件 = 前端禁发 + 后端 400；历史媒体在纯文本模型下降级占位不报错；标量回退路径带媒体 400。
- 压缩：媒体被剥离后预算回落、占位标记可读；`media_*` 设置键全部进 admin 设置页 model 分组。
- 每任务独立 conventional commit；T0/T7/T8/T9/T11 的真机结论回填本计划实施状态。

## 实施状态（滚动更新）

- 2026-09-03：需求对齐完成（对齐结论 9 条），vLLM part 格式源码级确认（video data URI 可行），方案成文，待批准后排期。
- 2026-09-03（批准后）：**T0 完成**，结论：
  - 部署 runtime pin 到 **vLLM 0.23.0**（`192.168.100.31:8008`，`GET /version` 确认；池内另一端点 `192.168.100.67:8001` 当前拒连）。
  - **wire 格式 pinning 通过**：对 vLLM 0.23.0 分别发送 `image_url`（data URI）/ `input_audio`（base64 wav）/ `video_url`（data URI）三种 part，服务器一律以 `"DeepSeek-V4-Flash is not a multimodal model"`（HTTP 400）拒绝——三种 part 类型均在请求解析层被正确识别，拒绝发生在模型能力检查，证明 part 命名与嵌套结构正确。
  - **当前无多模态模型部署**：唯一在线端点仅 DeepSeek-V4-Flash（纯文本）。真实解码冒烟（尤其视频 data URI）顺延到 T7/T8/T9 验收，前置条件是池内接入 Qwen-VL/Omni 级多模态模型（需运维部署）。
  - 备注：开发库标量回退指向 DashScope compatible-mode（在约定的 vLLM/SGLang 范围之外），其 `modalities` 按设计为空，媒体功能在该路径自然关闭。
