# doccorrector 结果一致性与 get_artifact 投影修复 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。Phase 间有依赖（见各 Task「依赖」行），Phase 内 Task 相互独立。

**Goal:** 修复 2026-08-16 真实会话日志（`.agent_logs/sess_f80a6ed0fe12`，内容审核+文本纠错任务）暴露的两组缺陷——(a) `correct_text` 结果内部不一致（errors 列表与 target 文本互相矛盾）及其上游规则误报（引号无开闭配对、数字小数点被当标点混用），最终报告与工具产物不同步；(b) `get_artifact` 对 `$ref` 产物静默丢弃 `materialize_as`/`source_scope` 投影约束导致 44KB 解析 JSON 重复注入上下文 3 次（132K prompt tokens 处理 1KB 正文），以及 `outline=true` 对 `parse_document` 产物必然失败且错误信息误导。不改变 doccorrector 对外 JSON 字段契约、不改变 get_artifact 的 raw-read 默认行为、不动前端协议。

**Architecture:** 改动分布三层：纠错库层（`libs/docaudit/doccorrector/`：规则修复 + 构造性一致化，errors 与 target 由同一应用算法生成）；宿主工具层（`courtier/agent/tools/builtin/get_artifact.py`：`$ref` 投影目标类型推断、结构化读取支持 parsed_document 形状）；提示与技能层（`behavioral.yaml` outline 指引限定文本型结果、`content_audit.md` 强制完整差异披露、correct_text 工具描述更新）。链路保持：correct_text 插件 → doccorrector 库 → content_audit 技能（LLM 汇总）→ 用户报告。

**Tech Stack:** Python 3.12+、uv、pytest；不涉及 LLM/ES/MinIO 改动（doccorrector 单测用 mock inferencer，不发起真实 CEC 调用）。

**关联文档:** `docs/superpowers/plans/2026-08-16-search-fixes-and-utilization-implementation.md`（同日另一条修复线，两者无文件交集）。证据会话：`courtier/.agent_logs/sess_f80a6ed0fe12/`（turn_002 的 outline 失败、turn_004 的静默投影丢弃、turn_007 的 errors/target 不一致、turn_008 的报告漏报）。

---

## 背景与现状问题

以下问题编号贯穿全文，证据为 2026-08-16 会话日志逐轮分析与对应源码核对结论：

| # | 问题 | 证据位置 | 阶段 |
|---|------|----------|------|
| 1 | `correct_text` 结果 21 条 errors 中仅 8 条体现在 target：阶段二 4B 模型输出形成 target，阶段三规则检测（全角半角、标点混用等）只追加进 errors、从不应用到 target，errors 与 target 结构性矛盾 | 日志 turn_007 `tool_executions.json`（9 处引号、2 处小数点只在 errors 不在 target）；`doccorrector/corrector.py:574-593`（规则 errors 追加）、`corrector.py:76-104`（target 仅由模型 diff 产生） | 1 |
| 2 | `_detect_punctuation_mixing` 将 `"` 一律映射为 `「`、`'` 一律映射为 `『`，无开闭交替：9 处引号全部建议改成左括号，若应用将产生 `「加快建设数字中国「`；公文规范应为弯引号 `“”` | `corrector.py:268-283`（`hw_to_fw` 映射表） | 0 |
| 3 | 同一函数将 `.`→`。` 无差别映射，把 "5.8亿元"、"3.5亿元" 的数字小数点误报为标点混用（GB/T 15835-2011 规定数字内小数点用半角句点），本次靠 agent 人工在报告中排除 | 日志 turn_007 errors 第 19/20 条、turn_008 报告第 4 项；`corrector.py:269` | 0 |
| 4 | 4B 模型删除 5 处标题前句号并合并一段（已应用于 target），最终报告未披露任何删除操作——报告与 target 不同步，用户按报告改文档会漏改 | 日志 turn_007 target 与 turn_008 报告对照；`domains/docaudit/skills/content_audit.md:48-63`（无"完整披露差异"要求） | 2 |
| 5 | `get_artifact` 对 registry 命中的 `$ref` 且未传 artifact_type 时直接返回原始数据，`materialize_as`/`source_scope` 被静默丢弃：模型请求 string 物化正文，收到与上一轮一字不差的 44KB JSON → 同一解析结果在上下文注入 3 次（turn_004 1 次、turn_005 2 次），turn_005 prompt 膨胀至 48K tokens | 日志 turn_004 `tool_executions.json` 与 turn_003 结果逐字相同；`get_artifact.py:216-225`；`behavioral.yaml:25` 明令模型不得自行传 artifact_type，投影路径对模型不可达 | 3 |
| 6 | `outline=true` 对 `parse_document` 产物必然失败：`_structured_read` 只认 str 或含 `markdown/text/content/plain_text/chunk_text/data` 顶层字段的 dict，parsed_document 文本嵌套在 `pages[].page_content.body.{title,main_text}`；错误信息「不含可解析的文本内容」与事实相反（产物含 44KB 文本）；behavioral.yaml 指引未限定产物类型 | 日志 turn_002 失败；`get_artifact.py:325-342`、`courtier/agent/core/cache_store.py:873`（`_TEXT_FIELD_PRIORITY`）、`behavioral.yaml:28` | 4 |

**目标形态：** errors 与 target 构造性一致（`target == apply(source, errors)`）；规则无误报（引号开闭配对、小数点豁免）；报告强制完整转写 source→target 全部差异；`get_artifact` 对 `$ref` 产物在收到投影参数时推断目标类型并真正投影（推断失败则明确报错，永不静默返回原始数据）；`parse_document` 产物支持大纲/按节读取，不支持时错误信息如实说明。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `libs/docaudit/doccorrector/doccorrector/corrector.py` | 规则修复（引号配对、小数点豁免）；新增 `_apply_errors` 与一致性重构；`_resolve_conflicts` 增补重叠消解 | 修改 | 0/1 |
| `tests/domains/docaudit/doccorrector/test_corrector.py` | doccorrector 库级单测（一致性不变量、规则误报回归、重叠消解） | **新建** | 0/1 |
| `plugins/docaudit/audit/text_correction/tools.py` | correct_text 工具 description 增补「target 为已应用全部修正的完整文本，errors 为完整差异」契约说明 | 修改 | 2 |
| `domains/docaudit/skills/content_audit.md` | 纠错流程增补「完整转写 source→target 差异（含 delete 操作），疑似误报须标注复核排除及依据」 | 修改 | 2 |
| `courtier/agent/tools/builtin/get_artifact.py` | `$ref` 双分支投影目标类型推断；`_structured_read` 文本提取支持 parsed_document 形状；错误信息如实化 | 修改 | 3/4 |
| `courtier/agent/artifacts/projectors.py` | 抽纯函数 `parsed_document_to_text(data, source_scope)`（现有 `_parsed_document_to_paragraph_list` 逻辑复用），供投影器与 `_structured_read` 共用 | 修改 | 4 |
| `tests/agent/tools/test_get_artifact.py` | `$ref`+约束推断投影、无约束 raw-read 回归、parsed_document 大纲读取、错误信息用例 | 修改 | 3/4 |
| `courtier/prompts/defaults/zh-CN/behavioral.yaml` | outline 指引限定文本型结果并补 parsed_document 取数指引（en-US 镜像同步） | 修改 | 4 |
| `courtier/prompts/defaults/en-US/behavioral.yaml` | 同 zh-CN | 修改 | 4 |

> 契约约束（不变）：doccorrector 对外条目保持 `original/corrected/position/operation/context` 五字段；`get_artifact` 无约束参数时的 raw-read 默认行为与返回结构不变；`behavioral.yaml:25`「不要自行添加 artifact_type」规则不变（推断在工具内部完成）。

---

## Phase 0：规则误报修复（必须最先合入——Phase 1 会把规则 errors 应用到 target，误报修复是前提）

### Task 0.1: 引号开闭配对转换

**Files:** `libs/docaudit/doccorrector/doccorrector/corrector.py`、`tests/domains/docaudit/doccorrector/test_corrector.py`

**依赖:** 无。

- [ ] **Step 1:** `_detect_punctuation_mixing` 中 `"`/`'` 的映射改为带开闭状态机（弯引号，符合 GB/T 15834 与公文惯例）：

  ```python
  # 半角 → 全角 映射（引号按出现序开闭交替，另处理）
  hw_to_fw = {",": "，", ".": "。", ";": "；", ":": "：", "!": "！", "?": "？",
              "(": "（", ")": "）", "[": "【", "]": "】", "<": "《", ">": "》"}
  _QUOTE_PAIRS = {'"': ("“", "”"), "'": ("‘", "’")}

  def _detect_punctuation_mixing(text: str) -> list[dict]:
      errors: list[dict] = []
      quote_state: dict[str, bool] = {'"': False, "'": False}   # False=期望开引号
      for i, ch in enumerate(text):
          if ch in _QUOTE_PAIRS:
              open_q, close_q = _QUOTE_PAIRS[ch]
              corrected = open_q if not quote_state[ch] else close_q
              quote_state[ch] = not quote_state[ch]
          elif ch in hw_to_fw:
              corrected = hw_to_fw[ch]
          else:
              continue
          ...
  ```

  其余标点保持现有逐字符检测与 `context` 构造方式不变。
- [ ] **Step 2:** 小数点豁免——`.` 前后均为 ASCII 数字（`text[i-1].isdigit() and text[i+1].isdigit()` 且为半角）时跳过，不产生 error：

  ```python
  if ch == "." and i > 0 and i + 1 < len(text):
      prev, nxt = text[i - 1], text[i + 1]
      if prev.isascii() and prev.isdigit() and nxt.isascii() and nxt.isdigit():
          continue   # 数字内小数点（GB/T 15835-2011），非标点混用
  ```

- [ ] **Step 3:** 单测（mock inferencer 返回原文恒等，直接调 `ErrorCorrect.infer`）：
  - (a) `"加快建设数字中国"和"一网通办"` → 断言 errors 依次为 `“`、`”`、`“`、`”`（开闭交替，跨多对连续状态正确）；
  - (b) `'单引号'` → `‘`、`’` 交替；
  - (c) `人民币5.8亿元、3.5亿元。` → 断言不存在 position 落在两个小数点上的 error；
  - (d) 普通句号场景（`。` 后的半角 `.` 如 `结束.`）仍正常报 `.`→`。`（豁免条件不误伤句末英文句点）。

**验收:** 新增单测全绿；`uv run pytest tests/plugin/test_audit_wrapper_plugins.py` 无回归。

---

## Phase 1：errors 与 target 构造性一致

### Task 1.1: target 由最终 errors 应用生成 + 重叠消解

**Files:** `libs/docaudit/doccorrector/doccorrector/corrector.py`、`tests/domains/docaudit/doccorrector/test_corrector.py`

**依赖:** Phase 0（规则无误报后应用才安全）。

- [ ] **Step 1:** 新增纯函数（patch 应用，errors 按 position 升序、单次遍历 source）：

  ```python
  def _apply_errors(source: str, errors: list[dict]) -> str:
      """按 errors 列表把修正应用到 source，构造最终 target。

      errors 须已按 position 升序且区间两两不重叠。单次从左到右应用，
      偏移量随已应用编辑的长度差累计，position 语义始终基于原 source。
      """
      parts: list[str] = []
      cursor = 0          # source 中下一未消费位置
      for e in errors:
          op = e.get("operation", "replace")
          pos = e.get("position", 0)
          original = e.get("original", "")
          corrected = e.get("corrected", "")
          parts.append(source[cursor:pos])
          if op == "insert":
              parts.append(corrected)
              cursor = pos
          else:
              parts.append(corrected)                 # delete 时 corrected=""
              cursor = pos + len(original)
      parts.append(source[cursor:])
      return "".join(parts)
  ```

  实现要点：errors 按 position 升序遍历；`insert` 在 `pos` 处插入 `corrected` 且 `original` 视为空，`delete` 跳过 `len(original)` 字符，`replace` 用 `corrected` 替换 `original`；无重叠前提保证单遍应用正确。
- [ ] **Step 2:** `ErrorCorrect.infer` 重构为「最终 errors → 应用生成 target」：

  ```python
  def infer(self, input_list: list[str]) -> list[dict]:
      # 阶段二：模型纠错（diff 误差先行）
      results = res_format(input_list, self.inferencer.infer(input_list))
      for item in results:
          source = item["source"]
          model_errors = item["errors"]                      # 模型 diff，权威
          # 阶段三：规则候选（重复字/全角半角/标点混用/截断人名等，现状不变）
          rule_errors = (_detect_duplicate_chars(source) + _detect_fullwidth_chars(source)
                         + _detect_punctuation_mixing(source) + _pycorrector_check(source)
                         + _find_truncated_forms(source, self._protected_words))
          # 阶段四：重叠消解——模型已改的位置规则不再重复报（模型 diff 优先）
          resolved = _resolve_conflicts(model_errors, source, self._protected_words,
                                        _build_protected_ranges(source, self._protected_words))
          resolved += [e for e in
                       _resolve_conflicts(rule_errors, source, self._protected_words,
                                          _build_protected_ranges(source, self._protected_words))
                       if not _overlaps_any(e, model_errors)]
          resolved.sort(key=lambda e: e.get("position", 0))
          # 阶段五：构造性一致——target 是 errors 的确定性应用结果
          item["errors"] = resolved
          item["target"] = _apply_errors(source, resolved)
      return results
  ```

  新增 `_overlaps_any(e, refs)`：区间 `[position, position+len(original or corrected))`（空区间按 `position` 单点）与 refs 任一区间相交即 True。
- [ ] **Step 3:** 冲突消解函数 `_resolve_conflicts` 与 `_protect_corrected_text` 保持现状（保护词逻辑不变，仍作用于各自候选集）。
- [ ] **Step 4:** 一致性不变量单测（mock inferencer）：
  - (a) 恒等模型 + 含引号/小数点/错字的 source → `_apply_errors(source, errors) == target`；
  - (b) 模型 diff 与规则在同一位置冲突（如模型把某半角 `,` 改为 `，`，规则同位置也报 `,`→`，`）→ 最终 errors 该位置仅 1 条、值取模型侧，target 无双重应用；
  - (c) 模型修正位置 353（惯→贯）与规则引号位置互不重叠 → 全部保留且应用正确；
  - (d) delete/insert/replace 三种 op 混合（含 position 0 与文末）应用结果与手工构造 target 逐字相等；
  - (e) 回归：仅模型修正（规则零命中）时 target 与 res_format 原 target 一致。

**验收:** 一致性单测全绿；`tests/plugin/test_audit_wrapper_plugins.py` 与既有插件链路无回归。用日志 turn_006 的真实输入（907 字请示正文）离线重放（mock CEC 返回日志中的模型输出）→ 断言 errors 与 target 一致、引号全部成对、无小数点误报。

---

## Phase 2：报告契约与技能说明

### Task 2.1: correct_text 工具契约说明

**Files:** `plugins/docaudit/audit/text_correction/tools.py`

**依赖:** Phase 1（契约定型后写说明）。

- [ ] **Step 1:** `description` 增补契约："返回的每条 results 中，`target` 为已应用全部 `errors` 修正后的完整文本（`errors` 即 source→target 的完整差异，含 delete/replace/insert）；`errors` 与 `target` 必然一致，可直接以二者其一为准转写修正清单。"

### Task 2.2: content_audit 技能完整差异披露要求

**Files:** `domains/docaudit/skills/content_audit.md`

**依赖:** Phase 1。

- [ ] **Step 1:** 「纠错流程」第 2 步增补两条：
  1. "报告中的文本纠错表必须完整转写 `errors` 列表（source→target 的全部差异），**包括 delete 类删除操作**；禁止只挑部分错误汇报。删除操作在「纠正后」列标注「（删除）」并在上下文列展示被删字符前后文。"
  2. "对疑似规则误报（如数字内小数点、成对引号中已被模型处理的位置）可不采纳，但必须在报告中单独列出并注明「复核排除」及依据（如 GB/T 15835-2011 条文），禁止静默丢弃。"
- [ ] **Step 2:** 「输出格式」节同步注明纠错表需覆盖全部差异。

**验收:** 人工核验技能渲染结果包含上述要求（`uv run courtier validate-domain domains/docaudit/` 通过）。

---

## Phase 3：get_artifact $ref 投影目标类型推断

### Task 3.1: 投影约束不再被静默丢弃

**Files:** `courtier/agent/tools/builtin/get_artifact.py`、`tests/agent/tools/test_get_artifact.py`

**依赖:** 无（与 Phase 0-2 独立）。

- [ ] **Step 1:** 新增模块级辅助：

  ```python
  # materialize_as → 推荐目标类型（源类型自有默认物化方式匹配时优先自身类型）
  _MATERIALIZE_TARGET_HINTS = {
      "string": "core.plain_text",
      "list_string": "docaudit.paragraph_list",
      "list_dict": "docaudit.paragraph_list",
      "dict": None,   # dict 目标 = 源类型自身
  }

  def _infer_projection_target(source_artifact_type: str, materialize_as: str | None) -> str | None:
      """由源类型 + 期望物化方式推断投影目标类型；推断不出返回 None。"""
      if materialize_as in (None, _default_materialize_as(source_artifact_type)):
          return None                       # 默认物化：走 raw-read 老路径
      if materialize_as == "dict":
          return source_artifact_type
      return _MATERIALIZE_TARGET_HINTS.get(materialize_as)
  ```

  说明：`source_scope/max_chars/normalize_whitespace` 等约束仅在投影路径生效；出现任一约束参数时也视为投影请求（`materialize_as` 为 None 时按源类型自身物化 + 约束传递）。
- [ ] **Step 2:** `$ref` registry 命中分支（`get_artifact.py:216-225`）改为：无 `artifact_type` 且存在任一投影参数时 → `target_type = _infer_projection_target(artifact.artifact_type, materialize_as)`；推断成功则调用 `_project_artifact(artifact_type=target_type, ...)`（`_project_artifact` 内部 `artifact_store.get(artifact_id)` 命中同一 registry 产物，源类型为真实注册类型，投影链可解析）；推断失败或投影解析失败 → 按 `_project_artifact` 现有错误机制返回明确错误（不再返回原始数据）。**无任何投影参数时行为与现状完全一致（raw-read）。**
- [ ] **Step 3:** `_resolve_ref` 持久化分支（`get_artifact.py:437-453` 的「No artifact_type」块）同规则：投影参数存在时先经 `_find_artifact_for_ref` 取真实类型产物推断目标类型；推断不出时返回明确错误（提示"该引用产物类型不支持所请求的物化方式，可省略 materialize_as/source_scope 直接读取原始数据"）。
- [ ] **Step 4:** 单测 `test_get_artifact.py` 增补：
  - (a) 注册 `docaudit.parsed_document` 产物（pages[].page_content.body.{title,main_text} 形状），`get_artifact(id=$ref, materialize_as="string", source_scope="body")` → 断言返回 `core.plain_text` 物化的正文字符串（含 main_text 内容、不含 header/footer），数据量显著小于原始 dict；
  - (b) 同产物 `materialize_as="string"` + 无 source_scope → full_document 语义（title+body）；
  - (c) 无投影参数的 `$ref` 读取 → 返回原始 dict（raw-read 回归）；
  - (d) 推断失败路径（如对纯 dict 产物请求 `materialize_as="list_string"` 且无投影链）→ `success=False` 且 error 含可操作指引，不返回原始数据；
  - (e) `source_scope` 单独出现（materialize_as=None）→ 走投影路径且 `source_scope` 生效。

**验收:** 新增单测全绿；既有 `test_get_artifact.py`、`test_get_artifact_defense.py` 全量无回归。

---

## Phase 4：structured read 支持 parsed_document + 指引收敛

### Task 4.1: parsed_document 文本提取复用投影器逻辑

**Files:** `courtier/agent/artifacts/projectors.py`、`courtier/agent/tools/builtin/get_artifact.py`、`tests/agent/tools/test_get_artifact.py`

**依赖:** 无（可与 Phase 3 同批合入，同文件注意顺序）。

- [ ] **Step 1:** `projectors.py` 抽出纯函数：

  ```python
  def parsed_document_to_text(data: dict, source_scope: str | None = None) -> str:
      """parsed_document 数据 → 按阅读序拼接的正文文本（title + main_text）。

      与 _parsed_document_to_paragraph_list 的提取路径完全一致（不复制逻辑，
      后者改为调用本函数的段落版或两者共享内部提取器）；source_scope=body
      时仅取 section=body 的段落，缺省取 title+body。
      """
  ```

  实现：把 `_parsed_document_to_paragraph_list` 的 `pages[].page_content.body.{title,main_text}` 遍历逻辑下沉为该函数内部提取器，投影器与 `_structured_read` 共用，避免两处漂移。
- [ ] **Step 2:** `_structured_read`（`get_artifact.py:325-342`）dict 分支在 `_TEXT_FIELD_PRIORITY` 全部不命中后，增加 parsed_document 形状探测（顶层含 `pages` 列表且 `pages[].page_content` 为 dict）→ 调 `parsed_document_to_text(data)` 得文本，继续走 `parse_sections` 大纲/按节逻辑。
- [ ] **Step 3:** 错误信息如实化（`get_artifact.py:336-342`）：仍失败时返回：

  ```
  f"结果 {id} 不支持大纲/按节读取：仅文本类结果（Markdown/纯文本）与 parse_document 解析结果可用。
  可直接 get_artifact 读取原始数据，或加 materialize_as=string 提取正文文本。"
  ```
- [ ] **Step 4:** 单测：(a) parsed_document 形状 dict（含「一、」「（一）」标题段）→ `outline=true` 返回 outline 含对应节；(b) `section="一、项目背景"` 返回该节文本；(c) 非文本 dict（如 search_results 形状）→ 返回如实错误信息（含「不支持大纲」字样，不再出现「不含可解析的文本内容」）；(d) str/带 markdown 字段 dict 的既有路径回归。

### Task 4.2: behavioral.yaml outline 指引限定范围

**Files:** `courtier/prompts/defaults/zh-CN/behavioral.yaml`、`courtier/prompts/defaults/en-US/behavioral.yaml`

**依赖:** Task 4.1（能力先落地再写指引）。

- [ ] **Step 1:** zh-CN 第 28 行改为：

  ```
  · 读取长文档/长结果时，先用 get_artifact 的 outline=true 获取大纲，再用 section 参数按节读取，避免全文回读或盲翻页（适用于文本类结果；parse_document 解析结果如需正文，优先 materialize_as=string + source_scope 提取，无需先取大纲）。
  ```
- [ ] **Step 2:** en-US 第 30 行镜像同步。
- [ ] **Step 3:** `tests/courtier/test_prompt_single_source.py` 增补断言：渲染 behavioral bundle 含 `materialize_as=string` 与 `source_scope` 关键词（防漂移护栏沿用该文件模式）。

**验收:** 单测全绿；PromptEngine 渲染 zh-CN/en-US 各一次无异常。

---

## 提交切分（约定式，逐 Task 一提交）

```
fix(doccorrector): pair quote conversions with open/close alternation          # 0.1
fix(doccorrector): exempt decimal points from punctuation-mixing detection     # 0.1（Step 2 可并入或独立）
refactor(doccorrector): derive target by applying resolved errors              # 1.1
test(doccorrector): consistency invariant and rule-regression unit tests       # 0.1/1.1 测试可随功能提交或单列
docs(tools): document correct_text errors/target contract                      # 2.1
docs(skills): content_audit full-diff disclosure and false-positive review     # 2.2
fix(tools): infer projection target for typed $ref in get_artifact             # 3.1
feat(tools): outline/section reading for parsed_document artifacts             # 4.1
docs(prompts): scope outline guidance to text-type results                     # 4.2
```

> 注：doccorrector 属独立安装包（`libs/docaudit/doccorrector/`），其测试落在 `tests/domains/docaudit/doccorrector/`（宿主测试目录，库包内不放测试，与 `libs/` 约定一致）；测试与功能同提交（test 前缀惯例见 `courtier/CLAUDE.md`，实施时按其最新约定微调切分）。

## 总验收

1. `uv run pytest -m "not integration"` 全绿；新增 `tests/domains/docaudit/doccorrector/test_corrector.py` 与 `test_get_artifact.py` 增量覆盖上表各 Task 验收项。
2. 日志重放验证（离线、mock CEC 与解析结果，不依赖真实 LLM）：
   - turn_006 的 correct_text 输入 → errors 与 target 一致、无小数点误报、引号成对；
   - turn_004 的 `get_artifact(id=$ref:parse_document:1, source_scope=body, materialize_as=string)` → 返回正文字符串（非 44KB dict）；
   - turn_002 的 `get_artifact(id=..., outline=true)` 对 parsed_document 产物 → 成功返回大纲。
3. 真实会话烟测（可选，需 LLM 环境）：同场景上传文档执行内容审核+纠错，确认 (a) 上下文不再重复注入解析 JSON；(b) 最终报告包含全部删除类修正；(c) 报告不存在小数点误报条目。
4. `uv run courtier validate-domain domains/docaudit/` 通过。

## 明确不做的事

- 不改 4B CEC 模型行为（如"标题前删句号"的判断），此类模型侧编辑靠 Phase 2 的完整差异披露兜底；若后续评估认为需约束，另行在 CEC 提示词立项。
- 不改 doccorrector 对外条目字段契约（不新增必填字段；如需溯源可在后续计划加可选 `detector` 字段）。
- 不改 `behavioral.yaml:25`「不要自行添加 artifact_type」规则（推断在 get_artifact 内部完成，模型调用面不变）。
- 不动 `get_artifact` 无约束参数时的 raw-read 默认行为与持久化存储格式。
- 不动 search/citations 链路与前端协议（属 2026-08-16 search 计划范畴）。
- 不引入对 parse_document 结果的缓存/摘要策略调整（44KB 首次 raw-read 是否值得截断属独立优化，需权衡格式审核工具对完整结构的依赖）。

---

## 实施记录（2026-08-16）

全部 Task 已按提交切分完成并逐项提交（5 个提交：`a45b51e` `066dc44` `b496826` `8b6783a` `8f0bfc6`）。与原计划的偏差与实测数据：

1. **Task 3.1 目标推断实现方式**：计划草稿中的静态 `_MATERIALIZE_TARGET_HINTS` 推断表改为「候选表 + ProjectionResolver 可行性验证」——`_infer_projection_target` 逐个尝试与 materialize_as 匹配的候选目标类型，取第一个能被解析器从源类型投影到的。等价满足验收且不硬编码类型表；仅约束参数（materialize_as=None）时只试 `core.plain_text`，不可解析即明确报错（计划原文「按源类型自身物化 + 约束传递」存在约束被静默丢弃的口子，已收紧为更强的不静默契约）。
2. **测试提交切分**：测试随对应功能同提交（而非单列 test 提交），与库内既有惯例一致。
3. **日志重放验证（离线，无真实 LLM 依赖）**：
   - turn_006（correct_text，907 字输入 + 日志中的 4B 模型输出重放）：errors 21→19 条（2 条小数点误报消失）；`target == apply(source, errors)` 一致；引号全部成对且已应用到 target；模型侧编辑（惯→贯、基处→基础设施、分号并列、句号删除）全部保留。
   - turn_004（get_artifact `$ref` + materialize_as=string + source_scope=body）：返回 7154 字正文字符串（原为 44KB 原始 dict 静默回传）。
   - turn_002（outline=true 对 parse_document 产物）：返回 27 节大纲（原必然失败）。
4. **重放中发现的额外事实（超出原计划问题清单）**：真实文档正文为 110 段 / 7154 字，而原始会话中 orchestrator 粘贴进 content_audit task 的文本仅 907 字，且**不是真实正文的子序列**（模型在上下文被 44KB JSON 污染后重建了截断版文档）。原报告的 3 条「仅有标题无正文」warning 均为假阳性——真实文档中这些章节有内容；correct_text 也只纠了截断文本。修复的价值不只是 token 节省，而是审核准确性。
5. **全量回归**：`uv run pytest -m "not integration"` → 1593 passed, 6 skipped, 22 deselected；`uv run courtier validate-domain domains/docaudit/` 通过；behavioral.yaml 两语种 YAML 解析正常。
