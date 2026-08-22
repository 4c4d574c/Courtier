# 插件 manifest 一致性统一（死配置清除 + 管理页运行时真相）实现计划

> **For agentic workers:** 按 Phase 推进，Task 内 Step 用 `- [x]` 跟踪，逐 Task 约定式提交。实施中发现与计划的偏差，记录到文末「实施记录」章节。

**Goal:** 消除插件 `plugin.yaml` 中宿主从不消费的声明式死配置（`capabilities.tools` / `capabilities.system_prompt`），将 8 个插件统一到 search 插件（commit `832dea7`）已确立的教义：manifest 只承载运行时配置，工具声明单一来源为 `tools.py` 类属性、system_prompt 单一来源为 `entry.py`；管理页工具清单改读运行时注册数据；并补齐管理页对 BLOCKED 插件的恢复能力（restart 前重扫 + 阻止原因透出）。
**Architecture:** 数据流向不变（SDK `_collect_capabilities` → `plugin.register` 通知 → `ExtensionRegistry`），只删死副本、换展示源。Phase 1 先给管理页接上运行时工具清单（此时 yaml 旧副本还在，行为零变化），Phase 2 才删 yaml 死配置——保证每个提交独立可发布。
**Tech Stack:** Python 3.12 + FastAPI + pytest；Vue 3 + TS（仅 Phase 4 小改）；Node assert 自定义脚本测试。
**关联文档:** 根因分析对话记录（2026-08-22）；search 清理先例 `832dea7` 与守卫测试 `tests/courtier/test_prompt_single_source.py`；开发指南目标形态模板 `courtier/docs/plugin-development.md:39-52`。

## 需求确认（2026-08-22 与用户对齐）

1. 统一方向：**向 search 教义收敛**——`plugin.yaml` 仅保留 `name/version/api/description/timeout_ms/dependencies/runtime.*`；不采用反向方案（让宿主改读 manifest 声明），因独立部署方向下远端插件的本地 manifest 本就不可信。
2. 管理页取舍：未连接（BLOCKED/DISCONNECTED/STOPPED）插件的条目、状态、操作按钮**全部保留**，仅嵌套工具清单显示空；不保留 yaml 死副本供展示。
3. 用户关切"页面如何重新激活/重连插件"已核实：DISCONNECTED 自动指数退避重连无需人工；两类 BLOCKED 今天就无法从页面恢复（见背景表 #7/#8）。据此将 **restart 前重扫** 与 **阻止原因透出** 纳入本计划范围（用户拍板："把它并进实施计划文档"）。

> **待拍板的四个取值**（方向已定，具体形态由计划拍板，审批时可否决）：
> a) `register_capabilities()` 返回形态统一为全量 dict `{"capabilities": [], "system_prompt": ...}`（SDK 文档化的正式形态；改动面：check_* 三家补键、search 从 `[]` 换 dict）。
> b) manifest 模型策略取过渡保守派：立即删除零使用的 `CheckerCapability/RouteCapability/ProcessorCapability` 三类；`Capabilities.tools/system_prompt` 保留解析但标注 deprecated（`extra="forbid"` 下立即全删会把仍带旧块的远端旧插件直接判 BLOCKED）；待独立部署切换后再整体移除。
> c) detect_plagiarism 补显式 `timeout_ms: 300000`（现默认 120s，查重大参考库可能不够；与其余 audit 插件量级一致）。
> d) 前端不为空工具清单加占位文案（chips 已有 `v-if` 空保护，静默留白即可）。

## 背景与现状问题

| # | 事实 | 证据位置 |
|---|------|----------|
| 1 | manifest 的 `capabilities.system_prompt` 全仓零消费者；`capabilities.tools[]` 唯一消费者是 admin 列表展示；yaml `input_contract` 无任何运行时读取 | `courtier/plugin/registry.py:53-97`（唯一活源）、`courtier/agent/api/routes/admin_extensions.py:57-64` |
| 2 | 工具注册与 system_prompt 只来自运行时通知：SDK 收集 `register_tool()` 工具实例 + `register_capabilities()` 返回值 | `libs/shared/plugin_sdk/src/courtier_plugin_sdk/runtime.py:618-638` |
| 3 | yaml 真正被消费的字段仅：name/version/api 校验、timeout_ms、dependencies 校验、runtime.port 兜底、runtime.env 注入 | `manager.py:311,414`、`plugin_sdk/runtime.py:856-893` |
| 4 | 双份文本已实际漂移：parse（yaml 含"仅限格式审核"限制，entry 版无，该规则现由 docaudit orchestrator workflow_rules 承载）、check_content（"内容合规规则" vs "文种规范"）、detect_plagiarism（结构不同） | 各 `plugins/**/plugin.yaml` vs 同目录 `entry.py` |
| 5 | search 先例：832dea7 发现 yaml 提示词从未到达宿主且已漂移，清理为 runtime-only 并加守卫测试 | `git show 832dea7`、`tests/courtier/test_prompt_single_source.py` |
| 6 | ExtensionRegistry 已按插件跟踪注册项（`_registrations`），ProxyTool 带 display_name/description | `courtier/plugin/registry.py:48,63-90`、`courtier/plugin/proxies.py:60-66` |
| 7 | 扫描期 BLOCKED（manifest 无效→manifest=None）点 restart 得 404：start_plugin 抛 KeyError；且 start/restart 用启动时缓存的 scan results，修好磁盘 manifest 不重启宿主不生效 | `manager.py:234,698-699` |
| 8 | 运行期 BLOCKED（缺 COURTIER_PLUGIN_ENDPOINTS 端点）页面无从得知原因（scanError 为空），点 start 也只会再次置 BLOCKED | `manager.py:254-260,708-711` |
| 9 | 前端已按运行时真相语义编写：chips 有空保护；availableTools 只收集 ACTIVE 插件的工具名 → Phase 1/2 零前端改动 | `webui/src/views/ExtensionManagement.vue:48-51,498-504` |
| 10 | 开发指南的 plugin.yaml 模板已是目标形态（runtime-only、无 capabilities）——文档同步工作量趋零 | `courtier/docs/plugin-development.md:39-52` |

## 目标契约（统一后）

- `plugin.yaml` = 部署运行时配置：标识、超时、依赖声明、监听端口、env 默认值。
- 工具元数据（name/display_name/description/parameters）= `tools.py` 类属性，经 `register_tool()` 上报，单一来源。
- system_prompt = `entry.py::register_capabilities()` 返回值，单一来源；跨工具行为规范归 core/domain 提示包（behavioral.yaml / workflow_rules）。
- 管理页工具清单 = 运行时注册快照；未注册即空。
- 守卫测试断言任何 `plugin.yaml` 不含 `capabilities:` 键。

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/plugin/registry.py` | 新增 `_tool_meta`（plugin → tool_name → {display_name, description}，register 时快照、unregister 时清除）+ `get_plugin_tool_summaries()` | 修改 | 1.1 |
| `courtier/agent/api/routes/admin_extensions.py` | `_plugin_items` 的 tools 改读 `get_plugin_tool_summaries()`；删除 `manifest.capabilities.tools` 分支 | 修改 | 1.1 |
| `plugins/shared/{anydoc,template,annotate}/plugin.yaml`、`plugins/docaudit/parse/plugin.yaml`、`plugins/docaudit/audit/{check_format,check_content,detect_plagiarism}/plugin.yaml` | 删除 `capabilities` 块（死配置）；parse 删除时核对 workflow_rules 已承载其路由限制 | 修改 | 2.1 |
| 各插件 `entry.py`（search、check_format、check_content、detect_plagiarism） | `register_capabilities()` 返回统一为 dict 形态（取值 a） | 修改 | 2.2 |
| `plugins/docaudit/audit/detect_plagiarism/plugin.yaml` | 显式 `timeout_ms`（取值 c） | 修改 | 2.3 |
| `tests/courtier/test_prompt_single_source.py` | search 专属断言泛化为遍历全部 `plugins/**/plugin.yaml` 断言无 `capabilities` 键；保留 behavioral 规则与 search runtime 断言 | 修改 | 3.1 |
| `courtier/plugin/manager.py` | 注入 PluginScanner；`restart_plugin` 在 stop 后重扫并合并 `_scan_results`；`start_plugin` 对 manifest=None 给出明确错误指引 | 修改 | 4.1 |
| `courtier/agent/api/routes/admin_extensions.py` | `_plugin_items` 增 `blockedReason`（VALID+BLOCKED+缺端点 → 指明缺 COURTIER_PLUGIN_ENDPOINTS 配置） | 修改 | 4.2 |
| `webui/src/api/client.ts`、`webui/src/views/ExtensionManagement.vue` | `PluginInfo.blockedReason?` 类型 + BLOCKED 卡片显示原因行 | 修改 | 4.2 |
| `courtier/plugin/manifest.py` | 删三类零使用 Capability 模型；`Capabilities` 标注 deprecated（取值 b） | 修改 | 5.1 |

## Phase 1：管理页工具清单改源运行时注册

### Task 1.1: registry 快照 + admin 路由切换

**Files:** `courtier/plugin/registry.py`、`courtier/agent/api/routes/admin_extensions.py`
**依赖:** 无

- [ ] **Step 1:** `ExtensionRegistry.__init__` 增 `self._tool_meta: dict[str, dict[str, dict[str, str]]]`；`on_register` 处理 tool cap 时快照 `{display_name, description}`（cap dict 直取，与 ProxyTool 同源）；`on_unregister` 弹出对应插件条目。
- [ ] **Step 2:** 新增 `get_plugin_tool_summaries() -> dict[str, list[dict]]`：返回 plugin → [{name, displayName, description}]（仅当前在册插件）。
- [ ] **Step 3:** `_plugin_items` 中 tools 改为 `summaries.get(name, [])`；删除 `manifest.capabilities.tools` 推导；其余字段（description/version/state/scanError）不变。
- [ ] **Step 4:** 测试：现有 admin/plugin 测试全绿；补一条——fake register 后列表含该插件工具元数据，unregister 后同插件 tools 为空。

## Phase 2：清除 yaml 死配置 + entry 形态统一

### Task 2.1: 删除 7 个插件 yaml 的 capabilities 块

**Files:** 见文件职责表第 3 行
**依赖:** Task 1.1（管理页已有新数据源，删除不影响展示）

- [ ] **Step 1:** 逐个删除 `capabilities` 块；`runtime`/`timeout_ms`/`dependencies` 及注释原样保留。
- [ ] **Step 2:** parse 特别核对：yaml 提示词中的"格式审核专用、内容任务禁用"路由限制确认由 `domains/docaudit/config/prompts/zh-CN/orchestrator.yaml`（workflow_rules 第 15/18 行区域）承载后再删；parse yaml 头部注释同步改为指向 entry.py 与 workflow_rules。
- [ ] **Step 3:** check_format/check_content 的 yaml `input_contract` 直接随块删除（全仓无消费者，模型实看 schema 来自 tools.py `parameters`）。

### Task 2.2: entry.py 返回形态统一

**Files:** `plugins/shared/search/entry.py`、`plugins/docaudit/audit/{check_format,check_content,detect_plagiarism}/entry.py`
**依赖:** 无（可与 2.1 同提交或紧随）

- [ ] **Step 1:** 四个文件 `register_capabilities()` 统一返回 `{"capabilities": [], "system_prompt": <原文>}`；search 的 prompt 取 ""，保留其"行为规范在 behavioral.yaml"注释。
- [ ] **Step 2:** `uv run pytest tests/plugin -k "entry or register or search"` 全绿（SDK 对两种形态均兼容，不应有破坏）。

### Task 2.3: detect_plagiarism 显式超时

**Files:** `plugins/docaudit/audit/detect_plagiarism/plugin.yaml`
**依赖:** Task 2.1

- [ ] **Step 1:** 增 `timeout_ms: 300000`（取值 c，审批可调）。

## Phase 3：守卫测试泛化

### Task 3.1: 全仓 manifest 死配置守卫

**Files:** `tests/courtier/test_prompt_single_source.py`
**依赖:** Task 2.1

- [ ] **Step 1:** 原 `test_search_manifest_declares_no_capabilities_or_prompt` 泛化为 `test_no_manifest_declares_capabilities`：`rglob plugins/**/plugin.yaml` 断言无 `capabilities` 键（错误消息保留"dead config"指引文案）。
- [ ] **Step 2:** search runtime 断言（port/env）与 behavioral zh/en 引用规则断言原样保留。

## Phase 4：管理页运维增强

### Task 4.1: restart 前重扫，修复"修了 manifest 也要重启宿主"

**Files:** `courtier/plugin/manager.py`
**依赖:** 无（与 Phase 1-3 无耦合）

- [ ] **Step 1:** ProcessManager 注入 `PluginScanner` 实例（构造参数，默认自建），`start_all` 改用注入实例。
- [ ] **Step 2:** `restart_plugin`：stop 后对该插件目录重扫一次，结果合并进 `_scan_results`（新结果覆盖同名；扫描消失的插件保留旧记录防列表闪空），再 `start_plugin`。效果：扫描期 BLOCKED 修复 manifest 后点 restart 即恢复。
- [ ] **Step 3:** `start_plugin` 中 `result.manifest is None` 的 KeyError 信息改为明确指引（"plugin.yaml 无效：<scanError>；修复后重试 restart"），admin 路由将其映射为 400（携带 scanError）而非 404"插件不存在"。
- [ ] **Step 4:** 测试：坏 manifest → BLOCKED → 磁盘修复 → restart → 连接循环重启（用 fake endpoint/monkeypatch 验证状态迁移与 scan results 更新）。

### Task 4.2: BLOCKED 原因透出到管理页

**Files:** `courtier/agent/api/routes/admin_extensions.py`、`webui/src/api/client.ts`、`webui/src/views/ExtensionManagement.vue`
**依赖:** Task 4.1（同一文件 admin_extensions，顺序提交避免冲突）

- [ ] **Step 1:** 后端 `_plugin_items` 增 `blockedReason: str = ""`：scanStatus=VALID 且 state=BLOCKED 且该插件不在 endpoints 表 → `"未在 COURTIER_PLUGIN_ENDPOINTS 配置端点，需修改 env 后重启宿主或在配置中补充端点"`。
- [ ] **Step 2:** 前端 `PluginInfo` 加可选字段；BLOCKED 且 blockedReason 非空时在卡片错误行（复用 `.ext-card-error` 样式位）展示原因；scanStatus=BLOCKED 的既有 scanError 展示不动。
- [ ] **Step 3:** `npm test` 全绿、`npm run build` 通过。

## Phase 5：manifest 模型收紧

### Task 5.1: 移除零使用模型，标注 deprecated

**Files:** `courtier/plugin/manifest.py`、`courtier/plugin/__init__.py`
**依赖:** Task 2.1（本仓 yaml 已无 capabilities）

- [ ] **Step 1:** 删除 `CheckerCapability`、`RouteCapability`、`ProcessorCapability`（全仓零引用；先 grep 复核含 tests）。
- [ ] **Step 2:** `Capabilities`/`ToolCapability` 与 `PluginManifest.capabilities` 字段保留解析，docstring 标注 deprecated（过渡期兼容仍带旧块的远端旧 manifest；独立部署切换后整块移除，届时 `__init__.py` 导出同步清理）。
- [ ] **Step 3:** `uv run pytest tests/plugin tests/courtier -m "not integration"` 全绿。

## 实施记录

（待实施后回填：各 Task 提交号、与计划的偏差及原因。）
