---
name: nested_skill
description: 嵌套 Skill 测试
tools:
  - echo
skills:
  - valid_skill
---

# 目标
这是一个用于测试嵌套调用的 Skill。

# 流程
1. 调用 echo 工具回显输入
2. 委托 valid_skill 执行子任务
