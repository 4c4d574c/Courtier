# 统一 ref 解析与自动化工具绑定设计

**日期**: 2026-06-07
**状态**: Draft
**范围**: Agent Harness — CacheStore、ArtifactStore、SubAgentTool、ToolRegistry

## 背景

多轮 HTTP session 运行审计流程时，格式审计子代理反复失败。通过诊断脚本和日志分析，
识别出 5 个问题（P0-P2），根因指向两个架构缺陷：

1. **CacheStore 与 ArtifactStore 不同步** — 同一份工具输出数据在两个系统中各自维护，
   ArtifactStore 在多轮 rehydrate 后保留数据，CacheStore.ref_map 每轮重建为空。
2. **LLM 被当作数据中继器** — 子代理的 LLM 需要手动传递 `$ref` 字符串给工具，
   但 LLM 不擅长数据搬运（类型搞错、ID 搞错、凭空编造 ref）。

**核心设计原则**：数据通过系统流转，LLM 只负责决策。

## 诊断验证

通过 `scripts/diagnose_subagent_ref.py` 确认：

- **Scenario A**（同轮 session）：CacheStore.ref_map 有 `$ref:parse_document:1` →
  `resolve_refs` 成功 → dict → 子代理正常工作
- **Scenario B**（多轮 session）：CacheStore.ref_map 为空（新实例）→
  `resolve_refs` 失败 → 仍是 str → 子代理 system prompt 显示 `$ref` 字符串 →
  LLM 传给 audit_format → 报错 "document 参数必须是 dict"

## 设计

### 第一节：rehydrate 时同步 CacheStore.ref_map

**问题**：`_rehydrate_artifact_store()` 扫描 prior messages 中的
`__persisted_output__` 标记，重建 ArtifactStore。但 CacheStore.ref_map
没有同步重建，导致 `resolve_refs()` 在后续轮次找不到 ref。

**修复**：在 `_rehydrate_artifact_store()` 中同时恢复 CacheStore.ref_map。

```python
# routes.py
def _rehydrate_artifact_store(artifact_store, messages, cache_store=None):
    for msg in messages:
        ...  # 扫描 __persisted_output__ markers
        artifact_store.register_cached_ref(ref_id=ref_id, ...)

        # 新增：同步恢复 cache_store 的 ref_map
        if cache_store is not None:
            cache_store.ref_map[ref_id] = filepath
```

调用处传入 cache_store：

```python
# routes.py — runner() 内
cache_store = context_manager._cache
_rehydrate_artifact_store(artifact_store, prior_state.messages, cache_store=cache_store)
```

**影响文件**：`src/agent/api/routes.py`

### 第二节：input_contract 自动绑定

**问题**：子代理 LLM 需要 `list_artifacts` → `get_artifact` →
再调 `audit_format`，浪费 turns。且 LLM 手动传 `$ref` 字符串容易出错。

**修复**：给需要 parsed document 的工具添加 `input_contract` 声明，让
ToolRegistry 自动从 ArtifactStore 绑定参数。

```python
# tools.py
class AuditFormatTool:
    input_contract = InputContract(
        tool_name="audit_format",
        required=[
            ContractField(
                param_name="document",
                artifact_type="docaudit.parsed_document",
            )
        ],
    )
    ...
```

效果：
- LLM 调用 `audit_format(doc_type="通知")`（不传 document）→ registry 自动绑定
- LLM 仍可显式传 `document`（显式参数优先于自动绑定）
- 子代理 system prompt 中 `document` 字段改为显示 `(已自动绑定，无需手动传递)`

**需要添加 input_contract 的工具**：

| 工具 | 参数 | artifact_type |
|------|------|--------------|
| `audit_format` | document | `docaudit.parsed_document` |
| `audit_content` | document | `docaudit.parsed_document` |
| `correct_text` | document | `docaudit.parsed_document` |
| `detect_document_type` | title | 需要从 parsed_document 投影提取 |

**影响文件**：`src/agent/skills/format_audit/tools.py`、
`src/agent/skills/content_audit/tools.py`、
`src/agent/skills/correction/tools.py`、子代理 prompt 模板

### 第三节：Harness 层自动类型转换

**问题**：LLM 把 `rules: []` 序列化为 `"[]"`（字符串），
Pydantic 校验拒绝。任何模型都可能出现此问题。

**修复**：在 `_SubAgentTool._execute_structured()` 的 Pydantic 校验前，
根据字段类型做自动 str→list/dict 转换。

```python
# subagent.py
def _coerce_kwargs_to_model(kwargs: dict, model_cls: type) -> dict:
    """对 Pydantic 模型中 list/dict 类型的字段，自动从 JSON 字符串转换。"""
    coerced = dict(kwargs)
    for name, field in model_cls.model_fields.items():
        if name not in coerced:
            continue
        value = coerced[name]
        if not isinstance(value, str):
            continue
        ann = field.annotation
        if _annotation_expects_type(ann, (list, dict)):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (list, dict)):
                    coerced[name] = parsed
            except (json.JSONDecodeError, ValueError):
                pass
    return coerced
```

边界：
- 只对 `str → list/dict` 转换，不做其他类型强制转换
- 转换失败保持原值（让 Pydantic 报原有错误）
- 适用于所有 `SubAgentInput` 子模型

**影响文件**：`src/agent/agents/subagent.py`

### 第四节：子代理 turn 预算与 prompt 优化

**问题**：子代理 4 turn 预算不够完成"探索 artifact → 获取数据 → 执行审计"。

**修复**：

1. **提升子代理默认 max_turns：4 → 6**

2. **优化子代理 system prompt**：

   当前：
   ```
   工具结果可能保存为 $ref 缓存引用。构造下游工具参数时，优先直接把业务 $ref 传给目标工具...
   ```

   改为：
   ```
   审计工具的核心参数（如 document）已通过系统自动绑定，无需手动传递。
   直接调用审计工具即可（如 audit_format(doc_type="通知")），不要先探索 artifact。
   如果审计工具调用失败，再使用 list_artifacts / get_artifact 排查。
   ```

3. **`list_artifacts` 的 role/subject 模糊匹配**：

   LLM 传入 `role="document"`, `subject="current"`，但实际是
   `role="primary_document"`, `subject="current_upload"`。

   在 `list_artifacts` 的过滤逻辑中增加子串匹配：
   ```python
   # "document" 匹配 "primary_document"
   # "current" 匹配 "current_upload"
   if role and not artifact.role == role:
       if role not in artifact.role:  # 子串匹配
           continue
   ```

**影响文件**：子代理配置、prompt 模板、artifact store 的 list 过滤逻辑

### 第五节：Orchestrator 误用 artifact_id 的防御

**问题**：LLM 把 `__persisted_output__` 的 `ref_id`
（如 `$ref:get_artifact:1`）误当作 `artifact_id` 传给 `get_artifact`。

**修复**：

1. **`get_artifact` 增加防御性校验**：

   ```python
   if artifact_id.startswith("$ref:get_artifact:"):
       return ToolResult(
           success=False,
           error=(
               f"'{artifact_id}' 是工具输出的持久化标记，不是有效的 artifact_id。"
               f"请先调用 list_artifacts() 获取可用的 artifact_id。"
           ),
       )
   ```

2. **Orchestrator system prompt 强化**：

   ```
   get_artifact 的 artifact_id 必须来自 list_artifacts 返回的 artifact_id 字段。
   不要使用工具返回的 ref_id（如 $ref:get_artifact:N）作为 artifact_id。
   ```

**影响文件**：`src/agent/skills/common/artifact_tools.py`（或 get_artifact 所在文件）、
Orchestrator prompt 模板

## 改动总结

| 文件 | 改动 |
|------|------|
| `src/agent/api/routes.py` | rehydrate 时同步 cache_store.ref_map |
| `src/agent/skills/format_audit/tools.py` | AuditFormatTool 添加 input_contract |
| `src/agent/skills/content_audit/tools.py` | AuditContentTool 添加 input_contract |
| `src/agent/skills/correction/tools.py` | 相关工具添加 input_contract |
| `src/agent/agents/subagent.py` | 添加 _coerce_kwargs_to_model 自动类型转换 |
| Artifact list 过滤逻辑 | role/subject 模糊匹配 |
| `get_artifact` 实现 | 防御性校验 |
| Prompt 模板 | 子代理和 Orchestrator 指引优化 |

## 测试计划

1. **单元测试**：为每节修复编写独立测试
   - rehydrate 同步 ref_map 的测试
   - input_contract 自动绑定的测试
   - str→list/dict 类型转换的测试
   - list_artifacts 模糊匹配的测试
   - get_artifact 防御校验的测试
2. **集成测试**：用诊断脚本验证 Scenario A 和 Scenario B 都能正确解析 ref
3. **端到端验证**：通过 API 发起多轮审计请求，验证格式审计能成功完成
