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
version: '1.0'
mode: sequential
timeout_seconds: 600
retry_policy: on_error
enabled: false
default_mode: subagent
---

# 目标
对政府公文执行完整审核流程，涵盖格式、内容合规、文本纠错、查重等维度（内容合规与文本纠错由 content_audit 技能一并承担）。

# 适用场景
- 用户要求完整审核一份政府公文
- 文件已上传且需要全面检查

# 执行流程
1. **子审核**：
   - `format_audit(task="执行格式审核", file_path=...)`
   - `content_audit(task="执行内容合规审核与文本纠错", file_path=...)`
   - `plagiarism(task="执行文档查重", file_path=...)`
2. 等待所有子审核完成后，汇总结果，返回综合审核报告。

注意：你只负责汇总和编排，不需要自己逐项分析文档内容。各子技能会返回结构化的审核结果。

# 输出格式
返回 markdown 形式的综合审核报告，按格式、内容合规、文本纠错、查重等维度组织，需要用表格列举出现的问题。
- 最后给出总体结论与优先处理建议。
