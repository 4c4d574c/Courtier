---
name: full_government_audit
display_name: 完整审核
description: 政府公文完整审核流程
skills:
  - format_audit
  - content_audit
  - plagiarism
output_artifact_type: full_government_audit_result
type: skill
version: "1.0"
mode: auto
timeout_seconds: 600
retry_policy: on_error
enabled: false

---

# 目标
对政府公文执行完整审核流程，涵盖格式、内容、风格、纠错、查重五大维度。

# 适用场景
- 用户要求完整审核一份政府公文
- 文件已上传且需要全面检查

# 执行流程
1. **子审核**：
   - `format_audit(task="执行格式审核", file_path=...)`
   - `content_audit(task="执行内容合规审核", file_path=...)`
   - `plagiarism(task="执行文档查重", file_path=...)`
2. 等待所有子审核完成后，汇总结果，返回综合审核报告。

注意：你只负责汇总和编排，不需要自己逐项分析文档内容。各子技能会返回结构化的审核结果。

# 输出格式
返回 markdown 形式的综合审核报告，按格式、内容、风格、纠错、查重五个维度组织，每个维度包含：
- `summary`: 该维度总体结论与问题数
- `issues`: 问题/纠错列表
- 最后给出总体结论与优先处理建议。

注意：最终报告中禁止出现工具名、skill 名、`$ref` 引用、artifact_id 等内部标识，统一使用自然语言描述。
