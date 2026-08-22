# SkillTool 结构化输入接线 — 设计决策文档

- 日期：2026-08-22
- 状态：设计定稿，待批准实施
- 前置依赖：mid-run 领域规则送达修复（config/orch/loop 三处，2026-08-22 方案）先行落地——
  规则送达是模型学会使用新参数的前提。
- 关联事故：sess_f575a957e19d（主循环把全文粘进 content_audit 的 task）

## 1. 背景与动机

`SkillConfig.input_model` 与 `SubAgentInput.explicit_inputs` 目前是装饰性代码：

| 环节 | 现状 |
|------|------|
| 技能声明 | 4 技能中仅 plagiarism.md 声明 `input_model`；`schemas/format_audit.py` 是无引用的孤儿模块 |
| 注册加载 | `SkillRegistry._resolve_input_model` 能加载校验（唯一生效环节） |
| 运行时 | `AgentConfig.input_model`（runtime.py:148）存入后无任何消费者 |
| 工具参数 | `SkillTool.parameters` 硬编码 task/file_path/ref_ids/mode，schema 不参与生成 |
| 派发 | spawn/delegate 只传 task 字符串，无 pydantic 校验、无字段注入 |
| explicit_inputs | 字段描述承诺"dispatch 前自动注入"，该机制不存在 |

价值判断：结构化字段让"数据"拥有显式通道（$ref 直传字段），消除"往 task 里拼正文"的
最后理由；同时参数描述成为模型学习取数方式的第一现场。

## 2. 已定决策

需求对齐闸门（2026-08-22）用户拍板：

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | task 与结构化字段职责边界 | **task=指令（审核要求、输出期望），结构化字段=数据（document 等）** |
| D2 | 校验失败契约 | **硬失败**：success=False + 字段级错误清单，模型下一轮自纠；pydantic v2 智能类型强转开启 |
| D3 | `SubAgentInput.explicit_inputs` | **删除**（字段 + orch.py/base.py 两处排除清单残留）；typed 字段直传 $ref 已覆盖其场景 |
| D4 | 首批迁移范围 | **四技能全迁**（content_audit、secret_analysis、format_audit、plagiarism、full_government_audit 中的全部启用项） |

事实性决策（代码/日志实证，无需选择）：

| # | 决策点 | 结论 | 依据 |
|---|--------|------|------|
| F1 | pydantic | v2（≥2.13），`model_json_schema()` 可用 | pyproject.toml |
| F2 | 参数合并 | 全量合并：typed 字段追加进 SkillTool.parameters；task/mode 保持必填；注册期字段名冲突（∩{task,file_path,ref_ids,mode}）→ SkillRegistry 扫描错误、技能不注册 | 唯一兼容存量调用方的方案 |
| F3 | $ref 解析 | 零新机制：ToolRegistry.execute 已按 param schema 类型自适应解析（string 字段走 _TEXT_FIELD_PRIORITY 文本投影；dict 字段取完整对象），typed 字段直接受益 | registry.py:353-357、cache_store.py |
| F4 | ref_ids | 保留（scoped store 可见性白名单，真实日志中模型用过） | sess_65d215b49c1b |
| F5 | file_path | 保留（子代理自取兜底通道，技能取数规则第 3 条依赖） | content_audit.md |
| F6 | 错误文案 | PromptEngine 新键 `errors.skill_input_validation`（core defaults zh/en + FALLBACK_TEMPLATES 兜底），遵守"core 无硬编码 NL"约定 | AGENTS.md §7.1 |
| F7 | 兼容性 | 无迁移风险：派发无持久状态，build_agent 每请求重建 | RunManager 设计 |

## 3. 架构设计

### 3.1 参数生成（SkillTool.__init__）

- 从 `skill.input_model` 取**子类自有字段**（排除 SubAgentInput 基类字段 task/ref_ids/output_for），
  经 `model_json_schema()` 提取 properties（name → {type, description}），追加进 `self.parameters`。
- Field description 随之进入工具 schema——这是 $ref 用法指引的第一现场（沿用 plagiarism 的
  document 字段描述风格）。
- `str | dict` 联合类型生成 anyOf schema：resolve_refs 对 anyOf 不做投影、按原样加载——
  适合需要完整 Document 对象的 format_audit；纯文本消费字段一律声明 `str`。
- 构造函数新增可选 `prompt_engine` 参数（activation.py 与 orch._build_skill_tools 传入）；
  缺省时 PromptEngine() 落 FALLBACK_TEMPLATES。

### 3.2 校验契约（SkillTool.execute）

1. 从 kwargs 分离：task/mode/file_path/ref_ids + typed 数据字段。
2. `input_model.model_construct` 前先 `model_validate(**data_fields)`（task 不入模型——
   D1 划界后模型只承载数据字段；SubAgentInput.task 由 spawn 的 task 参数承载，校验时排除基类字段）。
3. 校验失败 → `ToolResult(success=False, error=<rendered errors.skill_input_validation>)`，
   模板变量：`skill_name`、`error_details`（逐字段：字段名 / 期望类型 / 实际收到值摘要）。
   模型看到清单后下一轮自纠。
4. 校验通过 → 继续 subagent/inline 既有流程。

### 3.3 派发装配（数据如何到达子代理）

- **落点选 task、不选 context**：pipeline 的"任务上下文"段对超长值截断到 500 字符
  （pipeline.py:219），且 system prompt 不应承载大文本；task 是既有的大文本载体（inline
  模式同款）。
- 组装规则（SkillTool 内，spawn 前）：

  ```
  {task}

  # 输入数据
  ## {field_name}
  {值：str 原样；dict/list json.dumps(ensure_ascii=False)；None 字段整段跳过}
  ```

- `# 输入数据` 段成为技能提示词的稳定取数坐标：各技能 md「取数」节改写为
  "使用任务中「# 输入数据」段的 document 文本"。
- inline 模式同构：数据段拼进 inline_instruction。

### 3.4 explicit_inputs 删除（D3）

- `SubAgentInput` 删字段；`orch.py` / `base.py` 的 `model_dump` 排除清单同步清理。
- 程序化调用方如需 ref→字段映射，直接在构造 input 对象前自行解析——框架不再承诺。

### 3.5 四技能输入模型

| 技能 | schema | 字段 |
|------|--------|------|
| content_audit | 新建 `schemas/content_audit.py` | `document: str`（$ref 直传，文本投影）；审核维度选择留在 task（属指令） |
| secret_analysis | 新建 `schemas/secret_analysis.py` | `document: str` |
| format_audit | 接上孤儿 `FormatAuditorInput`，md 补 `input_model:` 声明 | `document: str \| dict`（dict ← $ref:parse_document:N 完整 Document 模型）；`doc_type: str = "通知"` |
| plagiarism | 已声明，仅更新 document 字段描述措辞 | `document: str \| dict`、`library_docs`、`top_k` |
| full_government_audit | 新建最小 schema（当前 enabled: false，为将来启用备好） | `doc_type: str = "通知"`；文档经 file_path 透传给子技能（其子调用形态决定不设 document） |

### 3.6 提示词配套

- 领域 `orchestrator.yaml` 取数规则 4（zh/en 同步）改为双轨表述：
  "技能有结构化数据字段（如 document）→ $ref 直传字段；无该字段 → $ref 内嵌 task；
  【禁止】get_artifact 读回全文再粘贴。"
- 四技能 md「取数」节改写为按「# 输入数据」段取数；file_path 自取兜底条款保留。

## 4. 测试计划

| 层 | 用例 |
|----|------|
| registry | 字段名冲突 → 扫描错误、技能不注册 |
| SkillTool | schema 合并（document 字段与描述出现在 parameters）；校验失败回包（字段级清单、模板渲染）；校验通过后 spawn 的 task 含「# 输入数据」段与解析后文本；None 字段跳过 |
| 集成 | typed 字段传 $ref 经 ToolRegistry.execute 解析（复用 scoped_store 测试范式）；编排器侧请求保持精简（summary 而非全文） |
| 回归 | SubAgentInput 无 explicit_inputs；`uv run courtier validate-domain domains/docaudit/` 通过；四技能 input_model 可导入 |
| e2e | MockModelClient 两轮：content_audit(document=$ref) → 子代理收到全文；对照真实日志验证编排器上下文不再出现全文读回 |

## 5. 实施切分（逐任务提交）

| 任务 | 内容 | 提交 |
|------|------|------|
| T1 | 框架：schema 合并 + 冲突检测 + prompt_engine 注入 + errors.skill_input_validation（zh/en/fallback）+ 单测 | `feat(skills): typed input models drive SkillTool parameter schemas` |
| T2 | 派发装配：校验 + 「# 输入数据」段组装（subagent/inline 两路）+ 单测 | `feat(skills): validated typed fields assemble into subagent task` |
| T3 | 清理：删 explicit_inputs 及排除清单残留 | `refactor(agent): drop unimplemented explicit_inputs promise` |
| T4 | 迁移：四技能 schemas + md 取数节 + 领域取数规则 4（zh/en）+ validate-domain | `feat(docaudit): migrate all skills to typed structured inputs` |
| T5 | 文档：AGENTS.md §5.3/§7.1 更新 + 本文档偏差记录 | `docs(plans): record typed-input design decisions` |

依赖顺序：前置修复 → T1 → T2 →（T3 ∥ T4）→ T5。

## 6. 风险与开放问题

- **参数面膨胀**：每技能多 1–3 个字段，工具 schema 变大；可控（四技能合计 <10 字段）。
- **模型混淆双轨**：结构化字段与 $ref 内嵌并存期，取数规则的双轨表述是关键缓解；
  全迁完成后实际上只剩字段轨 + file_path 兜底轨。
- **format_audit 的 dict 投影**：anyOf 不做文本投影是有意行为（要完整 Document）；
  若实测发现子代理更受益于正文文本，改字段为 `str` 即可（一行 schema 变更）。
- **full_government_audit 处于 disabled 状态**：schema 先行备好，启用验证推迟到实际启用时。

## 7. 偏差记录

实施期间与本文档的偏离（均已落地并测试）：

1. **校验入参填充**（§3.2）：设计写"task 不入模型"；实现为满足 `SubAgentInput.task`
   必填约束，`model_validate({"task": task, **data_fields})` 用 task 参数值填充基类
   字段，但 task 仍不参与「# 输入数据」段装配——职责边界（D1）不变。
2. **无 schema 技能的未知参数**：设计未覆盖。实现为记录 warning 并忽略
   （保持旧行为），不静默丢弃也不报错。
3. **RESERVED_INPUT_FIELDS 迁移期含 explicit_inputs**：T1 先建保留集时包含它，
   T3 删除该字段后集合收敛为 {task, ref_ids, output_for}。
4. **format_audit 取数节新增 doc_type 说明**：编排方指定的文种与实际解析不一致时
   以实际解析为准——避免 doc_type 默认值误导子代理。
5. **Fix 4 分两步提交**：先落 $ref 内嵌 task 契约，T4 再升级为双轨表述（结构化字段
   优先）。最终态与本文档一致。
6. **相对导入层级教训**：skill.py 位于 courtier/agent/tools/builtin/，跨到
   courtier/prompts 需要 4 个点（`....prompts.engine`）；3 个点解析到
   courtier.agent.prompts（不存在）。已由测试钉住。
