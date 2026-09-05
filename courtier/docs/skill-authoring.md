# Courtier 技能写作指南

## 概述

技能（skill）是带 YAML frontmatter 的 Markdown 文档，定义可复用的工作流。域激活后每个技能以 SkillTool 的形式暴露给编排器——模型调用技能名即触发派发，由 AgentRuntime 启动子代理执行（或按配置内联执行）。

技能放在域包的 `skills/` 目录（平铺扫描 `*.md`，不递归）。管理后台可以原地新建/编辑/启停技能文件；`activate_domain` 的技能目录按 mtime 缓存失效，且每次构建代理都会重新扫描注册——**改动在下一次请求即生效，无需重启**。

## 快速开始

以 `domains/docaudit/skills/format_audit.md` 为参照：

```markdown
---
name: format_audit
display_name: 格式审核
description: 政府公文格式审核
output_artifact_type: format_audit_result
type: skill
version: '1.0'
mode: sequential
timeout_seconds: 600
retry_policy: on_error
input_model: skills.schemas.format_audit.FormatAuditorInput
default_mode: inline
---

# 目标
你是文档格式审计专家，负责验证文档是否符合 GB/T 9704-2012 公文格式规范。

# 适用场景
- 文种为通知、报告、请示、函等党政机关公文
- 需要检查文档格式合规性

# 取数
1. 优先使用任务中「# 输入数据」段提供的 document……不要重复解析。
2. 若任务未提供 document 但有 file_path，调用 `parse_layout(file_path)`……

# 审核流程
1. ……
2. ……

# 输出格式
返回 markdown 形式的审核报告，需要用表格列举出现的问题。
- 如未发现问题，明确给出 "未发现明显格式问题" 的结论。
```

## Frontmatter 参考

| 字段 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `name` | 否 | 文件名（去扩展名） | 技能唯一标识（snake_case），同时是 SkillTool 的工具名 |
| `description` | 否 | 正文第一行（去 `#`） | 一句话摘要；进入 `activate_domain` 目录与工具描述 |
| `display_name` | 否 | — | 中文展示名（如「格式审核」），前端子代理树显示用 |
| `type` | 否 | `skill` | 必须是 `skill`，其他值报扫描错误 |
| `version` | 否 | `1.0` | 改变行为时递增；缺失会记录扫描错误 |
| `tools` | 否 | — | 本技能依赖的工具名清单。两个作用：inline 模式启动前检查可用性（缺工具直接报错并建议 subagent）；旧格式里也可填其他技能名（工具缺失时回退注入为 SkillTool） |
| `skills` | 否 | — | **子技能**显式清单（组合是选择性的）。列出的技能在该技能的子代理里可用（注入为 SkillTool）；未列出的技能不会被注入，防止技能互相调用失控。预算按代理链防环 |
| `tags` | 否 | — | 管理后台展示用标签 |
| `enabled` | 否 | `true` | 置 `false` 的技能不进激活目录、不注册 SkillTool |
| `default_mode` | 否 | 空 | `"subagent"` / `"inline"` / 空。设了值即钉死执行模式：`mode` 参数从工具 schema 里整体移除，模型无参数可选；留空则模型每次必须主动选 |
| `input_model` | 否 | — | Pydantic 输入模型的点分导入路径，见下文「结构化输入」 |
| `output_artifact_type` | 否 | — | 结果登记为该类型的产物（SkillTool 携带） |
| `mode` | 否 | `auto` | 协议字段：`sequential` / `parallel` / `auto`；非法值回退 `auto` 并记错误 |
| `output_schema` | 否 | — | 结果输出 schema（登记进工具注册表，供下游绑定/投影） |
| `timeout_seconds` | 否 | 600 | 声明的执行预算上限（管理后台展示；实际运行截止时间由 AgentRuntime 预算控制） |
| `retry_policy` | 否 | `none` | 声明的重试策略：`none` / `on_error` / `on_timeout`；非法值回退 `none` |

扫描是宽容的：单个文件解析失败只记录错误并跳过，不影响其他技能；重名技能后者覆盖前者并记警告。

## 执行模式

SkillTool 的内建参数：`task`（必填，交给技能的任务描述）、`file_path`（待审文档路径，从编排器上下文透传）、`ref_ids`（缓存引用白名单），以及数据字段（来自 `input_model`）。

- **`subagent`**：启动独立子代理异步执行，适合复杂/耗时任务。结果以 ExecutionResult 直通，附带 `subagent_run` 元数据，前端渲染进子代理树。
- **`inline`**：不派发子代理——把技能正文 + 任务注入当前代理的上下文，由它用自己已有的工具直接执行。启动前检查 `tools` 声明：缺工具立即失败并提示改用 subagent 模式（内联指令没有工具就是废纸）。适合简单/快速任务。

SkillTool 自带防环/防滥用的 `runtime_policy`：同一技能最多连续调用 3 次、总共 10 次。

## 结构化输入（input_model）

`input_model` 是一个**点分导入路径**（如 `skills.schemas.format_audit.FormatAuditorInput`），指向 Pydantic 模型。约定：

- 模型继承 `courtier.agent.agents.subagent.base.SubAgentInput`（基类字段：`task`、`output_for`、`ref_ids`）；
- 你声明在基类之上的**数据字段**才是技能的结构化参数——它们被投影进 SkillTool 的参数 schema（`Field(description=...)` 直接成为模型可见的参数说明），编排器可以直接把 `$ref:tool:N` 引用填进去；
- 数据字段不得与内建参数 `{task, mode, file_path, ref_ids}` 冲突（注册时报错）；`task`/`ref_ids`/`output_for` 是框架管线字段，永不出现在工具 schema 里；
- 派发前按模型校验入参，失败返回逐字段错误报告（模型可据此自纠重试）；校验通过后，数据字段组装成「# 输入数据」段附加在 task 末尾传给子代理——`None` 字段整体跳过，字符串原样传入（展开后的 `$ref` 全文正是在这里抵达），其他类型 JSON 序列化。**编排器因此从不粘贴整篇文档文本**。

`output_for` 是基类提供的可选引导：设置为目标下游工具名后，系统自动把该工具的输入 schema 注入子代理提示，引导其按格式输出。

```python
"""skills/schemas/format_audit.py"""
from pydantic import Field
from courtier.agent.agents.subagent.base import SubAgentInput

class FormatAuditorInput(SubAgentInput):
    """格式审计器的结构化输入。"""
    document: str | dict = Field(description="待审计的文档数据。可传入 $ref:parse_layout:1 引用")
    doc_type: str = Field(default="通知", description="要验证的文档类型（如：通知、函、请示）")
```

## 媒体输入（ImageRef / AudioRef / VideoRef）

技能可以声明媒体型数据字段：类型用 `courtier.agent.core.content_parts` 的 `ImageRef` / `AudioRef` / `VideoRef`（`Optional[XxxRef]` 也行）。

- 投影到工具 schema 时是普通字符串参数——模型填**当前会话媒体附件的 `file_id`**（裸字符串也接受，自动包装）；
- 派发时 SkillTool 把通过校验的媒体字段转成 MediaPart 附件挂到子代理的任务消息上——**媒体字节从不走任务文本**；「# 输入数据」段只留一行 `file_id` 标记；
- fail-closed 门控：子代理与主运行共用模型，该模型声明的 modalities 必须覆盖每种媒体类型，否则派发直接失败（`errors.media_model_unsupported`）；
- 正文里要写清「媒体已随消息内联、直接观察、不要调用工具去读」——参照 `visual_inspection` 技能。

## `$ref` 缓存引用

大体积工具结果（如解析出的版面模型、转换出的全文 Markdown）超过阈值时由宿主自动持久化，返回形如 `$ref:parse_layout:1` 的引用（可选第三段 `:字段名` 指定字段）。引用在**派发边界**递归展开：模型可以把它直接填进任何参数，字符串参数还会做文本适配（从包装对象里抽出 `markdown`/`text` 等正文），子代理拿到的是真实内容而非 JSON 信封。技能间传递数据的首选方式就是传 `$ref`（见 `full_government_audit` 的编排写法）；`ref_ids` 参数是显式白名单，限定子代理可用哪些引用。

## 技能正文

frontmatter 之后的 Markdown 正文**原样**作为子代理的系统提示词（不经过 Jinja 渲染，没有模板变量）；inline 模式下则整段注入当前代理的上下文。建议结构（docaudit 技能的既有惯例）：

- **目标**：一句身份定位 + 职责边界；
- **适用场景**：何时该调用本技能（帮助编排器选对技能）；
- **取数**：优先用「# 输入数据」段的数据，缺什么再调什么工具、失败怎么办——避免子代理重复解析大文档；
- **审核/执行流程**：具体步骤，写「做什么」，不写空泛原则；
- **输出格式**：明确的结构（markdown 报告 / 表格 / JSON），无发现时的结论句式也要定。

正文支持全部 Markdown。有一个高杠杆的写作约束：思考过程要求子代理「简要总结」，长篇分析既慢又挤上下文。

## 最佳实践

1. **具体优先**：给可执行的步骤和工具名，不给抽象指导；
2. **写死取数顺序**：先「# 输入数据」、再 `$ref`、最后自己调工具——三层兜底顺序在每个技能里保持一致；
3. **用 input_model 传数据**：让编排器填 `$ref` 引用而不是粘贴文本；
4. **定义输出格式**：给出现成结构，含「无问题」时的输出；
5. **一个技能一件事**：多维度任务用 `skills:` 组合子技能（`full_government_audit` 模式），自己只做编排与汇总；
6. **改行为必改版本**，并在管理后台核验目录里看到的是新描述。
