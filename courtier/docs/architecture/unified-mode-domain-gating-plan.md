# 统一会话模式 + 域门控 + OCR 统一 — 详细实施方案

## 背景与目标

废除 chat/audit 双模式，统一为单编排器；共享工具（plugins/shared）启动即可见，领域工具/技能按意图通过 `activate_domain` meta-tool 自激活（锁=可见性，插件进程照常启动）；内容类技能与阅读问答统一消费 `convert_document` 的 Markdown（含 OCR 兜底），仅 format_audit 消费 `parse_document` 的 Document 模型；两套 OCR 服务统一为 `http://192.168.100.67:8092`。

## 侦察结论（已验证事实）

- 插件发现：`PluginScanner` 递归扫 `plugins/**/plugin.yaml`（scanner.py:67）；`shared_plugin_names` 由 `plugins/shared/` 目录生成（app.py:183-192）。
- Agent 工具同步：`base.py:410-447` 每轮 `run()` 从 `_shared_tool_registry` 增量同步新工具并刷新系统提示词工具段（441-447）；`_refresh_plugin_guides`（448-450）按 agent 持有的插件自动注入插件 system_prompt——激活后领域插件 prompt 会自动出现，无需额外接线。
- Agent 按请求重建：sessions.py 续聊分支每条消息重新 build agent——激活状态必须显式持久化（会话记录），不能靠历史推导（上下文压缩会丢证据）。
- 会话持久化：文件型 JSON（session_store.py:374-400），加字段无需 DB 迁移。
- SessionRecord：frozen dataclass（api/models.py:246-276），`to_detail_dict` + `_load` raw.get 容错，加字段后向兼容。
- OrchestratorAgent（orch.py:41-136）：skill_registry/AgentRuntime 可选但成对要求；prompt = `orchestrator.system_prompt`（嵌 available_skills）+ `orchestrator.workflow_rules` → set_rules。core 默认模板目录 `prompts/defaults/{locale}/` 目前无 orchestrator.yaml（仅 behavioral/chat/context/errors/subagent/tools），orchestrator.* 由域包提供，FALLBACK_TEMPLATES 有极简英文兜底。
- 内容类插件输入均为纯文本：check_content(text,...)、correct_text(text)、detect_plagiarism(new_doc:str, reference_docs)，plagiarism 的 InputField 声明 `core.plain_text` 物化——零插件代码改动。
- Artifact 类型注册：工具声明 `output_artifact_type` 即可用，core schema 仅作元数据（docaudit.parsed_document 无 core schema 也在用）。投影器注册在 artifacts/projectors.py `_build_default_projector_registry`，schema 在 artifacts/models.py `_build_artifact_type_schemas`。
- manifest env 双白名单：`_ALLOWED_MANIFEST_ENV_VARS`（manager.py:81-124）+ `_SETTINGS_ENV_FALLBACK`（131-156，映射到 Settings 属性）。`DOCPARSE_OCR_DESKEW` 现已在白名单（106/139 行）——用户之前看到的 blocked 告警来自旧版本，已自愈，无需处理。
- **OCR 服务兼容性（实测）**：新旧 `/ocr` 端点响应结构逐键一致（15 键，扁平 rec_texts/rec_boxes/rec_polys/text_word，均无版面块）——docparse 切到 `http://192.168.100.67:8092/ocr` **零 adapter 代码改动**，仅配置。新服务 `/structure` 有 parsing_res_list 版面块但单页 22.5s（/ocr 仅 0.5s），且 rec_* 嵌套在 overall_ocr_res 下需 adapter 解包——本期不用，列为未来可选项。`/ocr/text` 实测：POST multipart `file` → `{"markdown": ...}`，整页按阅读顺序，中文识别质量好。
- DomainPackage（config.py:564-570）：`plugins_path=repo_root/"plugins"`（全域共享根）、`skills_path=domain_path/"skills"`、`prompt_bundle`（含域模板，激活载荷来源）。
- PluginScanResult 有 `dir` 字段——插件→域映射可从目录推导（plugins/shared→"shared"，plugins/<domain>/→域名）。

## 设计决策（已与用户确认）

1. 统一编排器为主体；`first_required_tool` 不再接线（机制保留闲置）。
2. 域门控 = 可见性过滤；激活机制 = 编排器自激活 meta-tool `activate_domain`（不做前置分类器，不做 list_domains——域目录静态嵌入工具描述）。
3. 取数：格式审核 → parse_document；内容类/阅读问答 → convert_document；格式+内容并存时内容侧复用 parse 文本投影，不重复 convert。
4. 扫描件兜底 = convert_document 自增 OCR 能力（/ocr/text），不回退 parse_document。
5. OCR 统一：docparse 切 `http://192.168.100.67:8092/ocr`（纯配置）；convert_document 用 `http://192.168.100.67:8092/ocr/text`。
6. `shared_plugin_names` 保留，语义改为"编排器初始可见集 = 共享插件"。
7. 多域冲突仲裁、插件进程懒启动：本期不做。

## 实施步骤（按提交批次）

### 批次 1：convert_document OCR 兜底（anydoc 插件）

- 新增 `plugins/shared/anydoc/ocr.py`（~120 行）：
  - `IMAGE_EXTENSIONS = {".jpg",".jpeg",".png",".tiff",".tif",".bmp"}`
  - `is_ocr_configured()`：读 `ANYDOC_OCR_API_URL`
  - `ocr_image(path) -> str`：requests.post(url, files={"file": (name, f, mime)}, timeout=60)，返回 `resp.json()["markdown"]`；非 JSON/非 200 → RuntimeError
  - `ocr_pdf(path) -> tuple[str, int]`：PyMuPDF 逐页渲染 PNG（150 DPI，`fitz`），`asyncio` 信号量 4 路并发 + `to_thread`，页间以 `【第 N 页】` 标记行拼接（部分缓解内容报告 location 退化）；单页失败即整单失败并报页号
- `plugins/shared/anydoc/tools.py` 改造：
  - 图片扩展名 → 直接 OCR 路径（anydoc 不处理图片）
  - anydoc 抛 `UnsupportedError` → OCR 路径（PDF 无文本层）
  - 未配置 `ANYDOC_OCR_API_URL` → 保持现报错（指向 parse_document）
  - 成功输出 `{"markdown", "format", "ocr": true, "pages": N}`；`ocr` 键仅 OCR 路径出现
- `plugins/shared/anydoc/plugin.yaml`：`runtime.env` 加 `ANYDOC_OCR_API_URL: "${ENV:ANYDOC_OCR_API_URL}"`；`timeout_ms` 60000 → 300000
- `plugins/shared/anydoc/pyproject.toml`：依赖 + `pymupdf`、`requests`；`uv sync` 重建插件 venv
- `courtier/plugin/manager.py`：`_ALLOWED_MANIFEST_ENV_VARS` + `_SETTINGS_ENV_FALLBACK` 各加 `ANYDOC_OCR_API_URL` 一行
- `courtier/config.py`：Settings 加 `anydoc_ocr_api_url: str = ""`（对照 `docparse_ocr_api_url` 现有写法）
- `.env` 加 `ANYDOC_OCR_API_URL=http://192.168.100.67:8092/ocr/text`；`.env.example` 同步
- 测试 `tests/plugin/test_anydoc_plugin.py` 新增：图片走 OCR（mock requests.post）、扫描 PDF 走 OCR（mock fitz+post）、未配置 → 指向 parse_document、OCR 服务失败 → 报错含页号、并发拼接页序正确
- 冒烟：插件 venv 直跑 `请示报告1.pdf`（真实 OCR 服务）
- 提交：`feat(plugins): add OCR fallback to convert_document via PPOCR text endpoint`

### 批次 2：OCR 服务统一（docparse 侧，纯配置）

- `.env`：`DOCPARSE_OCR_API_URL=http://192.168.100.67:8092/ocr`（原 100.118:8006/ocr）
- `.env.example` 同步；docparse `ParserConfig.from_env` 代码默认值（parsers/base.py 中 ocr api 默认）改为新地址
- 验证：docparse 单测全过（adapter mock 不变）；实跑一份扫描件 PDF 确认 ScannedParser 全链路正常
- 提交：`chore(config): unify OCR service endpoint to PPOCR v2 service`

### 批次 3：核心——工具可见性过滤（core）

- `courtier/agent/agents/base.py`：
  - `Agent.__init__` 新增 `tool_filter: Callable[[Any], bool] | None = None`，存 `self._tool_filter`
  - 构造期拷贝循环（216-237）与 run() 增量同步（410-433）均按 `_tool_filter(tool)` 过滤（None = 全放行，向后兼容）
  - 过滤依据工具 `_client.plugin_name`：共享集 or 已激活域；无 plugin_name（builtin/SkillTool）恒放行
- 测试：构造过滤、run() 同步过滤、插件重启热替换在过滤下仍工作、无 filter 行为不变
- 提交：`feat(agent): add tool visibility filter to agent tool sync`

### 批次 4：核心——域激活（activator + meta-tool + prompt 拆分）

- 新增 `courtier/agent/runtime/activation.py` `DomainActivator`（每编排器实例）：
  - 持有：app 全量 ToolRegistry、CourtierConfig（域列表/描述/skills_path/prompt_bundle）、AgentRuntime、PluginSystem（插件→域映射，新增 `plugin_domain(name)` 由 PluginScanResult.dir 推导）、PromptEngine、可变 `active_domains: set[str]`、`shared_plugin_names`
  - `visible(tool) -> bool`：供 tool_filter 闭包实时读取 active_domains
  - `activate(domain) -> ActivationResult`：校验域存在；幂等；扫描域 skills（SkillRegistry 缓存）；`AgentRuntime` 新增公共方法 `register_skills(registry)`（现 `_register_skills` 私有化调用改为公共可重入）；为域技能建 SkillTool 注册进 agent.tool_registry；渲染域 prompt_bundle 的 `orchestrator.workflow_rules` → `agent._prompt_pipeline.set_rules(overlay)`
- 新增 `courtier/agent/tools/builtin/activate_domain.py` `ActivateDomainTool`：
  - name `activate_domain`，参数 `{domain: string}`；描述静态嵌入域目录（name+description，构造时从 CourtierConfig 渲染）
  - execute → activator.activate；返回新可用工具/技能清单文本
- `courtier/agent/runtime/runtime.py`：`_register_skills` → 公共 `register_skills`（保持幂等，重复调用跳过已有）
- Prompt 拆分：
  - 新增 core 默认 `courtier/prompts/defaults/zh-CN/orchestrator.yaml` + `en-US/orchestrator.yaml`：`orchestrator.system_prompt`（域无关基础版：身份、文件取数策略——有上传且无领域意图 → convert_document；意图命中领域 → activate_domain、"疑似即激活"原则、convert_document OCR 失败时的降级说明）
  - `domains/docaudit/config/prompts/{locale}/orchestrator.yaml`：`orchestrator.system_prompt` 删除（回退 core 默认）；`orchestrator.workflow_rules` 保留并改写为"激活载荷"（docaudit 取数规则：格式审核 → parse_document；内容类 → 优先复用 convert 的 Markdown/parse 文本投影；审计工作流）
  - `chat.system_prompt` 模板保留（core 兜底），其最后消费者 build_chat_agent 删除——注释说明
- 测试：activator 单测（注册工具/技能、幂等、overlay 注入、未知域报错）；meta-tool 契约；core orchestrator.yaml 渲染断言；域包 system_prompt 缺失时回退 core
- 提交：`feat(agent): domain self-activation via activate_domain meta-tool`

### 批次 5：会话构建统一（agent_service + sessions 路由）

- `agent_service.py`：
  - 新 `build_agent(settings, plugin_system, tool_registry, courtier_config, prompt_engine, owner_id, session_id, active_domains=())`：建全量 AgentRuntime（skill_registry=None）+ DomainActivator + OrchestratorAgent（tools=[ListArtifacts, GetArtifact, ActivateDomainTool]，tool_registry=全量，tool_filter=activator.visible）；**不传** first_required_tool；建后按 `active_domains` 静默重放激活
  - 删除 `build_chat_agent` 与 `build_audit_agent`（或保留 audit 薄封装转发 build_agent，视引用面决定——优先删除并改引用）
  - `shared_plugin_names` 参数移入 build_agent（供 activator）
- `sessions.py`：新会话（fileId / 无 fileId）与续聊两分支 → 统一调 build_agent；fileId 时 `agent_context={"file_path": ...}` 保留；run 完成后把 `orchestrator.active_domains` 持久化进 SessionRecord
- `api/models.py`：SessionRecord + `active_domains: list[str]`；`to_detail_dict` 输出
- `session_store.py`：`_persist` 写 `active_domains`；`_load` `raw.get("active_domains", [])`
- `app.py`：`shared_plugin_names` 计算保留（新消费者 build_agent）
- 测试：路由统一 builder、fileId → file_path context、active_domains 持久化与重建重放、build_chat_agent 引用清零
- 提交：`refactor(api): unify session agent builder with domain gating`

### 批次 6：artifact 投影 + 内容技能取数改造

- `artifacts/models.py`：`_build_artifact_type_schemas` 加 `core.document_markdown`（required: markdown；properties: markdown/format）
- `artifacts/projectors.py`：默认注册表加 `core.document_markdown.to_plain_text`（markdown → {"text": markdown, "source_scope": "full_document"}）
- 内容技能 prompt 改写（domains/docaudit/skills/）：
  - `content_audit.md` / `text_correction.md` / `plagiarism.md` / `secret_analysis.md`：取数指令改为"优先使用会话中已有的文档文本（convert_document 的 Markdown 或 parse_document 的文本投影）；否则调用 convert_document(file_path) 获取 Markdown 作为 text 入参"
  - frontmatter `tools:` 补声明：`convert_document` + 各自审计工具（content_audit/text_correction 目前无 tools 字段——实施时先验证 runtime._build_agent 空 tools 的实际语义，再决定补法）
  - `skills/schemas/*.py` input 描述中 `$ref:parse_document:1` 表述同步更新
  - `format_audit.md` 不动
- 测试：投影器单测；技能 prompt 内容断言更新；plagiarism InputField 物化端到端（markdown → new_doc 字符串）
- 提交：`feat(artifacts): project document_markdown to plain_text; rewire content skills to convert_document`

### 批次 7：回归、文档、收尾

- 全量 `uv run pytest -m "not integration"`；`uv run courtier validate-domain domains/docaudit/`；`webui: npm run build && npm test`
- 受影响既有测试更新清单（实施时以实际失败为准）：双 builder 相关、first_required_tool 相关、orchestrator prompt 断言、技能 prompt 断言、路由 mode 分支相关
- 文档：根 AGENTS.md（§5.1/§5.3 流程描述、§10 注意事项）、courtier/CLAUDE.md（四层架构、会话模式描述）改写统一模式+域门控+OCR 统一；本方案留存于 `courtier/docs/architecture/unified-mode-domain-gating-plan.md`
- 提交：`docs: update architecture docs for unified mode and domain gating`

## 验证清单（每批次必做 + 端到端）

- [ ] 每批次：相关单测 + black/isort/ruff
- [ ] 批次 1：真实 OCR 服务冒烟（扫描件 PDF → markdown）
- [ ] 批次 2：ScannedParser 真实链路（新 OCR 服务）
- [ ] 批次 6 后端到端（本机 8000 重启后）：
  - 上传 docx + "总结一下" → convert_document（无激活）
  - 上传 docx + "按公文标准审一下格式" → activate_domain → parse_document → format_audit
  - 上传扫描件 PDF + "提炼要点" → convert_document OCR 路径
  - 上传 docx + "查重" → 内容技能消费 Markdown
  - 同会话追问"再审下内容合规" → 幂等激活，无重复解析
  - 续聊（重启会话）→ 激活态重放
- [ ] 全量回归 1300+ 通过；前端 build/test 通过

## 风险与缓解

- **意图漏激活**（agent 答"做不了"）：基础 prompt 写"疑似即激活"+关键词清单；激活幂等低成本
- **激活改变系统提示词稳定层**：rules 段本来就按轮替换；PromptPipeline 稳定层不动（identity 不嵌技能目录——技能目录随激活载荷进 rules 段）
- **插件重启后可见工具代理过期**：tool_filter 挂在既有 sync 循环上，热替换逻辑（base.py:421-433）对可见工具照常生效
- **上下文压缩丢激活证据**：激活态持久化 SessionRecord，按请求重建时重放，不依赖历史
- **技能空 tools 字段语义未确认**：批次 6 第一步先读 runtime._build_agent 空 tools 分支再定 frontmatter 补法
- **多页 OCR 耗时**：4 路并发 + 300s timeout；`/structure`（22.5s/页）本期不用
- **前端**：预期零改动（上传入口/步骤面板与模式无关）；批次 5 后人工点检一遍会话页

## 不做的事（明确排除）

- 多域注册表/同名冲突仲裁（第二域出现时再建）
- 插件进程懒启动（可见性门控已够）
- docparse 切换到 /structure 版面块（22.5s/页不划算，adapter 需解包 overall_ocr_res）
- 删除 `_maybe_forced_first_tool` 机制（保留闲置）
- chat.system_prompt 模板删除（保留为 core 兜底）
