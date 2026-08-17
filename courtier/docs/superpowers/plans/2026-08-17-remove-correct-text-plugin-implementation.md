# 移除 correct_text 插件，纠错能力迁入 content_audit 技能 实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 移除 `correct_text` 插件（原 `text_correction`），其文本纠错能力改为在 `content_audit` 技能中由提示词驱动的 LLM 子代理直接完成。同步清理依赖链：doccorrector 库（仅此插件使用）、CEC_* 环境配置与宿主环境白名单、插件测试与扩展管理测试断言、文档。历史日志中已有的 `$ref:correct_text:N` 引用不再可解析（不做兼容，与之前计划的处理方式一致）。

**Architecture:** 三层改动：技能层（`domains/docaudit/skills/content_audit.md` 纠错流程重写为提示词指引，frontmatter `tools:` 移除 correct_text）；插件与依赖层（`git rm -r plugins/docaudit/audit/correct_text/`；`git rm -r libs/docaudit/doccorrector/`；pyproject 依赖与 source、domain.yaml requires_plugins、manager.py 环境白名单、.env.example、config.py 的 cec_user_dict 默认值清理）；测试与文档层（test_audit_wrapper_plugins 的 text_correction 段、test_extension_admin 断言、doccorrector 测试目录、AGENTS/CLAUDE/架构文档）。核心零代码逻辑改动：插件发现动态、技能系统按 frontmatter 注册工具集。

**Tech Stack:** git rm、uv（宿主 lock 重生成）、pytest。

**关联文档:** `docs/superpowers/plans/2026-08-17-plugin-name-alignment-implementation.md`（刚把 text_correction 改名为 correct_text 并更新全部引用，本计划基于该最新状态）。此前 doccorrector 的修复历史（引号配对、errors/target 一致、拆分重试）随插件一并移除——纠错改由 LLM 子代理按提示词完成。

---

## 背景与现状问题

| # | 事项 | 证据 | 阶段 |
|---|------|------|------|
| 1 | correct_text 插件已改名为 correct_text 并验证可用，但用户决策改为移除该插件、用技能提示词实现纠错 | 用户指令 2026-08-17 | 0 |
| 2 | doccorrector 库仅被 correct_text 插件依赖，插件移除后成为死代码 | `plugins/.../correct_text/pyproject.toml`、宿主 `pyproject.toml:42,58` | 1 |
| 3 | CEC_* 环境变量与宿主白名单只为该插件存在 | `plugin.yaml runtime.env`、`manager.py:118-122`、`.env.example:63-68`、`config.py:276-286` | 1 |
| 4 | content_audit 技能 frontmatter `tools:` 与纠错流程引用 correct_text | `domains/docaudit/skills/content_audit.md:14,49-63` | 0 |
| 5 | 历史日志 `$ref:correct_text:N` 引用将不可解析 | `.agent_logs/sess_3cfae5b2ff0a` 等 | 收尾 |

**关键设计决策：**
- **技能提示词不预设任何专有名词结论**（如"两个维护"等规范术语不得写死在提示词里）——子代理只对输入文档负责。
- **纠错规则在技能提示词中定义**：错别字/重复字、全角字母数字→半角、半角标点→全角（弯引号开闭配对）、数字内小数点豁免（GB/T 15835-2011）——原规则引擎的语义以自然语言延续。
- **不修改 full_government_audit.md**（其 frontmatter `skills:` 只引用技能名，纠错已由 content_audit 技能承担）。
- **content_audit 技能 `tools:` 保留 `convert_document`**（文档取数仍需），只移除 `correct_text`。

---

## 文件职责

| 文件 | 操作 | 阶段 |
|------|------|------|
| `domains/docaudit/skills/content_audit.md` | frontmatter `tools:` 移除 `- correct_text`；「纠错流程」整段重写为提示词驱动（LLM 自行纠错，含规则定义、完整差异披露、复核排除要求） | 0 |
| `plugins/docaudit/audit/correct_text/` | `git rm -r`（整个插件：entry/tools/plugin.yaml/pyproject/uv.lock/.venv 不入库忽略） | 1 |
| `libs/docaudit/doccorrector/` | `git rm -r`（死代码） | 1 |
| `pyproject.toml`（宿主） | 移除 `doccorrector` 依赖（line 42）与 `[tool.uv.sources]` 条目（line 58）；`uv lock` 重生成 | 1 |
| `domains/docaudit/config/domain.yaml` | `requires_plugins` 移除 `correct_text`（列表改 7 项） | 1 |
| `courtier/plugin/manager.py` | 宿主环境白名单移除 `CEC_API_BASE/CEC_API_KEY/CEC_MODEL_NAME/CEC_MAX_LENGTH/CEC_USER_DICT`（line 118-122） | 1 |
| `courtier/config.py` | 删除 `cec_user_dict` Field（line 276-286，唯一引用了即将消失的 doccorrector 路径；确认无其他引用后删除） | 1 |
| `.env.example` | 移除 CEC_* 段（63-68 行）与相关注释 | 1 |
| `tests/plugin/test_audit_wrapper_plugins.py` | 删除 text_correction 三个测试（219-263 行附近） | 1 |
| `tests/agent/api/test_extension_admin.py` | 分组断言移除 `by_name["correct_text"]`（line 355） | 1 |
| `tests/domains/docaudit/doccorrector/` | `git rm -r`（随库移除） | 1 |
| `AGENTS.md`、`CLAUDE.md`、`courtier/CLAUDE.md`、`docs/agent/architecture.md`、`docs/plugin-development.md`、`docs/domain-package-guide.md` | 插件清单/目录树更新（移除 correct_text/text_correction 条目） | 2 |
| `libs/docaudit/` 目录本身 | 保留（docparse/validator/content_compliance/doccorrector 中前三个仍被依赖） | — |
| `.env`（真实配置） | **不改**（遗留 CEC_* 无害，用户可自行清理） | — |

---

## Phase 0：技能提示词重写

### Task 0.1: content_audit 纠错流程提示词化

**Files:** `domains/docaudit/skills/content_audit.md`

**依赖:** 无。

- [ ] **Step 1:** frontmatter `tools:` 移除 `- correct_text`（保留 `- convert_document`）。
- [ ] **Step 2:** 「纠错流程（文本纠错维度）」重写为提示词驱动：

  ```
  # 纠错流程（文本纠错维度）
  1. 基于任务中已获取的文档文本（convert_document 的 Markdown），由你直接逐段纠错——
     不调用纠错工具，纠错本身就是你的任务。
  2. 纠错规则（按此标准检查，不要套用外部规范文件的结论）：
     - **错别字/重复字**：音近形近错别字（如"惯彻"→"贯彻"）、缺字漏字、连续重复字（如"了了"）。
     - **全角/半角混用**：全角拉丁字母与全角数字应改为半角。
     - **标点语种混用**：中文文本中的半角标点改为全角；半角引号按开闭交替改为弯引号""和''；
       例外：数字内小数点（如5.8亿元）保留半角句点（GB/T 15835-2011），文件扩展名、
       代码/URL 等技术符号中的半角字符不改。
  3. 对发现的每一项错误，记录：错误位置（引用原文片段）、原始文本、纠正后的文本、
     操作类型（replace/delete/insert）、上下文（前后若干字）。
  4. 汇总返回结构化纠错结果：报告中的文本纠错表必须完整转写全部差异（含 delete 类删除操作）；
     删除操作在「纠正后」列标注「（删除）」，insert 类标注「（插入）」。对复核后确认不应改的
     条目单独列出并注明「复核排除」及依据，禁止静默丢弃。
  ```

  要点：规则以自然语言定义（弯引号配对、小数点豁免延续原引擎语义）；披露要求保持上一计划引入的"完整转写 + 复核排除"契约；不出现任何具体术语结论。
- [ ] **Step 3:** 「取数」段微调：当前第 3 条提到"调用 convert_document 获取 Markdown 作为审核文本"已满足纠错取数（LLM 直接对文本纠错），确认无需改动；「输出格式」节保持（含"文本纠错表必须覆盖全部差异"）。

**验收:** `uv run courtier validate-domain domains/docaudit/` 通过；技能渲染不含 correct_text 引用。

## Phase 1：插件与依赖移除

### Task 1.1: 移除 correct_text 插件与 doccorrector 库

**Files:** 见文件职责表 Phase 1 行

**依赖:** Task 0.1（技能先切换，避免空窗期技能引用不存在的工具）。

- [ ] **Step 1:** `git rm -r plugins/docaudit/audit/correct_text/`；`git rm -r libs/docaudit/doccorrector/`；`git rm -r tests/domains/docaudit/doccorrector/`。
- [ ] **Step 2:** 宿主 `pyproject.toml` 移除 doccorrector 依赖与 source；`uv lock` 重生成；`uv sync` 确认环境仍完整。
- [ ] **Step 3:** `domain.yaml requires_plugins` 移除 `correct_text`。
- [ ] **Step 4:** `manager.py` 白名单移除 5 个 CEC_* 变量；`config.py` 删除 `cec_user_dict` Field；`.env.example` 移除 CEC_* 段。
- [ ] **Step 5:** 测试清理：`test_audit_wrapper_plugins.py` 删除 text_correction 三测试；`test_extension_admin.py` 移除 correct_text 分组断言。
- [ ] **Step 6:** 回归：`uv run pytest -m "not integration"` 全绿；`validate-domain` 通过；`grep -rn "correct_text\|doccorrector\|CEC_"` 在 courtier/plugins/domains/libs/tests 中零命中（历史 .agent_logs 除外）。

## Phase 2：文档同步

### Task 2.1: 文档更新

- [ ] **Step 1:** `AGENTS.md`、`CLAUDE.md`、`courtier/CLAUDE.md` 插件清单与目录树移除 text_correction；`docs/agent/architecture.md` 插件表（line 599）与能力描述（"六大核心能力"中"文本纠错"能力表述改为由内容审核技能承担，而不是删掉能力本身）；`docs/plugin-development.md`/`docs/domain-package-guide.md` 示例引用更新。
- [ ] **Step 2:** 部署烟测：重启后端后跑一次内容审核会话——技能在无 correct_text 工具下完成 LLM 纠错并完整披露差异；`activate_domain` 返回的工具列表不再含 correct_text；无插件启动报错。

---

## 提交切分（约定式）

```
feat(skills): prompt-driven text correction in content_audit skill       # 0.1
refactor(plugins): remove correct_text plugin and doccorrector library   # 1.1
docs: drop text_correction from plugin inventories and architecture docs # 2.1
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；`validate-domain` 通过。
2. `grep` 清理验证零残留（见 Task 1.1 Step 6）。
3. 部署烟测：内容审核会话在纯提示词纠错下产出完整差异表；工具列表不含 correct_text。
4. 技能文档无死引用；frontmatter tools 与实际可用工具一致。

## 明确不做的事

- 不保留 correct_text 作为可选插件/开关——一次性移除（用户明确"去掉"）。
- 不做历史 `$ref:correct_text:N` 的解析兼容（与历史兼容策略一致）。
- 不改 `.env` 真实配置中的 CEC_*（部署配置由用户管理）。
- 不改 full_government_audit.md（只引用技能名）。
- 技能提示词不预设专有名词保护词表/术语结论（原 doccorrector 的 user_dict 概念不迁移——LLM 按语境自行判断）。
- 不改 libs/docaudit/ 其余库与 plugins/docaudit/ 其余插件。

---

## 实施记录

（实施时逐 Task 填写：完成状态、与计划的偏差、实测数据。）
