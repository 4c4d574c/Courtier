# WebUI 设置区样式统一 — 实施方案

## 背景与目标

设置区（AdminLayout 外壳下的 9 个视图 + 独立路由的 /profile）的样式是随页面各自生长的：同一 UI 概念存在多套 scoped 实现（弹窗 5 套、危险按钮 4 种写法、页内页签 3 种形态、状态消息 4 份复制 + 1 处裸奔 bug），且存在死类（`.endpoint-add`）、惰性规则（`.endpoint-remove` 的 hover-reveal）与硬编码色值（ProfileView `#e0dbd0`）。

目标：**深度统一** —— 每个概念收敛为一套全局规范外观，各页面向它看齐（用户已确认接受可见视觉变化）；`/profile` 不在管理壳内，纳入**浅度统一**（token 化、消息/按钮并入公共类，保留其衬线标题与居中卡片布局）。

对齐结论（已与用户确认，2026-09-04）：

1. 力度：深度统一（同概念强制同一外观，接受各页有可见变化）。
2. 范围：/profile 纳入浅度统一。
3. 争议外观的裁决：弹窗危险确认＝实心红（对齐用户管理/系统设置现状）；行内危险操作＝红描边 ghost；页内页签＝药丸式（资源库下划线改齐）；弹窗统一 460px / z-index 300；管理壳内内容宽度统一 1160px。

## 摸排结论（2026-09-04，三路侦察汇总）

基建：token 在 `theme.css`（`--chat-*` 前缀 + `[data-theme=dark]` 覆盖）；`admin.css` 经 `style.css` 全局加载（外壳/表格/徽章词汇表，362 行）；`extension.css` 被 SkillsView/SkillEditView/PluginMarketView 以 `<style scoped src>` 各编译一次（同一文件 3 份 scoped 副本）；其余视图各自持有 scoped 样式块（SystemSettings 一家 1307 行）。聊天组件全部使用带前缀类名（`tool-card-badge`、`confirm-btn`、`chat-sidebar-*` 等），与裸名全局类无冲突（已 grep 复核）。

同概念多套实现（抽象目标 → 收敛为）：

| 概念 | 现状 | 收敛为 |
|---|---|---|
| 弹窗 | UM/MM/RL scoped `.modal-*`（宽 400/420/460，z 200/300 各异）+ SystemSettings `.modal-backdrop` + ext `.ext-modal` | admin.css 全局 `.modal-backdrop/.modal/.modal-title/.modal-message/.modal-actions/.modal-btn(--primary/--danger)`；460px、z 300、宽变体 `.modal--wide` |
| 危险按钮 | 弹窗内实心红（UM/Profile/SS）vs 红描边 ghost（MM/RL）；行内删除 4 处同配方各自命名 | 弹窗确认＝`.btn.danger`/`.modal-btn--danger` 实心红；行内＝`.action-btn.danger-ghost` |
| 按钮 | `.action-btn`、`.ext-btn`、`.memory-btn`、`.resources-more-btn`、SS `.btn` 族 | 全局 `.btn(.primary/.ghost/.danger)`（SS 32px 族升级）；28px 行内＝`.action-btn`；`.ext-btn` 保留于 extension.css，数值对齐 |
| 页内页签 | 药丸×2（审批≡记忆逐字重复）、下划线×1（资源库）、分段选择、可选药丸 | 药丸 `.admin-tabs/.admin-tab(--active)`；分段选择与可选药丸为不同概念，保留本地 |
| 状态消息 | `msg-ok/msg-err` 逐字重复×3 + Profile 裸文本变体 + MemoryManageView 只用不定义（裸奔 bug） | admin.css 全局 `.msg-ok/.msg-err` 着色药丸 |
| 表单字段 | `.ext-field`、`.se-field`（近克隆）、`.modal-input`、资源库本地缩放 `.auth-field`、记忆页误用 auth 级 44px 输入框 | 全局 `.admin-field`（34px 控制台规格 + focus ring） |
| 开关 | SS `.switch/.track` ≡ ext `.ext-switch`（两套 38×21） | 全局 `.switch/.track/.switch-label/.switch-state`（SS 版） |
| 徽章 | admin.css `.badge-*`、ext `.ext-badge--*`、SS `.badge-db/dirty/restart/rebuild`、memory/resources 私有徽章 | admin.css `.badge` + 通用色变体 `.badge-ok/warn/err/dim/dirty` |
| 卡片/页头/搜索/空态 | 卡 5 处等价实现；页头 4 套；搜索 3 套；空态 4 种；`.admin-header` 局部×2 | 统一 `.admin-card/.admin-heading/.admin-subtitle/.admin-search/.admin-toolbar/.admin-empty(-state)`；新增 `.admin-header/.admin-section-title/.admin-back-link` |
| 内容宽度 | 1160/820/760/1240/440 | 管理壳内统一 `.admin-page` 1160px；Profile 保留 440 居中卡 |
| mono 字体 | 同一 font stack 硬编码 6+ 处 | theme.css 新增 `--font-mono` |

SystemSettings 文件内部去重（保持 scoped）：`.pool-box`≡`.field-input` 合并共享选择器；「＋ 添加接入点」死类按钮改用 `.tbl-add`；删惰性 `.endpoint-remove` 规则与死类 `.field-info`；`.badge-db`≡`.badge-dirty` 合并；test-card-head 内联样式类化。

## 统一后的可见变化（深度统一的代价）

- 我的记忆/资源库弹窗危险按钮：红描边 → 实心红。
- 资源库下划线页签 → 药丸页签。
- 我的记忆/资源库宽度 → 1160px，页面内二次滚动移除（滚动交给外壳 `.admin-content`）。
- 我的记忆弹窗输入框：auth 级 44px 大字 → 34px 控制台规格（顺带修 bug）。
- 我的记忆/资源库页内「← 返回主页」删除（侧栏已有同款）；技能编辑页「技能列表」保留改用 `.admin-back-link`；Profile 返回保留（不在壳内）。
- 状态消息统一着色药丸（Profile 从裸文本变更）；mono 字体走 token。
- Profile 浅度：`#e0dbd0`→token、4 处内联样式→类、消息/按钮并入公共类；保留衬线标题、居中卡片布局与 auth 级输入框。

## 任务分解（每任务一个 conventional commit）

- T0 本计划文档。
- T1 基线：浏览器对 9 页 × 明暗主题截「改造前」截图存 `.archdoc-build/style-audit/before/`；全量 grep 复核类名冲突。
- T2 admin.css 新增全局词汇；theme.css 加 `--font-mono`；style.css 全局 `@import extension.css`，删 3 处 `<style scoped src>`。
- T3 SystemSettings.vue：删与全局同名的 scoped 重复（`.badge` 族、`.switch` 族、`.modal-backdrop` 族，全局版按其现值定义，视觉零变）+ 文件内去重。
- T4 UserManagement + ApprovalManagement：弹窗/消息/危险按钮/页签迁全局。
- T5 MemoryManageView：全面迁移 + 修 msg 裸奔 + 双滚修复 + 宽度统一 + 删冗余返回链接。
- T6 ResourceLibraryView：同上 + 页签改药丸；上传拖拽区、可见性分段控件保留本地。
- T7 Skills/PluginMarket/SkillEdit：ext-modal/ext-switch/ext-badge → 全局类；se-field/se-card → `.admin-field/.admin-card`；宽 1240→1160。
- T8 ProfileView 浅度统一。
- T9 验证：`npm run build` + `npm test`；浏览器逐页（9 页 × 明暗）对照基线截图核对回归。

## 风险与对策

- 裸类名全局化（`.badge/.btn/.switch/.modal-*`）：实施前全量 grep 复核聊天组件无占用；删 scoped 副本时逐类 diff 数值，全局版以「多数派/系统设置现值」为准。
- 记忆/资源库去自滚动后需确认外壳滚动正常（长列表页重点核对）。
- Vite 长驻标签页热更可能报 ReferenceError——验收时强刷（已知环境坑）。

## 偏差记录

- 行内危险/肯定按钮命名：计划写 `.action-btn.danger-ghost`，实现为 `.action-btn--danger` / `.action-btn--ok`（与 `badge-*` 语义后缀风格一致）。
- 新增 `.modal--lg`（640px）、`.modal-backdrop--top`（顶对齐 + 60px 顶距）并给 `.modal` 加 `max-height: 84vh; overflow-y: auto`——承接技能/插件市场原 `ext-modal` 的高表单/日志弹窗行为；确认类弹窗仍为居中 460px。
- `.badge-auditor` 保留（角色语义），与新 `.badge-neutral` 合并为一条共享规则，避免重复声明。
- 计划外顺带修复：`main.ts` 启动即调用 `useTheme().initTheme()`——原先主题只在登录/注册/主页初始化，管理页硬刷新后 `data-theme` 永远不会被设置。
- extension.css 保留范围与计划一致（卡网格/域分组/chips/日志/`ext-btn`/`ext-error`/`ext-banner`/checklist），另新增 `.ext-domain-header .switch, .ext-card-header .switch { margin-left: auto }` 承接原 `ext-switch` 的推右布局。
- SystemSettings scoped `.badge` 基类删除后统一为全局 `.badge`（padding 1px 8px → 2px 9px），徽章垂直 padding +1px，视觉几乎无差。
- ProfileView：`.profile-msg` 一并删除（原计划倾向保留 16px 字号），状态消息完全走全局药丸。
- 验证阶段发现 Vite 文件监视丢事件导致 ResourceLibraryView 的 scoped 样式模块陈旧（dev 服务返回旧 CSS、模板却是新的）；touch 文件强制失效后恢复，与既有 inotify 限额问题相符，非本次代码问题。

## 实施记录（2026-09-04）

T0–T9 全部完成，共 9 个提交：`57eb559`（计划）→ `67d2869`（T2 全局词汇）→ `d15b5e8`（T3）→ `40f0a96`（T4）→ `5d1a9d6`（T5）→ `cbe77cc`（T6）→ `0a95ce3`（T7）→ `7543ddd`（T8）→ 本文档。`npm run build` 与 `npm test` 全绿；改造前后 9 页 × 明暗截图存于 `.archdoc-build/style-audit/{before,after}/`（工作区本地产物，不入库）。
