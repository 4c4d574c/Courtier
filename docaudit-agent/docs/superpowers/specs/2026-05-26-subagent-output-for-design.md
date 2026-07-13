# SubAgent output_for 参数设计

**日期**: 2026-05-26
**状态**: 已确认

## 概述

为子代理工具 `_SubAgentTool` 增加 `output_for` 参数，允许父代理在调用子代理时指定下游工具的输入 schema，子代理将结果格式化为下游工具可直接使用的输入参数。

## 场景

当工具调用结果被持久化缓存后，下一轮工具调用不能直接使用缓存格式时，
由子代理读取缓存数据、按目标工具 schema 转换格式、返回可直接使用的数据。
大数据的格式转换在子代理内部完成，原始大数据不进入父代理上下文。

## 数据流

```
父代理 loop:
  Step 1: LLM 调用 tool_A → 返回大数据 → loop 持久化
          → 父代理收到 {"__persisted_output__": true, "ref_id": "ref_xxx"}

  Step 2: LLM 需要调用 tool_B，但缓存格式不匹配
          → 调用子代理:
              run_format_auditor(
                task="读取缓存并转换为 tool_B 所需格式",
                output_for="tool_B"
              )

          子代理内部:
              1. read_cached_output("ref_xxx") → 读取原始数据
              2. LLM 按 tool_B schema 转换/提取
              3. 返回格式化后的 JSON

  Step 3: 父代理收到子代理格式化的数据 → 直接传给 tool_B
```

## 实现

### subagent.py

**SubAgentRunner 新增**:
- `_parent_tool_schemas: dict[str, dict]` — 父代理工具的 input_schema 映射
- `register_parent_schema(name, schema)` — 注册单个 schema

**_SubAgentTool 变更**:
- `parameters` 增加 `output_for` 可选字段（string）
- `execute()` 中如果 `output_for` 有值，查找 schema 并拼接到 task 中，告诉子代理按此格式输出
- `_extract_result_data` 透传 `__persisted_output__` 引用

### orch.py

`__init__` 中注册父代理专属工具（如 `read_cached_output`）的 parameters schema 到 runner。

### loop.py

无变更。

### 子代理 task 增强格式

```
【输出格式要求】
你的结果将作为工具「tool_B」的输入参数。
请在完成任务的最终回复中，输出一个 JSON 对象，
字段与类型需匹配以下 schema：
```json
{...schema...}
```
只输出 JSON，不要包含其他说明文字。
```
