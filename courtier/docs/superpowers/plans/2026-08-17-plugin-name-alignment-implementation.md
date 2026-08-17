# 插件目录/名称与工具名对齐（方向 A，8 个一次到位）实现计划

> **For agentic workers:** 实施本计划时按 phase 逐个 Task 推进，每个 Task 使用 checkbox（`- [x]`）语法跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交（feat/fix/refactor/docs/test/chore）提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 把 8 个单工具插件的目录名、`plugin.yaml` 的 `name:`、插件内 `pyproject.toml` 的 `[project] name` 统一对齐到工具名，消除"目录/插件进程名 ≠ 工具名"的三层错位，以及 content_audit/format_audit/plagiarism 三个插件名与同名**技能**的撞车。**工具名、技能名、库名一律不动**——工具名是 LLM 调用接口且嵌在 `$ref:<tool>:N` 引用里，技能名出现在 orchestrator 提示词与技能文档中，均零改动。`search` 插件（多工具）不动。

**Architecture:** 纯重命名重构：`git mv` 8 个插件目录 + 改三处名字（manifest name / pyproject project name / 测试与文档引用）。插件发现是动态的（`PluginScanner` glob `**/plugin.yaml`；`shared_plugin_names` 由 `plugins/shared/` 目录名派生，`app.py:180-189`），宿主代码无硬编码插件名，**核心零代码改动**；唯一按名匹配的是 `domains/docaudit/config/domain.yaml` 的 `requires_plugins`（`domain/loader.py:88-99` 以 manifest name 对账）。

**Tech Stack:** git mv、uv（各插件 lock 重生成）、pytest。

**关联文档:** 无前置计划依赖。背景：2026-08-17 用户指出"工具目录名与工具实际名对不上"（例：`plugins/docaudit/audit/content_audit/` 注册的工具是 `check_content`，而 `content_audit` 同时还是技能名、插件进程日志名 `.agent_logs/plugins/content_audit.log`）。

---

## 背景与现状问题

| # | 问题 | 证据 | 阶段 |
|---|------|------|------|
| 1 | 8 个单工具插件目录名/插件名 ≠ 工具名（对照表见下） | `plugins/` 目录 vs 各 tools.py | 0/1 |
| 2 | `content_audit`、`format_audit`、`plagiarism` 三个插件名与同名技能撞车：日志/对话中无法区分"技能子代理执行"与"插件进程" | `domains/docaudit/skills/*.md` vs `plugin.yaml name` | 0 |
| 3 | 插件内 `pyproject [project] name` 沿用旧语义名（如 `courtier-plugin-content-audit`） | 各插件 pyproject.toml | 0/1 |

**改名对照表（工具名不变，仅目录/插件名对齐工具名）：**

| 旧目录 | 新目录 | plugin.yaml name（新） | 工具（不变） |
|--------|--------|------------------------|--------------|
| `plugins/docaudit/audit/content_audit` | `.../audit/check_content` | `check_content` | check_content |
| `plugins/docaudit/audit/format_audit` | `.../audit/check_format` | `check_format` | check_format |
| `plugins/docaudit/audit/plagiarism` | `.../audit/detect_plagiarism` | `detect_plagiarism` | detect_plagiarism |
| `plugins/docaudit/audit/text_correction` | `.../audit/correct_text` | `correct_text` | correct_text |
| `plugins/docaudit/parse` | `.../parse_document` | `parse_document` | parse_document |
| `plugins/shared/anydoc` | `.../convert_document` | `convert_document` | convert_document |
| `plugins/shared/annotate` | `.../annotate_document` | `annotate_document` | annotate_document |
| `plugins/shared/template` | `.../load_template` | `load_template` | load_template |
| `plugins/shared/search` | **不动**（多工具插件按能力命名） | `search` | search_documents + read_chunks |

**目标形态：** 单工具插件的目录名 == 插件名 == 工具名；`.agent_logs/plugins/check_content.log` 等进程日志一眼可辨；技能名（content_audit 等）不再与任何插件撞车。

---

## 文件职责

| 对象 | 操作 | 阶段 |
|------|------|------|
| 8 个插件目录（上表） | `git mv`（含全部内容；`.venv`/`__pycache__` 随目录走，venv 需重建见 Phase 2） | 0/1 |
| 各 `plugin.yaml` | `name:` 改为新名（其余内容不动，`runtime.env` 注入等随文件迁移） | 0/1 |
| 各 `pyproject.toml` | `[project] name` 改为 `courtier-plugin-<新名>`（如 `courtier-plugin-check-content`）；相对路径依赖深度不变，无需改 | 0/1 |
| 各 `uv.lock` | 改 project name 后在每个插件目录 `uv lock` 重新生成 | 0/1 |
| `domains/docaudit/config/domain.yaml` | `requires_plugins` 改为 `[parse_document, load_template, search, annotate_document, detect_plagiarism, check_format, check_content, correct_text]` | 0 |
| `tests/plugin/`（test_audit_wrapper_plugins、test_parse_plugin、test_plagiarism_plugin、test_anydoc_plugin、test_annotate_plugin、test_template_plugin） | `_ensure_plugin_path("新名")` 与 `from plugins.<...>.<新名> import ...` 字符串替换 | 0/1 |
| `tests/courtier/test_prompt_single_source.py` | 无涉（引用的是 search 插件，不动） | — |
| 文档：`../AGENTS.md`（仓库布局树）、`CLAUDE.md`、`docs/plugin-development.md`、`docs/domain-package-guide.md`、`docs/agent/architecture.md`、`docs/agent/modules/artifact-system-guide.md`、`docs/architecture/plugin-skill-boundary.md` | 插件目录路径/名称同步更新 | 2 |
| `courtier/agent/runtime/activation.py:237` 注释 | "plugin names like \"parse\" differ from..." 更新为新名 | 2 |
| 历史计划/设计文档（docs/superpowers/plans、specs 下 2026-06-29 等） | **不改**（历史记录保持当时事实） | — |
| `domains/docaudit/config/prompts/**`、`domains/docaudit/skills/**` | **不改**（其中 format_audit/content_audit/plagiarism 指技能与工作流，非插件） | — |

> 已核实无硬编码插件名的宿主代码：`shared_plugin_names` 由目录动态派生（`app.py:180-189`）；`PluginScanner` glob 发现（`scanner.py:54-69`）；`ProcessManager` 以 manifest name 为键（随 yaml 自动更新）；`_SCOPE_SENSITIVE_TOOLS` 用工具名。技能文档/提示词中的旧词全部指技能，不属于本次改名对象。

---

## Phase 0：docaudit 域 4 个审计插件（含 domain.yaml 对账，风险最高先做）

### Task 0.1: audit/ 四插件改名 + domain.yaml + 测试

**Files:** 上表前 4 行 + `domains/docaudit/config/domain.yaml` + `tests/plugin/test_audit_wrapper_plugins.py` + `tests/plugin/test_plagiarism_plugin.py`

**依赖:** 无。

- [ ] **Step 1:** `git mv` 四个目录（audit/ 下，深度不变）。
- [ ] **Step 2:** 四个 `plugin.yaml` 的 `name:` 改新名；四个 `pyproject.toml` 的 `[project] name` 改 `courtier-plugin-check-content` / `-check-format` / `-detect-plagiarism` / `-correct-text`；各目录 `uv lock` 重新生成（lock 内 root project name 随之更新；本地路径依赖无需网络）。
- [ ] **Step 3:** `domain.yaml` `requires_plugins` 按新名更新。
- [ ] **Step 4:** 两个测试文件的 `_ensure_plugin_path(...)` 与 dotted import 全量替换旧名 → 新名（`test_audit_wrapper_plugins.py` 含 format_audit/text_correction/content_audit 相关导入，`test_plagiarism_plugin.py` 含 plagiarism）。
- [ ] **Step 5:** 回归：`uv run pytest tests/plugin/ -q` 全绿；`uv run courtier validate-domain domains/docaudit/` 通过（requires_plugins 对账即在此校验）。

### Task 0.2: 部署侧验证（本地 localhost:8000）

- [ ] **Step 1:** 后端重启后 `GET /health` 正常；`.agent_logs/plugins/` 出现 `check_content.log` 等新进程日志名；调用一次内容审核会话确认 check_content 工具仍可用（工具名未变，模型侧无感）。

## Phase 1：parse + shared 三插件

### Task 1.1: parse/anydoc/annotate/template 四目录改名 + 测试

**Files:** 上表 5-8 行 + `tests/plugin/test_parse_plugin.py`、`test_anydoc_plugin.py`、`test_annotate_plugin.py`、`test_template_plugin.py` + `domain.yaml`（parse → parse_document，Task 0.1 已改则此处只核对）

**依赖:** Task 0.1（domain.yaml 一次性改全亦可，实施时合并）。

- [ ] **Step 1:** `git mv` 四个目录；`plugin.yaml` name、`pyproject [project] name`（`courtier-plugin-parse-document` / `-convert-document` / `-annotate-document` / `-load-template`）、`uv lock` 同步。
- [ ] **Step 2:** 四个测试文件字符串替换。
- [ ] **Step 3:** 回归：`uv run pytest tests/plugin/ tests/courtier/ tests/agent/ -q` 全绿。
- [ ] **Step 4:** 确认 `shared_plugin_names` 派生正常：`convert_document`/`annotate_document`/`load_template` 进入共享集（grep app.py 逻辑为目录遍历，无需代码改动；如有一处 smoke：启动日志或单测断言）。

## Phase 2：文档同步与收尾

### Task 2.1: 文档与注释更新

**Files:** `../AGENTS.md`、`CLAUDE.md`（courtier）、`docs/plugin-development.md`、`docs/domain-package-guide.md`、`docs/agent/architecture.md`、`docs/agent/modules/artifact-system-guide.md`、`docs/architecture/plugin-skill-boundary.md`、`courtier/agent/runtime/activation.py`（注释）

- [ ] **Step 1:** 各文档中插件目录树/路径示例更新为新名；明确"单工具插件目录名 == 插件名 == 工具名；多工具插件按能力命名"的命名约定写入 `docs/plugin-development.md`。
- [ ] **Step 2:** activation.py:237 注释更新。

### Task 2.2: 插件 venv 重建 + 全量回归

- [ ] **Step 1:** 8 个目录内旧 `.venv`（随 git mv 物理位移，venv 不可重定位）删除并按现有 bootstrap 方式重建（宿主启动时的插件安装机制或文档约定流程）；确认 `proc.plugin_dir/.venv/bin/python` 可用。
- [ ] **Step 2:** `uv run pytest -m "not integration"` 全量绿；`validate-domain` 通过。

---

## 提交切分（约定式）

```
refactor(plugins): align docaudit audit plugin names with tool names    # 0.1
refactor(plugins): align parse and shared plugin names with tool names  # 1.1
docs(plugins): naming convention and path updates                       # 2.1
chore(plugins): rebuild plugin venvs after rename                       # 2.2（如 venv 不入库则无提交，记录即可）
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿；`uv run courtier validate-domain domains/docaudit/` 通过。
2. `grep -r "plugins/docaudit/audit/content_audit\|plugins/docaudit/audit/format_audit\|..."` 在 courtier/tests/plugins/domains/docs（历史 plans/specs 除外）零命中。
3. 部署烟测：重启后端，跑一次内容审核会话——工具调用名不变（check_content 等）、`.agent_logs/plugins/` 日志按新插件名落盘、`activate_domain` 返回的工具/技能列表与之前一致。
4. 技能零影响确认：`domains/docaudit/skills/`、`config/prompts/` 无 diff。

## 明确不做的事

- 工具名、技能名、入口类名（ParsePlugin/FormatAuditPlugin 等）不改——类名是纯内部符号，改了只增加 churn。
- `search` 插件不改名（多工具插件按能力命名是合理惯例）。
- 历史计划/规格文档（docs/superpowers/ 下的旧文档）不改写。
- 不动 libs/ 下库名（docparse/content_compliance/doccorrector 等）。
- 不做插件名注册表/别名兼容层——旧名一次性消亡（部署重启即切换）。

---

## 实施记录

（实施时逐 Task 填写：完成状态、与计划的偏差、实测数据。）
