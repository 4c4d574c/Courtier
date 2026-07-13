# 统一 ref 解析与自动化工具绑定 — 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复多轮 session 中子代理 `$ref` 解析失败、LLM 类型错误、artifact 探索浪费 turns 等 5 个问题。

**架构：** 在 Harness 层统一 ref 解析路径（CacheStore.ref_map rehydrate + ArtifactStore fallback），给审计工具添加 `input_contract` 实现自动参数绑定，在 Pydantic 校验前增加类型强制转换兜底。

**技术栈：** Python 3.12+, Pydantic v2, pytest

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `src/agent/api/routes.py` | `_rehydrate_artifact_store()` 同步恢复 `cache_store.ref_map` |
| `src/agent/skills/format_audit/tools.py` | `AuditFormatTool` 添加 `input_contract`（自动绑定 `document`） |
| `src/agent/skills/content_audit/tools.py` | `AuditContentTool` 添加 `input_contract`（自动绑定 `document`） |
| `src/agent/skills/text_correction/tools.py` | `CorrectTextTool` 添加 `input_contract`（自动绑定 `texts`） |
| `src/agent/agents/subagent.py` | 新增 `_coerce_kwargs_to_model()` 自动 str→list/dict 转换 |
| `src/agent/tools/builtin/list_artifacts.py` | `role`/`subject` 过滤增加子串匹配 |
| `src/agent/tools/builtin/get_artifact.py` | 增加防御性校验：拒绝 `$ref:get_artifact:*` 作为 artifact_id |
| `src/agent/agents/orch.py` | Orchestrator 角色 prompt 强化 get_artifact 使用规则 |
| `tests/agent/test_routes_rehydrate.py` | rehydrate 同步 ref_map 的单元测试 |
| `tests/agent/test_subagent_coerce.py` | str→list/dict 自动转换的单元测试 |
| `tests/agent/tools/test_list_artifacts_fuzzy.py` | list_artifacts 模糊匹配的单元测试 |
| `tests/agent/tools/test_get_artifact_defense.py` | get_artifact 防御校验的单元测试 |
| `scripts/diagnose_subagent_ref.py` | 集成验证脚本（已存在，任务完成后运行验证） |

---

## 任务 1：rehydrate 时同步 CacheStore.ref_map

**文件：**
- 修改：`src/agent/api/routes.py:192-243`
- 修改：`src/agent/api/routes.py:388-389`
- 创建：`tests/agent/test_routes_rehydrate.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/test_routes_rehydrate.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.agent.api.routes import _rehydrate_artifact_store
from src.agent.core.cache_store import CacheStore


@pytest.fixture
def mock_artifact_store():
    store = MagicMock()
    store.register_cached_ref = MagicMock()
    return store


@pytest.fixture
def cache_store(tmp_path):
    return CacheStore(cache_dir=str(tmp_path))


def test_rehydrate_syncs_cache_ref_map(mock_artifact_store, cache_store, tmp_path):
    """_rehydrate_artifact_store should populate cache_store.ref_map."""
    # Prepare a prior message with __persisted_output__ marker
    filepath = tmp_path / "test_parse_document_1.json"
    filepath.write_text('{"user_id": "u1", "doc_id": "abc"}', encoding="utf-8")
    
    msg_content = {
        "data": {
            "__persisted_output__": True,
            "ref_id": "$ref:parse_document:1",
            "file": str(filepath),
            "size_chars": 50,
            "content_type": "application/json",
        }
    }
    
    from src.agent.core.state import Message
    messages = (Message(role="tool", name="parse_document", content=json.dumps(msg_content)),)
    
    assert "$ref:parse_document:1" not in cache_store.ref_map
    
    _rehydrate_artifact_store(
        artifact_store=mock_artifact_store,
        messages=messages,
        cache_store=cache_store,
    )
    
    assert "$ref:parse_document:1" in cache_store.ref_map
    assert cache_store.ref_map["$ref:parse_document:1"] == str(filepath)
    mock_artifact_store.register_cached_ref.assert_called_once()


def test_rehydrate_without_cache_store_does_not_crash(mock_artifact_store, tmp_path):
    """Passing cache_store=None should still work (backward compat)."""
    filepath = tmp_path / "test.json"
    filepath.write_text('{}', encoding="utf-8")
    
    msg_content = {
        "data": {
            "__persisted_output__": True,
            "ref_id": "$ref:parse_document:1",
            "file": str(filepath),
        }
    }
    
    from src.agent.core.state import Message
    messages = (Message(role="tool", name="parse_document", content=json.dumps(msg_content)),)
    
    _rehydrate_artifact_store(
        artifact_store=mock_artifact_store,
        messages=messages,
        cache_store=None,
    )
    
    mock_artifact_store.register_cached_ref.assert_called_once()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/test_routes_rehydrate.py -v
```
预期：FAIL — `TypeError: _rehydrate_artifact_store() got an unexpected keyword argument 'cache_store'`

- [ ] **步骤 3：修改 `_rehydrate_artifact_store` 接收 cache_store 参数**

```python
# src/agent/api/routes.py:192
# 修改函数签名，添加可选的 cache_store 参数
def _rehydrate_artifact_store(
    artifact_store: ArtifactStore,
    messages: tuple,
    cache_store: Any = None,
) -> None:
```

并在函数体内 `artifact_store.register_cached_ref(...)` 调用后添加：

```python
        # 新增：同步恢复 cache_store 的 ref_map
        if cache_store is not None and ref_id and filepath:
            cache_store.ref_map[ref_id] = filepath
```

- [ ] **步骤 4：修改调用处传入 cache_store**

```python
# src/agent/api/routes.py:388-389
# 在 runner() 内，_rehydrate_artifact_store 调用前获取 cache_store
cache_store = getattr(context_manager, '_cache', None) if context_manager else None
_rehydrate_artifact_store(
    artifact_store, prior_state.messages, cache_store=cache_store
)
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/agent/test_routes_rehydrate.py -v
```
预期：PASS（2 tests passed）

- [ ] **步骤 6：Commit**

```bash
git add src/agent/api/routes.py tests/agent/test_routes_rehydrate.py
git commit -m "fix: rehydrate cache_store.ref_map alongside artifact_store

Multi-turn sessions lose $ref resolution because ArtifactStore is
rehydrated from prior messages but CacheStore.ref_map is not.
This causes tools like audit_format to receive raw $ref strings
instead of resolved dicts.

- Add optional cache_store parameter to _rehydrate_artifact_store()
- Populate cache_store.ref_map from __persisted_output__ markers
- Pass context_manager._cache at call site in runner()"
```

---

## 任务 2：AuditFormatTool 添加 input_contract

**文件：**
- 修改：`src/agent/skills/format_audit/tools.py`
- 创建：`tests/agent/skills/format_audit/test_input_contract.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/format_audit/test_input_contract.py
import pytest
from src.agent.skills.format_audit.tools import AuditFormatTool


def test_audit_format_tool_has_input_contract():
    """AuditFormatTool must declare input_contract for auto-binding document."""
    tool = AuditFormatTool()
    assert hasattr(tool, 'input_contract')
    assert tool.input_contract is not None
    assert tool.input_contract.tool_name == "audit_format"
    
    field_names = {f.name for f in tool.input_contract.fields}
    assert "document" in field_names
    
    doc_field = next(f for f in tool.input_contract.fields if f.name == "document")
    assert doc_field.artifact_type == "docaudit.parsed_document"
    assert doc_field.materialize_as == "dict"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/skills/format_audit/test_input_contract.py -v
```
预期：FAIL — `AttributeError: 'AuditFormatTool' object has no attribute 'input_contract'`

- [ ] **步骤 3：添加 input_contract 到 AuditFormatTool**

```python
# src/agent/skills/format_audit/tools.py
# 在现有 imports 后添加
from src.agent.artifacts.models import (
    ContractField,
    ToolInputContract,
)

# 在 AuditFormatTool 类定义内，parameters 之后添加
    input_contract = ToolInputContract(
        tool_name="audit_format",
        fields=(
            ContractField(
                name="document",
                artifact_type="docaudit.parsed_document",
                role="primary_document",
                subject="current_upload",
                materialize_as="dict",
                required=True,
                allow_explicit_override=True,
            ),
        ),
    )
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/agent/skills/format_audit/test_input_contract.py -v
```
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add src/agent/skills/format_audit/tools.py tests/agent/skills/format_audit/test_input_contract.py
git commit -m "feat: add input_contract to AuditFormatTool for auto-binding document

Allows the tool registry to automatically bind 'document' from the
artifact store when the LLM calls audit_format without passing it.
Eliminates the need for the LLM to manually pass $ref strings."
```

---

## 任务 3：AuditContentTool 添加 input_contract

**文件：**
- 修改：`src/agent/skills/content_audit/tools.py`
- 创建：`tests/agent/skills/content_audit/test_input_contract.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/content_audit/test_input_contract.py
import pytest
from src.agent.skills.content_audit.tools import AuditContentTool


def test_audit_content_tool_has_input_contract():
    """AuditContentTool must declare input_contract for auto-binding."""
    tool = AuditContentTool()
    assert hasattr(tool, 'input_contract')
    assert tool.input_contract is not None
    
    field_names = {f.name for f in tool.input_contract.fields}
    assert "paragraphs" in field_names
    
    p_field = next(f for f in tool.input_contract.fields if f.name == "paragraphs")
    assert p_field.artifact_type == "docaudit.paragraph_list"
    assert p_field.materialize_as == "list_string"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/skills/content_audit/test_input_contract.py -v
```
预期：FAIL

- [ ] **步骤 3：添加 input_contract**

```python
# src/agent/skills/content_audit/tools.py
# 在 imports 中添加
from src.agent.artifacts.models import ContractField, ToolInputContract

# 在 AuditContentTool 类内添加
    input_contract = ToolInputContract(
        tool_name="audit_content",
        fields=(
            ContractField(
                name="paragraphs",
                artifact_type="docaudit.paragraph_list",
                role="primary_document",
                subject="current_upload",
                materialize_as="list_string",
                required=True,
                allow_explicit_override=True,
            ),
        ),
    )
```

> 注意：`AuditContentTool` 的参数是 `paragraphs`（段落文本列表），不是 `document`。
> 从 `docaudit.parsed_document` 可以投影到 `docaudit.paragraph_list`，
> 再物化为 `list_string`。

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/agent/skills/content_audit/test_input_contract.py -v
```

- [ ] **步骤 5：Commit**

```bash
git add src/agent/skills/content_audit/tools.py tests/agent/skills/content_audit/test_input_contract.py
git commit -m "feat: add input_contract to AuditContentTool for auto-binding paragraphs"
```

---

## 任务 4：CorrectTextTool 添加 input_contract

**文件：**
- 修改：`src/agent/skills/text_correction/tools.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/skills/text_correction/test_input_contract.py
import pytest
from src.agent.skills.text_correction.tools import CorrectTextTool


def test_correct_text_tool_has_input_contract():
    """CorrectTextTool must declare input_contract for auto-binding texts."""
    tool = CorrectTextTool()
    assert hasattr(tool, 'input_contract')
    assert tool.input_contract is not None
    
    field_names = {f.name for f in tool.input_contract.fields}
    assert "texts" in field_names
    
    t_field = next(f for f in tool.input_contract.fields if f.name == "texts")
    assert t_field.artifact_type == "docaudit.paragraph_list"
    assert t_field.materialize_as == "list_string"
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/skills/text_correction/test_input_contract.py -v
```

- [ ] **步骤 3：添加 input_contract**

```python
# src/agent/skills/text_correction/tools.py
# 在 imports 中添加
from src.agent.artifacts.models import ContractField, ToolInputContract

# 在 CorrectTextTool 类内添加
    input_contract = ToolInputContract(
        tool_name="correct_text",
        fields=(
            ContractField(
                name="texts",
                artifact_type="docaudit.paragraph_list",
                role="primary_document",
                subject="current_upload",
                materialize_as="list_string",
                required=True,
                allow_explicit_override=True,
            ),
        ),
    )
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/agent/skills/text_correction/test_input_contract.py -v
```

- [ ] **步骤 5：Commit**

```bash
git add src/agent/skills/text_correction/tools.py tests/agent/skills/text_correction/test_input_contract.py
git commit -m "feat: add input_contract to CorrectTextTool for auto-binding texts"
```

---

## 任务 5：Harness 层自动类型转换（str → list/dict）

**文件：**
- 修改：`src/agent/agents/subagent.py`
- 创建：`tests/agent/test_subagent_coerce.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/test_subagent_coerce.py
import pytest
from src.agent.agents.input_models import ContentAuditorInput, FormatAuditorInput
from src.agent.agents.subagent import _SubAgentTool


class TestCoerceKwargsToModel:
    def test_coerce_rules_string_to_list(self):
        """rules: "[]" should be coerced to [] before Pydantic validation."""
        kwargs = {"document": {"a": 1}, "rules": "[]", "task": "test"}
        result = _SubAgentTool._coerce_kwargs_to_model(kwargs, ContentAuditorInput)
        assert result["rules"] == []
        assert isinstance(result["rules"], list)

    def test_coerce_rules_json_string_to_list(self):
        """rules: '["content_compliance"]' should be coerced to list."""
        kwargs = {
            "document": {"a": 1},
            "rules": '["content_compliance", "sensitive_info"]',
            "task": "test",
        }
        result = _SubAgentTool._coerce_kwargs_to_model(kwargs, ContentAuditorInput)
        assert result["rules"] == ["content_compliance", "sensitive_info"]

    def test_coerce_document_string_to_dict(self):
        """document: '{"a": 1}' should be coerced to dict."""
        kwargs = {"document": '{"a": 1, "b": 2}', "doc_type": "通知", "task": "test"}
        result = _SubAgentTool._coerce_kwargs_to_model(kwargs, FormatAuditorInput)
        assert result["document"] == {"a": 1, "b": 2}
        assert isinstance(result["document"], dict)

    def test_non_json_string_unchanged(self):
        """Non-JSON strings should be left as-is for Pydantic to handle."""
        kwargs = {"document": {"a": 1}, "rules": "not_json", "task": "test"}
        result = _SubAgentTool._coerce_kwargs_to_model(kwargs, ContentAuditorInput)
        assert result["rules"] == "not_json"

    def test_already_correct_type_unchanged(self):
        """Already-correct types should not be modified."""
        kwargs = {"document": {"a": 1}, "rules": ["x"], "task": "test"}
        result = _SubAgentTool._coerce_kwargs_to_model(kwargs, ContentAuditorInput)
        assert result["rules"] == ["x"]
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/test_subagent_coerce.py -v
```
预期：FAIL — `AttributeError: type object '_SubAgentTool' has no attribute '_coerce_kwargs_to_model'`

- [ ] **步骤 3：实现 `_coerce_kwargs_to_model`**

```python
# src/agent/agents/subagent.py
# 添加 import
import json
import typing

# 在 _SubAgentTool 类内添加静态方法（放在 _resolve_input_refs 附近）
    @staticmethod
    def _coerce_kwargs_to_model(
        kwargs: dict[str, Any], model_cls: type[BaseModel]
    ) -> dict[str, Any]:
        """Coerce JSON-encoded string values to list/dict before Pydantic validation.

        LLMs sometimes serialize list/dict arguments as JSON strings
        (e.g. rules="[]" instead of rules=[]). This helper attempts to
        auto-convert such strings so Pydantic validation succeeds.
        """
        coerced = dict(kwargs)
        for name, field_info in model_cls.model_fields.items():
            if name not in coerced:
                continue
            value = coerced[name]
            if not isinstance(value, str):
                continue
            if not _annotation_expects_type(field_info.annotation, (list, dict)):
                continue
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (list, dict)):
                    coerced[name] = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return coerced


# 在模块级别添加辅助函数
def _annotation_expects_type(annotation: Any, types: tuple[type, ...]) -> bool:
    """Check whether a type annotation expects one of the given types.

    Handles simple types, Optional[T], Union[T, None], and generic aliases.
    """
    if annotation is None:
        return False

    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = typing.get_args(annotation)
        return any(
            _annotation_expects_type(arg, types) for arg in args if arg is not type(None)
        )

    if origin is not None:
        # Generic aliases like list[str], dict[str, int]
        return any(origin is t or (isinstance(t, type) and issubclass(origin, t)) for t in types)

    if isinstance(annotation, type):
        return any(issubclass(annotation, t) for t in types)

    return False
```

- [ ] **步骤 4：在 `_execute_structured` 中调用 coercion**

```python
# src/agent/agents/subagent.py:584-606
# 在 Pydantic 校验前插入 coercion 步骤
        try:
            kwargs = self._coerce_kwargs_to_model(kwargs, config.input_model)
            input_obj = config.input_model(**kwargs)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"参数校验失败: {exc}",
            )
```

- [ ] **步骤 5：运行测试验证通过**

```bash
uv run pytest tests/agent/test_subagent_coerce.py -v
```
预期：PASS（5 tests passed）

- [ ] **步骤 6：Commit**

```bash
git add src/agent/agents/subagent.py tests/agent/test_subagent_coerce.py
git commit -m "feat: auto-coerce JSON string args to list/dict in subagent dispatch

LLMs frequently serialize list/dict parameters as JSON strings
(e.g. rules='[]' instead of rules=[]). Add _coerce_kwargs_to_model()
that inspects Pydantic field annotations and auto-converts strings
to list/dict before validation. Reduces type errors across all
subagent types."
```

---

## 任务 6：list_artifacts role/subject 模糊匹配

**文件：**
- 修改：`src/agent/tools/builtin/list_artifacts.py`
- 创建：`tests/agent/tools/test_list_artifacts_fuzzy.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/tools/test_list_artifacts_fuzzy.py
import pytest
from unittest.mock import MagicMock
from src.agent.tools.builtin.list_artifacts import ListArtifactsTool


@pytest.fixture
def mock_artifact_store():
    store = MagicMock()
    
    # Mock artifacts with actual role/subject values
    artifact1 = MagicMock()
    artifact1.artifact_type = "docaudit.parsed_document"
    artifact1.metadata.semantic_role = "primary_document"
    artifact1.metadata.subject = "current_upload"
    artifact1.metadata.created_by = "parse_document"
    artifact1.metadata.content_hash = "abc"
    artifact1.metadata.quality = {}
    artifact1.metadata.lineage = []
    
    artifact2 = MagicMock()
    artifact2.artifact_type = "core.debug_view"
    artifact2.metadata.semantic_role = "debug"
    artifact2.metadata.subject = "debug"
    artifact2.metadata.created_by = "get_artifact"
    artifact2.metadata.content_hash = "def"
    artifact2.metadata.quality = {}
    artifact2.metadata.lineage = []
    
    store.list_projection_candidates = MagicMock(return_value=[artifact1, artifact2])
    return store


@pytest.mark.asyncio
async def test_fuzzy_role_match(mock_artifact_store):
    """'document' should match 'primary_document' via substring."""
    tool = ListArtifactsTool()
    result = await tool.execute(artifact_store=mock_artifact_store, role="document")
    
    assert result.success
    artifacts = result.data["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["role"] == "primary_document"


@pytest.mark.asyncio
async def test_fuzzy_subject_match(mock_artifact_store):
    """'current' should match 'current_upload' via substring."""
    tool = ListArtifactsTool()
    result = await tool.execute(artifact_store=mock_artifact_store, subject="current")
    
    assert result.success
    artifacts = result.data["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["subject"] == "current_upload"


@pytest.mark.asyncio
async def test_exact_match_still_works(mock_artifact_store):
    """Exact matches should continue to work."""
    tool = ListArtifactsTool()
    result = await tool.execute(
        artifact_store=mock_artifact_store,
        role="primary_document",
        subject="current_upload",
    )
    
    assert result.success
    artifacts = result.data["artifacts"]
    assert len(artifacts) == 1


@pytest.mark.asyncio
async def test_no_match_returns_empty(mock_artifact_store):
    """Non-matching filters should return empty."""
    tool = ListArtifactsTool()
    result = await tool.execute(artifact_store=mock_artifact_store, role="nonexistent")
    
    assert result.success
    assert result.data["artifacts"] == []
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/tools/test_list_artifacts_fuzzy.py -v
```
预期：FAIL — fuzzy match 未实现，role="document" 不匹配 "primary_document"

- [ ] **步骤 3：实现模糊匹配**

```python
# src/agent/tools/builtin/list_artifacts.py:90-101
# 将精确匹配替换为子串匹配
        if role:
            candidates = [
                a for a in candidates
                if a.metadata.semantic_role == role or role in a.metadata.semantic_role
            ]
        if subject:
            candidates = [
                a for a in candidates
                if a.metadata.subject == subject or subject in a.metadata.subject
            ]
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/agent/tools/test_list_artifacts_fuzzy.py -v
```

- [ ] **步骤 5：Commit**

```bash
git add src/agent/tools/builtin/list_artifacts.py tests/agent/tools/test_list_artifacts_fuzzy.py
git commit -m "feat: fuzzy matching for list_artifacts role/subject filters

LLMs guess filter values like role='document' and subject='current'
which don't exactly match stored values 'primary_document' and
'current_upload'. Add substring fallback so inexact guesses still
find artifacts, reducing wasted turns."
```

---

## 任务 7：get_artifact 防御性校验

**文件：**
- 修改：`src/agent/tools/builtin/get_artifact.py`
- 创建：`tests/agent/tools/test_get_artifact_defense.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/agent/tools/test_get_artifact_defense.py
import pytest
from unittest.mock import MagicMock
from src.agent.tools.builtin.get_artifact import GetArtifactTool


@pytest.mark.asyncio
async def test_rejects_ref_get_artifact_ids():
    """get_artifact should reject $ref:get_artifact:N as artifact_id."""
    tool = GetArtifactTool()
    result = await tool.execute(
        artifact_store=MagicMock(),
        artifact_id="$ref:get_artifact:1",
        artifact_type="core.plain_text",
    )
    
    assert not result.success
    assert "不是有效的 artifact_id" in result.error
    assert "list_artifacts" in result.error


@pytest.mark.asyncio
async def test_accepts_valid_artifact_ids():
    """Valid artifact_ids like $ref:parse_document:1 should proceed."""
    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock())
    
    tool = GetArtifactTool()
    # This will fail at projection stage but should pass the defense check
    result = await tool.execute(
        artifact_store=store,
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
    )
    
    # Should NOT be the defense error
    if not result.success:
        assert "不是有效的 artifact_id" not in result.error
```

- [ ] **步骤 2：运行测试验证失败**

```bash
uv run pytest tests/agent/tools/test_get_artifact_defense.py -v
```
预期：FAIL — `$ref:get_artifact:1` 被当作正常 artifact_id 处理，可能后续报错不同

- [ ] **步骤 3：添加防御性校验**

```python
# src/agent/tools/builtin/get_artifact.py:105-120
# 在 artifact_store is None 检查之后，source lookup 之前添加
        if artifact_id.startswith("$ref:get_artifact:"):
            return ToolResult(
                success=False,
                error=(
                    f"'{artifact_id}' 是工具输出的持久化标记，不是有效的 artifact_id。"
                    f"请先调用 list_artifacts() 获取可用的 artifact_id。"
                ),
            )
```

- [ ] **步骤 4：运行测试验证通过**

```bash
uv run pytest tests/agent/tools/test_get_artifact_defense.py -v
```

- [ ] **步骤 5：Commit**

```bash
git add src/agent/tools/builtin/get_artifact.py tests/agent/tools/test_get_artifact_defense.py
git commit -m "feat: defensive validation in get_artifact for invalid artifact_ids

Reject $ref:get_artifact:N IDs which LLMs mistakenly pass as
artifact_ids. Provide a clear error message directing them to
list_artifacts instead."
```

---

## 任务 8：Orchestrator Prompt 优化

**文件：**
- 修改：`src/agent/agents/orch.py:170-197`

- [ ] **步骤 1：修改 Orchestrator 角色 prompt**

```python
# src/agent/agents/orch.py:170-197
# 将现有 role 字符串中的相关段落替换

        role = (
            "你是 DocAudit 文档审计平台的编排代理（OrchestratorAgent），"
            "负责协调文档审计工作流。"
            "收到文档审计任务后，首先调用 parse_document 解析文档文件，"
            "然后根据解析结果和审计需求调用相应的审计工具。"
            "对政府公文（通知/函/请示），始终先执行格式审计验证文档结构，"
            "再根据需要执行其他审计。所有审计完成后，简洁地总结发现。"
            "\n\n"
            "# 工作流规则\n"
            "1. 收到任务后，首先调用 parse_document 解析文档。"
            "文件路径在上下文的 file_path 中。\n"
            "2. parse_document 成功后，系统会自动缓存解析结果，"
            "后续审计工具可通过 $ref:parse_document:1 引用。\n"
            "3. 对政府公文（通知/函/请示），先执行格式审计，"
            "再根据需要执行其他审计。\n"
            "4. get_artifact 之前必须先调用 list_artifacts，原因有二："
            "① 获取准确的 artifact_id（含 $ref: 前缀，如 $ref:parse_document:1）；"
            "② 查看 projectable_to_types 字段了解可投影的目标类型"
            "（如 docaudit.parsed_document 可投影为 core.plain_text），"
            "然后选择合适的目标类型调用 get_artifact。"
            "严禁跳过 list_artifacts 直接调用 get_artifact。\n"
            "\n"
            "# get_artifact 使用规范\n"
            "get_artifact 的 artifact_id 必须来自 list_artifacts 返回的 artifact_id 字段。"
            "不要使用工具返回的 ref_id（如 $ref:get_artifact:N）作为 artifact_id。"
            "\n\n"
            "# 结构化参数传递规则\n"
            "每个审计工具的参数中，数据字段（如 document）接受两种传值方式：\n"
            '1. 传入 $ref 缓存引用字符串（如 "$ref:parse_document:1"），系统自动加载完整数据 — 推荐\n'
            "2. 传入完整的 JSON 对象（仅当数据较小时使用）\n"
            "始终优先使用方式 1 避免上下文膨胀。\n"
        )
```

> 注意：新增了 "# get_artifact 使用规范" 段落，明确禁止用 `$ref:get_artifact:N` 作为 artifact_id。

- [ ] **步骤 2：运行现有 Orchestrator 测试确保无 regression**

```bash
uv run pytest tests/agent/test_orchestrator.py -v
```
预期：PASS（或确认失败不是由本次改动引起）

- [ ] **步骤 3：Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "docs: strengthen get_artifact usage rules in orchestrator prompt

Add explicit rule: artifact_id must come from list_artifacts,
not from tool-returned ref_ids like $ref:get_artifact:N.
This addresses LLM confusion between ref_ids and artifact_ids."
```

---

## 任务 9：集成验证

**文件：**
- 运行：`scripts/diagnose_subagent_ref.py`

- [ ] **步骤 1：运行诊断脚本验证 Scenario B**

```bash
PYTHONPATH=. uv run python scripts/diagnose_subagent_ref.py
```

预期输出中 Scenario B 的 `resolve_refs` 应该从 ❌ 变为 ✅（因为 rehydrate 后 cache_store.ref_map 有数据了）。

- [ ] **步骤 2：运行全部新增测试**

```bash
uv run pytest tests/agent/test_routes_rehydrate.py tests/agent/test_subagent_coerce.py tests/agent/skills/format_audit/test_input_contract.py tests/agent/skills/content_audit/test_input_contract.py tests/agent/skills/text_correction/test_input_contract.py tests/agent/tools/test_list_artifacts_fuzzy.py tests/agent/tools/test_get_artifact_defense.py -v
```

预期：全部 PASS

- [ ] **步骤 3：运行完整测试套件**

```bash
uv run pytest tests/ -x --timeout=60
```

预期：无 regression（或确认已有失败不是由本次改动引起）

- [ ] **步骤 4：Commit（如有额外修复）**

如果诊断脚本或完整测试发现 regression，修复后 commit：

```bash
git add ...
git commit -m "fix: address regressions from unified ref resolution changes"
```

---

## 自检

**1. 规格覆盖度：**

| 规格章节 | 对应任务 |
|----------|---------|
| 第一节：rehydrate 同步 ref_map | 任务 1 ✅ |
| 第二节：input_contract 自动绑定 | 任务 2, 3, 4 ✅ |
| 第三节：自动类型转换 | 任务 5 ✅ |
| 第四节：turn 预算 + prompt 优化 | 任务 6 (fuzzy match), 任务 8 (prompt) ✅ |
| 第五节：防御校验 | 任务 7 ✅ |

**2. 占位符扫描：** 无 TODO、无"后续实现"、无"添加适当的错误处理"。每步都有实际代码。

**3. 类型一致性：**
- `_coerce_kwargs_to_model` 在任务 5 中定义，在步骤 4 中调用 — 一致
- `ContractField` 使用 `materialize_as="dict"` 和 `materialize_as="list_string"` — 与现有 plagiarism tool 模式一致

---

## 执行选项

**计划已完成并保存到 `docs/superpowers/plans/2026-06-07-unified-ref-resolution-plan.md`。两种执行方式：**

**1. 子代理驱动（推荐）** — 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** — 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

**选哪种方式？**
