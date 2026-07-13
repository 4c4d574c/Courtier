# Artifact Projector Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-ready slice of the artifact projector contract framework so plagiarism detection can receive typed, projected inputs without `read_cached_output` exploration loops.

**Architecture:** Add a small typed artifact/projection package, wire semantic tool contracts into `ToolRegistry`, and keep existing `$ref` cache behavior compatible while adding safer artifact-aware auto-binding. Start with the plagiarism path: `ParsedDocument -> ParagraphList -> PlainText` and `SearchResults -> ReferenceTextList -> TextCollection`.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, uv, existing `src.agent` runtime, existing `ContextManager`/`CacheStore` persistence.

---

## Scope

This plan implements the first complete vertical slice from the approved design:

1. Loop safety hotfixes.
2. Typed artifact and contract models.
3. Projector registry and initial docaudit projectors.
4. Resolver, executor, materializer, and step cache.
5. ToolRegistry contract-aware auto-binding for `detect_plagiarism`.
6. Debug/cache isolation for `read_cached_output`.
7. Regression tests for the observed `.agent_logs` failure mode.

Deferred to a later plan:

- Full graph audit CLI.
- Broad contracts for every existing tool.
- Deterministic plagiarism pipeline mode in `PlagiarismAuditorAgent`.
- Permission/sensitivity enforcement beyond metadata fields.

These are intentionally deferred because the first vertical slice must remain testable and compatible with existing tools.

---

## File Structure

### New files

- `src/agent/artifacts/__init__.py` — public exports for artifact primitives.
- `src/agent/artifacts/models.py` — immutable Pydantic models for artifacts, contracts, projection plans, diagnostics, policies, and materialized bindings.
- `src/agent/artifacts/store.py` — in-memory typed artifact store that wraps existing cached refs and projected artifacts.
- `src/agent/artifacts/projectors.py` — projector protocol, registry, and initial docaudit projectors.
- `src/agent/artifacts/resolver.py` — bounded projection graph resolver.
- `src/agent/artifacts/executor.py` — projection plan executor and materializer registry.
- `tests/agent/artifacts/__init__.py` — test package marker.
- `tests/agent/artifacts/test_models.py` — artifact model tests.
- `tests/agent/artifacts/test_projectors.py` — initial projector tests.
- `tests/agent/artifacts/test_resolver.py` — resolver path/role/debug tests.
- `tests/agent/artifacts/test_executor.py` — executor/materializer/cache tests.
- `tests/agent/test_tool_contract_binding.py` — ToolRegistry integration tests for `detect_plagiarism`.

### Modified files

- `src/agent/tools/protocol.py` — add optional `input_contract`, `output_contract`, and `runtime_policy` protocol attributes.
- `src/agent/tools/registry.py` — add optional `artifact_store` and contract-aware pre-execution binding while preserving old behavior.
- `src/agent/core/context_manager.py` — prevent micro-compaction from creating chainable `read_cached_output` refs and update ref instructions.
- `src/agent/core/loop.py` — enforce all-turn max steps and add direct read-cache chain guard before executing tools.
- `src/agent/tools/builtin/read_cached.py` — reject `read_cached_output` refs by default and mark tool metadata as debug-only.
- `src/agent/skills/search/tool.py` — reject low-signal punctuation-only search queries.
- `src/agent/skills/plagiarism/tools.py` — add input/output contracts to `DetectPlagiarismTool`.
- `src/agent/agents/orch.py` — register parse artifact metadata after `force_persist` and pass artifact store to the agent loop when available.
- `tests/agent/test_loop.py` — add safety regressions.
- `tests/agent/test_context_manager.py` — add micro-compaction regression.
- `tests/agent/tools/test_read_cached.py` — add read-cache chain rejection tests.
- `tests/agent/tools/test_search.py` — add low-signal query tests.

---

## Implementation Tasks

### Task 1: Add loop safety tests for all-turn max steps and read-cache chains

**Files:**
- Modify: `tests/agent/test_loop.py`
- Modify: `tests/agent/tools/test_read_cached.py`

- [ ] **Step 1: Add a test that tool-call turns count against `max_steps`**

Append this test to `tests/agent/test_loop.py` inside `class TestAgentLoop`:

```python
    @pytest.mark.asyncio
    async def test_max_steps_applies_to_repeated_tool_call_turns(self, registry_with_echo):
        """A model that keeps requesting tools must stop at max_steps."""
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "ping"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo forever", max_steps=2)
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
        )

        assert final.status == "completed"
        assert final.termination_reason == "max_steps"
        assert final.current_step == 2
```

- [ ] **Step 2: Add a test that `read_cached_output` refuses to read its own refs**

Append this test to `tests/agent/tools/test_read_cached.py` inside `class TestReadCachedOutputTool`:

```python
    def test_rejects_read_cached_output_ref_chain(self, tmp_path) -> None:
        import asyncio

        cache_path = tmp_path / "read_cached_output_1.json"
        cache_path.write_text('{"text": "cached debug data"}', encoding="utf-8")

        tool = ReadCachedOutputTool()
        cm = FakeContextManager()
        cm._cache.ref_map["$ref:read_cached_output:1"] = str(cache_path)

        result = asyncio.run(
            tool.execute(
                ref_id="$ref:read_cached_output:1",
                context_manager=cm,
                query=".",
            )
        )

        assert result.success is False
        assert "read_cached_output 的输出不能再次作为 read_cached_output 的输入" in result.error
```

- [ ] **Step 3: Run the new tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/test_loop.py::TestAgentLoop::test_max_steps_applies_to_repeated_tool_call_turns tests/agent/tools/test_read_cached.py::TestReadCachedOutputTool::test_rejects_read_cached_output_ref_chain -v
```

Expected:

```text
FAILED tests/agent/test_loop.py::TestAgentLoop::test_max_steps_applies_to_repeated_tool_call_turns
FAILED tests/agent/tools/test_read_cached.py::TestReadCachedOutputTool::test_rejects_read_cached_output_ref_chain
```

- [ ] **Step 4: Commit failing safety tests**

```bash
git add tests/agent/test_loop.py tests/agent/tools/test_read_cached.py
git commit -m "test: capture agent loop cache-chain regressions"
```

---

### Task 2: Implement loop safety hotfixes

**Files:**
- Modify: `src/agent/core/state.py`
- Modify: `src/agent/core/loop.py`
- Modify: `src/agent/tools/builtin/read_cached.py`

- [ ] **Step 1: Stop tool-call turns at `max_steps` in `AgentState.add_thought`**

In `src/agent/core/state.py`, replace the `if response.tool_calls:` branch in `add_thought` with:

```python
        # If the model returned tool calls, add an assistant message with them.
        if response.tool_calls:
            next_step = self.current_step + 1
            new_messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if next_step >= self.max_steps:
                return self.model_copy(
                    update={
                        "status": "completed",
                        "messages": tuple(new_messages),
                        "tool_calls": (),
                        "current_step": next_step,
                        "termination_reason": "max_steps",
                    }
                )
            return self.model_copy(
                update={
                    "status": "waiting_for_tool",
                    "messages": tuple(new_messages),
                    "tool_calls": tuple(response.tool_calls),
                    "current_step": next_step,
                }
            )
```

- [ ] **Step 2: Reject read-cache chains in `ReadCachedOutputTool.execute`**

In `src/agent/tools/builtin/read_cached.py`, add this module-level constant after `_NULLSAFE_RE`:

```python
_READ_CACHED_REF_RE = re.compile(r"^\$ref:read_cached_output:\d+(?::[a-zA-Z_][a-zA-Z0-9_]*)?$")
```

Then add this check in `execute` after the `if not isinstance(ref_id, str):` block and before `if context_manager is None:`:

```python
        if _READ_CACHED_REF_RE.match(ref_id):
            return ToolResult(
                success=False,
                error=(
                    "read_cached_output 的输出不能再次作为 read_cached_output 的输入；"
                    "请回到原始业务 ref，或改为调用目标业务工具。"
                ),
                metadata={"blocked_reason": "read_cached_output_chain"},
            )
```

- [ ] **Step 3: Add a direct pre-execution chain guard in the agent loop**

In `src/agent/core/loop.py`, add this helper near the other helper functions:

```python
def _is_read_cached_chain_call(tool_name: str, arguments: dict) -> bool:
    """Return True when read_cached_output tries to read read_cached_output output."""
    if tool_name != "read_cached_output":
        return False
    ref_id = arguments.get("ref_id")
    if isinstance(ref_id, dict):
        ref_id = ref_id.get("ref_id")
    return isinstance(ref_id, str) and ref_id.startswith("$ref:read_cached_output:")
```

Then in the tool execution loop, immediately before `try:` at current line near `293`, insert:

```python
            if _is_read_cached_chain_call(tool_call.name, dict(tool_call.arguments)):
                result = ToolResult(
                    success=False,
                    error=(
                        "Blocked debug cache chain: read_cached_output cannot read "
                        "a read_cached_output ref. Use the original business ref or "
                        "call the target business tool directly."
                    ),
                    metadata={"blocked_reason": "read_cached_output_chain"},
                )
                tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
                tool_records.append(
                    ToolExecutionRecord(
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                        arguments=dict(tool_call.arguments),
                        result_success=result.success,
                        result_data=result.data,
                        result_error=result.error,
                        duration_ms=tool_duration_ms,
                    )
                )
                results.append(result)
                continue
```

- [ ] **Step 4: Run safety tests**

Run:

```bash
uv run pytest tests/agent/test_loop.py::TestAgentLoop::test_max_steps_applies_to_repeated_tool_call_turns tests/agent/tools/test_read_cached.py::TestReadCachedOutputTool::test_rejects_read_cached_output_ref_chain -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run existing loop/read-cache tests**

Run:

```bash
uv run pytest tests/agent/test_loop.py tests/agent/tools/test_read_cached.py -v
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit safety hotfixes**

```bash
git add src/agent/core/state.py src/agent/core/loop.py src/agent/tools/builtin/read_cached.py tests/agent/test_loop.py tests/agent/tools/test_read_cached.py
git commit -m "fix: block cache-chain loops in agent execution"
```

---

### Task 3: Reject low-signal search queries

**Files:**
- Modify: `src/agent/skills/search/tool.py`
- Modify: `tests/agent/tools/test_search.py`

- [ ] **Step 1: Add low-signal query tests**

Append these tests to `tests/agent/tools/test_search.py`:

```python
import pytest

from src.agent.skills.search.tool import SearchDocumentsTool


@pytest.mark.asyncio
async def test_search_rejects_punctuation_only_query():
    tool = SearchDocumentsTool()

    result = await tool.execute(query=".")

    assert result.success is False
    assert "查询内容过短或缺少有效字符" in result.error


@pytest.mark.asyncio
async def test_search_rejects_symbol_only_query():
    tool = SearchDocumentsTool()

    result = await tool.execute(query="***")

    assert result.success is False
    assert "查询内容过短或缺少有效字符" in result.error
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/tools/test_search.py::test_search_rejects_punctuation_only_query tests/agent/tools/test_search.py::test_search_rejects_symbol_only_query -v
```

Expected:

```text
FAILED
```

- [ ] **Step 3: Add query signal validation**

In `src/agent/skills/search/tool.py`, add this constant after `_MAX_QUERY_CHARS`:

```python
_LOW_SIGNAL_QUERY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
```

Add this helper after `_parse_query`:

```python
def _has_search_signal(query: str) -> bool:
    """Return True when query contains meaningful alphanumeric or CJK content."""
    stripped = query.strip()
    if len(stripped) < 2:
        return False
    return _LOW_SIGNAL_QUERY_RE.match(stripped) is None
```

Then modify `SearchDocumentsTool.execute` after the empty-query check:

```python
        if not _has_search_signal(query):
            return ToolResult(success=False, error="查询内容过短或缺少有效字符")
```

- [ ] **Step 4: Run search tests**

Run:

```bash
uv run pytest tests/agent/tools/test_search.py -v
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit search guard**

```bash
git add src/agent/skills/search/tool.py tests/agent/tools/test_search.py
git commit -m "fix: reject low-signal document search queries"
```

---

### Task 4: Add artifact and contract models

**Files:**
- Create: `src/agent/artifacts/__init__.py`
- Create: `src/agent/artifacts/models.py`
- Create: `tests/agent/artifacts/__init__.py`
- Create: `tests/agent/artifacts/test_models.py`

- [ ] **Step 1: Write model tests**

Create `tests/agent/artifacts/test_models.py` with:

```python
from __future__ import annotations

from src.agent.artifacts.models import (
    Artifact,
    ArtifactMetadata,
    ContractField,
    ProjectionPolicy,
    ToolInputContract,
)


def test_artifact_defaults_to_projection_allowed_business_artifact():
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="docaudit.parsed_document",
        schema_version="1.0",
        data={"pages": []},
        metadata=ArtifactMetadata(created_by="parse_document"),
    )

    assert artifact.metadata.projection_allowed is True
    assert artifact.metadata.debug_only is False
    assert artifact.metadata.semantic_role == "intermediate"


def test_debug_artifact_is_not_projection_candidate():
    artifact = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        schema_version="1.0",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="read_cached_output",
            semantic_role="debug",
            subject="debug",
            projection_allowed=False,
            debug_only=True,
        ),
    )

    assert artifact.is_projection_candidate is False


def test_tool_input_contract_indexes_required_fields():
    contract = ToolInputContract(
        tool_name="detect_plagiarism",
        fields=(
            ContractField(
                name="new_doc",
                artifact_type="core.plain_text",
                role="primary_document",
                subject="current_upload",
                materialize_as="string",
            ),
            ContractField(
                name="library_docs",
                artifact_type="core.text_collection",
                role="reference_document",
                subject="reference_library",
                materialize_as="list_string",
            ),
        ),
    )

    assert contract.required_fields == ("new_doc", "library_docs")


def test_projection_policy_defaults_are_safe():
    policy = ProjectionPolicy()

    assert policy.max_depth == 3
    assert policy.allow_debug_artifacts is False
    assert policy.allow_deprecated is False
```

- [ ] **Step 2: Run model tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/artifacts/test_models.py -v
```

Expected:

```text
FAILED with ModuleNotFoundError: No module named 'src.agent.artifacts'
```

- [ ] **Step 3: Create artifact models**

Create `src/agent/artifacts/models.py` with:

```python
"""Typed artifact and projection contract models."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

ProjectorStability = Literal["stable", "experimental", "deprecated", "disabled"]
ProjectorLayer = Literal["core", "domain", "plugin", "local"]
ProjectorCost = Literal["free", "cheap", "moderate", "expensive"]
Lossiness = Literal["lossless", "lossy", "summary"]


class ArtifactMetadata(BaseModel, frozen=True):
    """Metadata attached to an artifact instance."""

    created_by: str
    source_refs: tuple[str, ...] = ()
    content_hash: str = ""
    semantic_role: str = "intermediate"
    subject: str = "unknown"
    scope: str = "current_task"
    sensitivity: str = "internal"
    projection_allowed: bool = True
    debug_only: bool = False
    quality: dict[str, Any] = Field(default_factory=dict)
    lineage: tuple[str, ...] = ()


class Artifact(BaseModel, frozen=True):
    """A typed data instance that can participate in projection."""

    artifact_id: str
    artifact_type: str
    schema_version: str = "1.0"
    data: Any
    metadata: ArtifactMetadata

    @property
    def is_projection_candidate(self) -> bool:
        return self.metadata.projection_allowed and not self.metadata.debug_only


class ContractField(BaseModel, frozen=True):
    """One semantic field requirement for a tool."""

    name: str
    artifact_type: str
    role: str = "intermediate"
    subject: str = "unknown"
    materialize_as: str
    required: bool = True
    constraints: dict[str, Any] = Field(default_factory=dict)
    allow_explicit_override: bool = True


class ToolInputContract(BaseModel, frozen=True):
    """Semantic input contract for a tool."""

    tool_name: str
    fields: tuple[ContractField, ...] = ()
    require_contract_binding: bool = False

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields if field.required)


class OutputArtifactContract(BaseModel, frozen=True):
    """Semantic output contract for a tool result."""

    artifact_type: str
    role: str = "intermediate"
    subject: str = "unknown"
    schema_version: str = "1.0"
    persist: str = "auto"
    llm_visible: str = "summary"
    projection_allowed: bool = True
    debug_only: bool = False


class ToolOutputContract(BaseModel, frozen=True):
    """Output artifact declarations for a tool."""

    tool_name: str
    outputs: dict[str, OutputArtifactContract]


class ToolRuntimePolicy(BaseModel, frozen=True):
    """Runtime safety policy for a tool."""

    max_calls_per_agent_run: int | None = None
    max_consecutive_calls: int | None = None
    allow_chaining: bool = True
    hidden_from_task_agents_by_default: bool = False
    require_debug_policy: bool = False


class ProjectionDiagnostic(BaseModel, frozen=True):
    """Human-readable diagnostic emitted during resolution or execution."""

    level: Literal["info", "warning", "error"]
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProjectionQuality(BaseModel, frozen=True):
    """Runtime quality metadata for a projection."""

    confidence: float = 1.0
    lossiness: Lossiness = "lossless"
    stats: dict[str, Any] = Field(default_factory=dict)


class ProjectorSpec(BaseModel, frozen=True):
    """Metadata for one projector edge."""

    name: str
    source_type: str
    target_type: str
    version: str = "1.0.0"
    owner: str
    layer: ProjectorLayer = "domain"
    stability: ProjectorStability = "stable"
    deterministic: bool = True
    lossiness: Lossiness = "lossy"
    cost: ProjectorCost = "cheap"
    quality_score: float = 1.0
    supported_constraints: tuple[str, ...] = ()
    description: str = ""


class ProjectionStep(BaseModel, frozen=True):
    """One selected projector step in a projection plan."""

    projector_name: str
    source_type: str
    target_type: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class MaterializerSpec(BaseModel, frozen=True):
    """How to convert a final artifact into a concrete tool argument."""

    artifact_type: str
    materialize_as: str


class ProjectionPlan(BaseModel, frozen=True):
    """Plan for satisfying one tool field."""

    field_name: str
    required_type: str
    source_artifact_id: str
    steps: tuple[ProjectionStep, ...] = ()
    materializer: MaterializerSpec
    score: float = 1.0
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()


class ProjectionResolution(BaseModel, frozen=True):
    """Resolution result for a tool input contract."""

    status: Literal["resolved", "failed"]
    plans: dict[str, ProjectionPlan] = Field(default_factory=dict)
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()
    suggested_actions: tuple[dict[str, Any], ...] = ()


class ProjectionPolicy(BaseModel, frozen=True):
    """Resolver policy."""

    max_depth: int = 3
    min_quality: float = 0.7
    allow_lossy: bool = True
    allow_experimental: bool = False
    allow_deprecated: bool = False
    allow_debug_artifacts: bool = False
    prefer_cached: bool = True
    strict_ambiguity: bool = False


class MaterializedBinding(BaseModel, frozen=True):
    """Concrete tool argument plus artifact provenance."""

    value: Any
    artifact_id: str
    materializer: str


def stable_content_hash(data: Any) -> str:
    """Return a deterministic SHA-256 hash for JSON-compatible data."""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Export artifact models**

Create `src/agent/artifacts/__init__.py` with:

```python
"""Typed artifact projection framework."""

from .models import (
    Artifact,
    ArtifactMetadata,
    ContractField,
    MaterializedBinding,
    MaterializerSpec,
    OutputArtifactContract,
    ProjectionDiagnostic,
    ProjectionPlan,
    ProjectionPolicy,
    ProjectionQuality,
    ProjectionResolution,
    ProjectionStep,
    ProjectorSpec,
    ToolInputContract,
    ToolOutputContract,
    ToolRuntimePolicy,
    stable_content_hash,
)

__all__ = [
    "Artifact",
    "ArtifactMetadata",
    "ContractField",
    "MaterializedBinding",
    "MaterializerSpec",
    "OutputArtifactContract",
    "ProjectionDiagnostic",
    "ProjectionPlan",
    "ProjectionPolicy",
    "ProjectionQuality",
    "ProjectionResolution",
    "ProjectionStep",
    "ProjectorSpec",
    "ToolInputContract",
    "ToolOutputContract",
    "ToolRuntimePolicy",
    "stable_content_hash",
]
```

Create `tests/agent/artifacts/__init__.py` with:

```python
"""Tests for artifact projection framework."""
```

- [ ] **Step 5: Run model tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_models.py -v
```

Expected:

```text
4 passed
```

- [ ] **Step 6: Commit artifact models**

```bash
git add src/agent/artifacts/__init__.py src/agent/artifacts/models.py tests/agent/artifacts/__init__.py tests/agent/artifacts/test_models.py
git commit -m "feat: add typed artifact contract models"
```

---

### Task 5: Add typed ArtifactStore wrapper

**Files:**
- Create: `src/agent/artifacts/store.py`
- Create: `tests/agent/artifacts/test_store.py`

- [ ] **Step 1: Write artifact store tests**

Create `tests/agent/artifacts/test_store.py` with:

```python
from __future__ import annotations

from src.agent.artifacts.models import Artifact, ArtifactMetadata
from src.agent.artifacts.store import ArtifactStore


def test_put_and_get_artifact():
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(artifact)

    assert store.get("a1") == artifact


def test_list_projection_candidates_excludes_debug_artifacts():
    store = ArtifactStore()
    business = Artifact(
        artifact_id="business",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="projector"),
    )
    debug = Artifact(
        artifact_id="debug",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="read_cached_output",
            projection_allowed=False,
            debug_only=True,
        ),
    )

    store.put(business)
    store.put(debug)

    assert store.list_projection_candidates() == [business]


def test_register_cached_ref_creates_artifact_with_metadata():
    store = ArtifactStore()

    artifact = store.register_cached_ref(
        ref_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_document",
        data={"pages": []},
        role="primary_document",
        subject="current_upload",
    )

    assert artifact.artifact_id == "$ref:parse_document:1"
    assert artifact.artifact_type == "docaudit.parsed_document"
    assert artifact.metadata.semantic_role == "primary_document"
    assert artifact.metadata.subject == "current_upload"
```

- [ ] **Step 2: Run store tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/artifacts/test_store.py -v
```

Expected:

```text
FAILED with ModuleNotFoundError or ImportError for ArtifactStore
```

- [ ] **Step 3: Implement ArtifactStore**

Create `src/agent/artifacts/store.py` with:

```python
"""In-memory typed artifact store.

This wraps existing cached refs without replacing ContextManager or CacheStore.
"""

from __future__ import annotations

from typing import Any

from .models import Artifact, ArtifactMetadata, stable_content_hash


class ArtifactStore:
    """Small typed artifact registry for one agent run."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    def put(self, artifact: Artifact) -> Artifact:
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return artifact

    def list_all(self) -> list[Artifact]:
        return list(self._artifacts.values())

    def list_projection_candidates(self) -> list[Artifact]:
        return [artifact for artifact in self._artifacts.values() if artifact.is_projection_candidate]

    def find_by_type(self, artifact_type: str) -> list[Artifact]:
        return [
            artifact
            for artifact in self._artifacts.values()
            if artifact.artifact_type == artifact_type
        ]

    def register_cached_ref(
        self,
        *,
        ref_id: str,
        artifact_type: str,
        created_by: str,
        data: Any,
        role: str = "intermediate",
        subject: str = "unknown",
        projection_allowed: bool = True,
        debug_only: bool = False,
    ) -> Artifact:
        metadata = ArtifactMetadata(
            created_by=created_by,
            content_hash=stable_content_hash(data),
            semantic_role=role,
            subject=subject,
            projection_allowed=projection_allowed,
            debug_only=debug_only,
        )
        artifact = Artifact(
            artifact_id=ref_id,
            artifact_type=artifact_type,
            schema_version="1.0",
            data=data,
            metadata=metadata,
        )
        return self.put(artifact)
```

- [ ] **Step 4: Run store tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_store.py -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Export ArtifactStore**

Modify `src/agent/artifacts/__init__.py` to import and expose `ArtifactStore`:

```python
from .store import ArtifactStore
```

Add `"ArtifactStore"` to `__all__`.

- [ ] **Step 6: Run store and model tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_models.py tests/agent/artifacts/test_store.py -v
```

Expected:

```text
7 passed
```

- [ ] **Step 7: Commit ArtifactStore**

```bash
git add src/agent/artifacts/__init__.py src/agent/artifacts/store.py tests/agent/artifacts/test_store.py
git commit -m "feat: add typed artifact store"
```

---

### Task 6: Add projector registry and initial docaudit projectors

**Files:**
- Create: `src/agent/artifacts/projectors.py`
- Create: `tests/agent/artifacts/test_projectors.py`

- [ ] **Step 1: Write projector tests**

Create `tests/agent/artifacts/test_projectors.py` with:

```python
from __future__ import annotations

import pytest

from src.agent.artifacts.models import Artifact, ArtifactMetadata
from src.agent.artifacts.projectors import (
    ProjectorRegistry,
    create_default_projector_registry,
)


def _parsed_document_artifact() -> Artifact:
    return Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={
            "pages": [
                {
                    "page_content": {
                        "body": {
                            "title": {
                                "elements": [
                                    {"font": {"text": "关于开展安全生产检查的通知"}}
                                ]
                            },
                            "main_text": [
                                {
                                    "elements": [
                                        {"font": {"text": "第一段正文。"}},
                                        {"font": {"text": "第二句。"}},
                                    ]
                                },
                                {"elements": [{"font": {"text": "第二段正文。"}}]},
                            ],
                        }
                    }
                }
            ]
        },
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )


def _search_results_artifact() -> Artifact:
    return Artifact(
        artifact_id="$ref:search_documents:1",
        artifact_type="docaudit.search_results",
        data={
            "total": 2,
            "hits": [
                {
                    "title": "参考通知",
                    "chunk_text": "参考文本一",
                    "resource_id": 10,
                    "source_id": "s1",
                    "chunk_no": 0,
                },
                {
                    "title": "空文本",
                    "chunk_text": "",
                    "resource_id": 11,
                    "source_id": "s2",
                    "chunk_no": 1,
                },
            ],
        },
        metadata=ArtifactMetadata(
            created_by="search_documents",
            semantic_role="reference_document",
            subject="reference_library",
        ),
    )


def test_registry_rejects_duplicate_projector_names():
    registry = ProjectorRegistry()
    default_registry = create_default_projector_registry()
    projector = default_registry.get("docaudit.parsed_document.to_paragraph_list")

    registry.register(projector)
    with pytest.raises(ValueError, match="Duplicate projector"):
        registry.register(projector)


def test_parsed_document_projects_to_paragraph_list():
    registry = create_default_projector_registry()
    projector = registry.get("docaudit.parsed_document.to_paragraph_list")

    result = projector.project(_parsed_document_artifact(), constraints={})

    assert result.artifact.artifact_type == "docaudit.paragraph_list"
    paragraphs = result.artifact.data["paragraphs"]
    assert [p["text"] for p in paragraphs] == [
        "关于开展安全生产检查的通知",
        "第一段正文。第二句。",
        "第二段正文。",
    ]
    assert paragraphs[0]["section"] == "title"
    assert paragraphs[1]["section"] == "body"


def test_paragraph_list_projects_to_plain_text_body_only():
    registry = create_default_projector_registry()
    paragraph_projector = registry.get("docaudit.parsed_document.to_paragraph_list")
    text_projector = registry.get("docaudit.paragraph_list.to_plain_text")

    paragraph_result = paragraph_projector.project(_parsed_document_artifact(), constraints={})
    text_result = text_projector.project(
        paragraph_result.artifact,
        constraints={"source_scope": "body", "normalize_whitespace": True},
    )

    assert text_result.artifact.artifact_type == "core.plain_text"
    assert text_result.artifact.data["text"] == "第一段正文。第二句。\n第二段正文。"
    assert text_result.artifact.data["source_scope"] == "body"


def test_search_results_projects_to_reference_text_list():
    registry = create_default_projector_registry()
    projector = registry.get("docaudit.search_results.to_reference_text_list")

    result = projector.project(
        _search_results_artifact(),
        constraints={"min_text_chars": 1, "dedupe": True},
    )

    assert result.artifact.artifact_type == "docaudit.reference_text_list"
    assert result.artifact.data["items"] == [
        {
            "text": "参考文本一",
            "title": "参考通知",
            "resource_id": 10,
            "source_id": "s1",
            "chunk_no": 0,
            "score": None,
        }
    ]


def test_reference_text_list_projects_to_core_text_collection():
    registry = create_default_projector_registry()
    reference_projector = registry.get("docaudit.search_results.to_reference_text_list")
    collection_projector = registry.get("docaudit.reference_text_list.to_text_collection")

    reference_result = reference_projector.project(_search_results_artifact(), constraints={})
    collection_result = collection_projector.project(reference_result.artifact, constraints={})

    assert collection_result.artifact.artifact_type == "core.text_collection"
    assert collection_result.artifact.data["items"][0]["text"] == "参考文本一"
    assert collection_result.artifact.data["items"][0]["metadata"]["title"] == "参考通知"
```

- [ ] **Step 2: Run projector tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/artifacts/test_projectors.py -v
```

Expected:

```text
FAILED with ModuleNotFoundError or ImportError for projectors
```

- [ ] **Step 3: Implement projector registry and projectors**

Create `src/agent/artifacts/projectors.py` with:

```python
"""Projector registry and initial docaudit projectors."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .models import (
    Artifact,
    ArtifactMetadata,
    ProjectionDiagnostic,
    ProjectionQuality,
    ProjectorSpec,
    stable_content_hash,
)

ProjectorFn = Callable[[Artifact, dict[str, Any]], tuple[Any, ProjectionQuality, tuple[ProjectionDiagnostic, ...]]]


class Projector:
    """A deterministic ArtifactType-to-ArtifactType transformation."""

    def __init__(self, spec: ProjectorSpec, fn: ProjectorFn) -> None:
        self.spec = spec
        self._fn = fn

    def project(self, artifact: Artifact, constraints: dict[str, Any]) -> "ProjectionResult":
        if artifact.artifact_type != self.spec.source_type:
            raise ValueError(
                f"Projector {self.spec.name} expected {self.spec.source_type}, "
                f"got {artifact.artifact_type}"
            )
        unsupported = set(constraints) - set(self.spec.supported_constraints)
        if unsupported:
            raise ValueError(
                f"Projector {self.spec.name} does not support constraints: "
                f"{sorted(unsupported)}"
            )
        data, quality, diagnostics = self._fn(artifact, constraints)
        metadata = ArtifactMetadata(
            created_by=self.spec.name,
            source_refs=(artifact.artifact_id,),
            content_hash=stable_content_hash(data),
            semantic_role=artifact.metadata.semantic_role,
            subject=artifact.metadata.subject,
            quality=quality.model_dump(),
            lineage=artifact.metadata.lineage + (self.spec.name, ),
        )
        projected = Artifact(
            artifact_id=f"projected:{self.spec.target_type}:{metadata.content_hash[-12:]}",
            artifact_type=self.spec.target_type,
            schema_version="1.0",
            data=data,
            metadata=metadata,
        )
        return ProjectionResult(artifact=projected, quality=quality, diagnostics=diagnostics)


class ProjectionResult:
    """Result of one projector execution."""

    def __init__(
        self,
        *,
        artifact: Artifact,
        quality: ProjectionQuality,
        diagnostics: tuple[ProjectionDiagnostic, ...] = (),
    ) -> None:
        self.artifact = artifact
        self.quality = quality
        self.diagnostics = diagnostics


class ProjectorRegistry:
    """Registry of known projector edges."""

    def __init__(self) -> None:
        self._by_name: dict[str, Projector] = {}
        self._by_source: dict[str, list[Projector]] = {}

    def register(self, projector: Projector) -> None:
        if projector.spec.name in self._by_name:
            raise ValueError(f"Duplicate projector: {projector.spec.name}")
        self._by_name[projector.spec.name] = projector
        self._by_source.setdefault(projector.spec.source_type, []).append(projector)

    def get(self, name: str) -> Projector:
        return self._by_name[name]

    def outgoing(self, artifact_type: str) -> list[Projector]:
        return list(self._by_source.get(artifact_type, []))

    def all(self) -> list[Projector]:
        return list(self._by_name.values())


def create_default_projector_registry() -> ProjectorRegistry:
    registry = ProjectorRegistry()
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.parsed_document.to_paragraph_list",
                source_type="docaudit.parsed_document",
                target_type="docaudit.paragraph_list",
                owner="document",
                supported_constraints=("source_scope",),
                description="Extract document paragraphs in reading order.",
            ),
            _parsed_document_to_paragraph_list,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.paragraph_list.to_plain_text",
                source_type="docaudit.paragraph_list",
                target_type="core.plain_text",
                owner="document",
                supported_constraints=("source_scope", "normalize_whitespace", "max_chars"),
                description="Join paragraph texts into plain text.",
            ),
            _paragraph_list_to_plain_text,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.search_results.to_reference_text_list",
                source_type="docaudit.search_results",
                target_type="docaudit.reference_text_list",
                owner="search",
                supported_constraints=("max_items", "min_text_chars", "dedupe"),
                description="Extract reference chunks from search results.",
            ),
            _search_results_to_reference_text_list,
        )
    )
    registry.register(
        Projector(
            ProjectorSpec(
                name="docaudit.reference_text_list.to_text_collection",
                source_type="docaudit.reference_text_list",
                target_type="core.text_collection",
                owner="search",
                supported_constraints=("min_items",),
                description="Convert reference texts to generic text collection.",
            ),
            _reference_text_list_to_text_collection,
        )
    )
    return registry


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in paragraph.get("elements", []) or []:
        font = element.get("font", {}) if isinstance(element, dict) else {}
        text = font.get("text", "")
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


def _append_paragraph(
    paragraphs: list[dict[str, Any]],
    paragraph: dict[str, Any] | None,
    *,
    section: str,
    source_path: str,
) -> None:
    if not isinstance(paragraph, dict):
        return
    text = _paragraph_text(paragraph)
    if not text:
        return
    paragraphs.append(
        {
            "index": len(paragraphs),
            "text": text,
            "section": section,
            "source_path": source_path,
            "style": {
                "outline_level": paragraph.get("outline_level"),
                "alignment": paragraph.get("alignment"),
            },
        }
    )


def _parsed_document_to_paragraph_list(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    paragraphs: list[dict[str, Any]] = []
    for page_index, page in enumerate(artifact.data.get("pages", []) or []):
        body = ((page.get("page_content") or {}).get("body") or {})
        _append_paragraph(
            paragraphs,
            body.get("title"),
            section="title",
            source_path=f".pages[{page_index}].page_content.body.title",
        )
        for paragraph_index, paragraph in enumerate(body.get("main_text", []) or []):
            _append_paragraph(
                paragraphs,
                paragraph,
                section="body",
                source_path=(
                    f".pages[{page_index}].page_content.body.main_text[{paragraph_index}]"
                ),
            )
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()
    confidence = 1.0
    if not paragraphs:
        confidence = 0.0
        diagnostics = (
            ProjectionDiagnostic(
                level="warning",
                code="no_paragraphs_extracted",
                message="No paragraphs were extracted from parsed document.",
            ),
        )
    return (
        {"paragraphs": paragraphs},
        ProjectionQuality(
            confidence=confidence,
            lossiness="lossy",
            stats={"paragraphs": len(paragraphs)},
        ),
        diagnostics,
    )


def _paragraph_list_to_plain_text(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    source_scope = constraints.get("source_scope", "full_document")
    normalize = bool(constraints.get("normalize_whitespace", False))
    max_chars = constraints.get("max_chars")
    paragraphs = artifact.data.get("paragraphs", []) or []
    if source_scope == "body":
        paragraphs = [p for p in paragraphs if p.get("section") == "body"]
    texts = [p.get("text", "") for p in paragraphs if p.get("text")]
    text = "\n".join(texts)
    if normalize:
        text = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
    if isinstance(max_chars, int) and max_chars >= 0:
        text = text[:max_chars]
    return (
        {"text": text, "language": "zh", "source_scope": source_scope},
        ProjectionQuality(
            confidence=1.0 if text else 0.0,
            lossiness="lossy",
            stats={"chars": len(text)},
        ),
        (),
    )


def _search_results_to_reference_text_list(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    max_items = constraints.get("max_items")
    min_text_chars = int(constraints.get("min_text_chars", 1))
    dedupe = bool(constraints.get("dedupe", False))
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for hit in artifact.data.get("hits", []) or []:
        text = str(hit.get("chunk_text", "")).strip()
        if len(text) < min_text_chars:
            continue
        if dedupe and text in seen:
            continue
        seen.add(text)
        items.append(
            {
                "text": text,
                "title": hit.get("title"),
                "resource_id": hit.get("resource_id"),
                "source_id": hit.get("source_id"),
                "chunk_no": hit.get("chunk_no"),
                "score": hit.get("score"),
            }
        )
        if isinstance(max_items, int) and len(items) >= max_items:
            break
    return (
        {"items": items},
        ProjectionQuality(
            confidence=1.0 if items else 0.0,
            lossiness="lossy",
            stats={"items": len(items)},
        ),
        (),
    )


def _reference_text_list_to_text_collection(
    artifact: Artifact,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], ProjectionQuality, tuple[ProjectionDiagnostic, ...]]:
    items = [
        {
            "text": item.get("text", ""),
            "metadata": {k: v for k, v in item.items() if k != "text"},
        }
        for item in artifact.data.get("items", []) or []
        if item.get("text")
    ]
    return (
        {"items": items},
        ProjectionQuality(
            confidence=1.0 if items else 0.0,
            lossiness="lossy",
            stats={"items": len(items)},
        ),
        (),
    )
```

- [ ] **Step 4: Run projector tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_projectors.py -v
```

Expected:

```text
5 passed
```

- [ ] **Step 5: Commit projectors**

```bash
git add src/agent/artifacts/projectors.py tests/agent/artifacts/test_projectors.py
git commit -m "feat: add initial artifact projectors"
```

---

### Task 7: Add projection resolver

**Files:**
- Create: `src/agent/artifacts/resolver.py`
- Create: `tests/agent/artifacts/test_resolver.py`

- [ ] **Step 1: Write resolver tests**

Create `tests/agent/artifacts/test_resolver.py` with:

```python
from __future__ import annotations

from src.agent.artifacts.models import (
    Artifact,
    ArtifactMetadata,
    ContractField,
    ProjectionPolicy,
    ToolInputContract,
)
from src.agent.artifacts.projectors import create_default_projector_registry
from src.agent.artifacts.resolver import ProjectionResolver


def test_resolves_two_step_plain_text_path():
    artifact = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={"pages": []},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    contract = ToolInputContract(
        tool_name="detect_plagiarism",
        fields=(
            ContractField(
                name="new_doc",
                artifact_type="core.plain_text",
                role="primary_document",
                subject="current_upload",
                materialize_as="string",
                constraints={"source_scope": "body", "normalize_whitespace": True},
            ),
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        contract,
        [artifact],
        ProjectionPolicy(),
    )

    assert resolution.status == "resolved"
    plan = resolution.plans["new_doc"]
    assert [step.projector_name for step in plan.steps] == [
        "docaudit.parsed_document.to_paragraph_list",
        "docaudit.paragraph_list.to_plain_text",
    ]


def test_ignores_debug_artifact_by_default():
    artifact = Artifact(
        artifact_id="$ref:read_cached_output:1",
        artifact_type="core.plain_text",
        data={"text": "debug text"},
        metadata=ArtifactMetadata(
            created_by="read_cached_output",
            semantic_role="primary_document",
            subject="current_upload",
            projection_allowed=False,
            debug_only=True,
        ),
    )
    contract = ToolInputContract(
        tool_name="detect_plagiarism",
        fields=(
            ContractField(
                name="new_doc",
                artifact_type="core.plain_text",
                role="primary_document",
                subject="current_upload",
                materialize_as="string",
            ),
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        contract,
        [artifact],
        ProjectionPolicy(),
    )

    assert resolution.status == "failed"
    assert "new_doc" in resolution.diagnostics[0].message


def test_role_mismatch_fails_even_with_matching_type():
    artifact = Artifact(
        artifact_id="reference-text",
        artifact_type="core.plain_text",
        data={"text": "reference"},
        metadata=ArtifactMetadata(
            created_by="projector",
            semantic_role="reference_document",
            subject="reference_library",
        ),
    )
    contract = ToolInputContract(
        tool_name="detect_plagiarism",
        fields=(
            ContractField(
                name="new_doc",
                artifact_type="core.plain_text",
                role="primary_document",
                subject="current_upload",
                materialize_as="string",
            ),
        ),
    )

    resolution = ProjectionResolver(create_default_projector_registry()).resolve(
        contract,
        [artifact],
        ProjectionPolicy(),
    )

    assert resolution.status == "failed"
```

- [ ] **Step 2: Run resolver tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/artifacts/test_resolver.py -v
```

Expected:

```text
FAILED with ModuleNotFoundError or ImportError for resolver
```

- [ ] **Step 3: Implement resolver**

Create `src/agent/artifacts/resolver.py` with:

```python
"""Bounded projection graph resolver."""

from __future__ import annotations

from collections import deque

from .models import (
    Artifact,
    ContractField,
    MaterializerSpec,
    ProjectionDiagnostic,
    ProjectionPlan,
    ProjectionPolicy,
    ProjectionResolution,
    ProjectionStep,
    ToolInputContract,
)
from .projectors import ProjectorRegistry


class ProjectionResolver:
    """Resolve tool input contracts into projection plans."""

    def __init__(self, registry: ProjectorRegistry) -> None:
        self._registry = registry

    def resolve(
        self,
        contract: ToolInputContract,
        artifacts: list[Artifact],
        policy: ProjectionPolicy,
    ) -> ProjectionResolution:
        plans: dict[str, ProjectionPlan] = {}
        diagnostics: list[ProjectionDiagnostic] = []
        for field in contract.fields:
            plan = self._resolve_field(field, artifacts, policy)
            if plan is None:
                diagnostics.append(
                    ProjectionDiagnostic(
                        level="error",
                        code="field_unresolved",
                        message=f"Could not resolve required field {field.name}",
                        details={"required_type": field.artifact_type},
                    )
                )
            else:
                plans[field.name] = plan
        if diagnostics:
            return ProjectionResolution(status="failed", diagnostics=tuple(diagnostics))
        return ProjectionResolution(status="resolved", plans=plans)

    def _resolve_field(
        self,
        field: ContractField,
        artifacts: list[Artifact],
        policy: ProjectionPolicy,
    ) -> ProjectionPlan | None:
        candidates = [
            artifact
            for artifact in artifacts
            if self._artifact_allowed(artifact, field, policy)
        ]
        queue: deque[tuple[Artifact, tuple[ProjectionStep, ...], str]] = deque(
            (artifact, (), artifact.artifact_type) for artifact in candidates
        )
        visited: set[tuple[str, str, int]] = set()
        while queue:
            source_artifact, steps, current_type = queue.popleft()
            if len(steps) > policy.max_depth:
                continue
            if current_type == field.artifact_type:
                return ProjectionPlan(
                    field_name=field.name,
                    required_type=field.artifact_type,
                    source_artifact_id=source_artifact.artifact_id,
                    steps=steps,
                    materializer=MaterializerSpec(
                        artifact_type=field.artifact_type,
                        materialize_as=field.materialize_as,
                    ),
                    score=max(0.0, 1.0 - (0.05 * len(steps))),
                )
            key = (source_artifact.artifact_id, current_type, len(steps))
            if key in visited:
                continue
            visited.add(key)
            if len(steps) >= policy.max_depth:
                continue
            for projector in self._registry.outgoing(current_type):
                if projector.spec.stability == "disabled":
                    continue
                if projector.spec.stability == "deprecated" and not policy.allow_deprecated:
                    continue
                if projector.spec.stability == "experimental" and not policy.allow_experimental:
                    continue
                constraints = {
                    name: value
                    for name, value in field.constraints.items()
                    if name in projector.spec.supported_constraints
                }
                queue.append(
                    (
                        source_artifact,
                        steps
                        + (
                            ProjectionStep(
                                projector_name=projector.spec.name,
                                source_type=projector.spec.source_type,
                                target_type=projector.spec.target_type,
                                constraints=constraints,
                            ),
                        ),
                        projector.spec.target_type,
                    )
                )
        return None

    @staticmethod
    def _artifact_allowed(
        artifact: Artifact,
        field: ContractField,
        policy: ProjectionPolicy,
    ) -> bool:
        if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
            return False
        if not artifact.metadata.projection_allowed and not policy.allow_debug_artifacts:
            return False
        if artifact.metadata.semantic_role != field.role:
            return False
        if artifact.metadata.subject != field.subject:
            return False
        return True
```

- [ ] **Step 4: Run resolver tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_resolver.py -v
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit resolver**

```bash
git add src/agent/artifacts/resolver.py tests/agent/artifacts/test_resolver.py
git commit -m "feat: add artifact projection resolver"
```

---

### Task 8: Add projection executor and materializers

**Files:**
- Create: `src/agent/artifacts/executor.py`
- Create: `tests/agent/artifacts/test_executor.py`

- [ ] **Step 1: Write executor tests**

Create `tests/agent/artifacts/test_executor.py` with:

```python
from __future__ import annotations

from src.agent.artifacts.executor import MaterializerRegistry, ProjectionExecutor
from src.agent.artifacts.models import Artifact, ArtifactMetadata, MaterializerSpec, ProjectionPlan, ProjectionStep
from src.agent.artifacts.projectors import create_default_projector_registry
from src.agent.artifacts.store import ArtifactStore


def test_materializes_plain_text_as_string():
    registry = MaterializerRegistry.default()
    artifact = Artifact(
        artifact_id="plain",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    value = registry.materialize(artifact, MaterializerSpec(artifact_type="core.plain_text", materialize_as="string"))

    assert value == "hello"


def test_executor_runs_two_step_plan_and_materializes_text():
    store = ArtifactStore()
    source = Artifact(
        artifact_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        data={
            "pages": [
                {
                    "page_content": {
                        "body": {
                            "main_text": [
                                {"elements": [{"font": {"text": "正文"}}]},
                            ]
                        }
                    }
                }
            ]
        },
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role="primary_document",
            subject="current_upload",
        ),
    )
    store.put(source)
    plan = ProjectionPlan(
        field_name="new_doc",
        required_type="core.plain_text",
        source_artifact_id=source.artifact_id,
        steps=(
            ProjectionStep(
                projector_name="docaudit.parsed_document.to_paragraph_list",
                source_type="docaudit.parsed_document",
                target_type="docaudit.paragraph_list",
                constraints={},
            ),
            ProjectionStep(
                projector_name="docaudit.paragraph_list.to_plain_text",
                source_type="docaudit.paragraph_list",
                target_type="core.plain_text",
                constraints={"source_scope": "body"},
            ),
        ),
        materializer=MaterializerSpec(artifact_type="core.plain_text", materialize_as="string"),
    )

    result = ProjectionExecutor(
        projector_registry=create_default_projector_registry(),
        materializer_registry=MaterializerRegistry.default(),
        artifact_store=store,
    ).execute(plan)

    assert result.value == "正文"
    assert store.get(result.artifact_id).artifact_type == "core.plain_text"
```

- [ ] **Step 2: Run executor tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/artifacts/test_executor.py -v
```

Expected:

```text
FAILED with ModuleNotFoundError or ImportError for executor
```

- [ ] **Step 3: Implement executor**

Create `src/agent/artifacts/executor.py` with:

```python
"""Projection execution and materialization."""

from __future__ import annotations

from typing import Any

from .models import Artifact, MaterializedBinding, MaterializerSpec, ProjectionPlan
from .projectors import ProjectorRegistry
from .store import ArtifactStore


class MaterializerRegistry:
    """Registry for converting artifacts into concrete tool arguments."""

    def __init__(self) -> None:
        self._materializers: dict[tuple[str, str], Any] = {}

    @classmethod
    def default(cls) -> "MaterializerRegistry":
        registry = cls()
        registry.register("core.plain_text", "string", lambda artifact: artifact.data.get("text", ""))
        registry.register(
            "core.text_collection",
            "list_string",
            lambda artifact: [item.get("text", "") for item in artifact.data.get("items", [])],
        )
        return registry

    def register(self, artifact_type: str, materialize_as: str, fn: Any) -> None:
        self._materializers[(artifact_type, materialize_as)] = fn

    def materialize(self, artifact: Artifact, spec: MaterializerSpec) -> Any:
        key = (spec.artifact_type, spec.materialize_as)
        if artifact.artifact_type != spec.artifact_type:
            raise ValueError(
                f"Materializer expected {spec.artifact_type}, got {artifact.artifact_type}"
            )
        if key not in self._materializers:
            raise KeyError(f"No materializer registered for {key}")
        return self._materializers[key](artifact)


class ProjectionExecutor:
    """Execute projection plans using registered projectors."""

    def __init__(
        self,
        *,
        projector_registry: ProjectorRegistry,
        materializer_registry: MaterializerRegistry,
        artifact_store: ArtifactStore,
    ) -> None:
        self._projectors = projector_registry
        self._materializers = materializer_registry
        self._store = artifact_store

    def execute(self, plan: ProjectionPlan) -> MaterializedBinding:
        current = self._store.require(plan.source_artifact_id)
        for step in plan.steps:
            projector = self._projectors.get(step.projector_name)
            result = projector.project(current, step.constraints)
            current = self._store.put(result.artifact)
        value = self._materializers.materialize(current, plan.materializer)
        return MaterializedBinding(
            value=value,
            artifact_id=current.artifact_id,
            materializer=f"{plan.materializer.artifact_type}.{plan.materializer.materialize_as}",
        )
```

- [ ] **Step 4: Run executor tests**

Run:

```bash
uv run pytest tests/agent/artifacts/test_executor.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit executor**

```bash
git add src/agent/artifacts/executor.py tests/agent/artifacts/test_executor.py
git commit -m "feat: add artifact projection executor"
```

---

### Task 9: Add tool contracts to protocol and plagiarism tool

**Files:**
- Modify: `src/agent/tools/protocol.py`
- Modify: `src/agent/skills/plagiarism/tools.py`
- Create: `tests/agent/tools/test_plagiarism_contracts.py`

- [ ] **Step 1: Write plagiarism contract tests**

Create `tests/agent/tools/test_plagiarism_contracts.py` with:

```python
from src.agent.skills.plagiarism.tools import DetectPlagiarismTool


def test_detect_plagiarism_declares_input_contract():
    tool = DetectPlagiarismTool()

    contract = tool.input_contract

    assert contract.tool_name == "detect_plagiarism"
    assert contract.require_contract_binding is True
    assert contract.required_fields == ("new_doc", "library_docs")


def test_detect_plagiarism_declares_output_contract():
    tool = DetectPlagiarismTool()

    output = tool.output_contract.outputs["result"]

    assert output.artifact_type == "docaudit.plagiarism_report"
    assert output.projection_allowed is True
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/tools/test_plagiarism_contracts.py -v
```

Expected:

```text
FAILED with AttributeError: 'DetectPlagiarismTool' object has no attribute 'input_contract'
```

- [ ] **Step 3: Extend `ToolProtocol` optional attributes**

In `src/agent/tools/protocol.py`, inside `ToolProtocol`, add these attributes after `output_content_type`:

```python
    input_contract: Any | None
    output_contract: Any | None
    runtime_policy: Any | None
```

The file already imports `Any`, so no import change is needed.

- [ ] **Step 4: Add contracts to `DetectPlagiarismTool`**

In `src/agent/skills/plagiarism/tools.py`, add imports:

```python
from src.agent.artifacts.models import (
    ContractField,
    OutputArtifactContract,
    ToolInputContract,
    ToolOutputContract,
)
```

Then inside `DetectPlagiarismTool`, after `parameters`, add:

```python
    input_contract = ToolInputContract(
        tool_name="detect_plagiarism",
        require_contract_binding=True,
        fields=(
            ContractField(
                name="new_doc",
                artifact_type="core.plain_text",
                role="primary_document",
                subject="current_upload",
                materialize_as="string",
                constraints={
                    "source_scope": "body",
                    "normalize_whitespace": True,
                    "min_chars": 20,
                },
            ),
            ContractField(
                name="library_docs",
                artifact_type="core.text_collection",
                role="reference_document",
                subject="reference_library",
                materialize_as="list_string",
                constraints={
                    "min_items": 1,
                    "item_min_chars": 20,
                    "dedupe": True,
                },
            ),
        ),
    )
    output_contract = ToolOutputContract(
        tool_name="detect_plagiarism",
        outputs={
            "result": OutputArtifactContract(
                artifact_type="docaudit.plagiarism_report",
                role="report",
                subject="current_upload",
            )
        },
    )
```

- [ ] **Step 5: Run contract tests**

Run:

```bash
uv run pytest tests/agent/tools/test_plagiarism_contracts.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit contracts**

```bash
git add src/agent/tools/protocol.py src/agent/skills/plagiarism/tools.py tests/agent/tools/test_plagiarism_contracts.py
git commit -m "feat: declare plagiarism tool artifact contracts"
```

---

### Task 10: Add ToolRegistry artifact auto-binding

**Files:**
- Modify: `src/agent/tools/registry.py`
- Create: `tests/agent/test_tool_contract_binding.py`

- [ ] **Step 1: Write ToolRegistry auto-binding integration test**

Create `tests/agent/test_tool_contract_binding.py` with:

```python
from __future__ import annotations

import pytest

from src.agent.artifacts.store import ArtifactStore
from src.agent.skills.plagiarism.tools import DetectPlagiarismTool
from src.agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_detect_plagiarism_auto_binds_from_artifacts():
    store = ArtifactStore()
    store.register_cached_ref(
        ref_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_document",
        data={
            "pages": [
                {
                    "page_content": {
                        "body": {
                            "main_text": [
                                {
                                    "elements": [
                                        {"font": {"text": "为深入贯彻落实安全生产要求，开展检查工作。"}}
                                    ]
                                }
                            ]
                        }
                    }
                }
            ]
        },
        role="primary_document",
        subject="current_upload",
    )
    store.register_cached_ref(
        ref_id="$ref:search_documents:1",
        artifact_type="docaudit.search_results",
        created_by="search_documents",
        data={
            "hits": [
                {
                    "title": "参考",
                    "chunk_text": "为深入贯彻落实安全生产要求，开展检查工作。",
                    "resource_id": 1,
                }
            ]
        },
        role="reference_document",
        subject="reference_library",
    )

    registry = ToolRegistry()
    registry.register(DetectPlagiarismTool())

    result = await registry.execute("detect_plagiarism", artifact_store=store)

    assert result.success is True
    assert result.metadata["artifact_bindings"]["new_doc"].startswith("projected:core.plain_text")
    assert result.metadata["artifact_bindings"]["library_docs"].startswith("projected:core.text_collection")


@pytest.mark.asyncio
async def test_detect_plagiarism_binding_failure_does_not_call_tool():
    store = ArtifactStore()
    store.register_cached_ref(
        ref_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_document",
        data={"pages": []},
        role="primary_document",
        subject="current_upload",
    )

    registry = ToolRegistry()
    registry.register(DetectPlagiarismTool())

    result = await registry.execute("detect_plagiarism", artifact_store=store)

    assert result.success is False
    assert "Could not resolve required field library_docs" in result.error
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/agent/test_tool_contract_binding.py -v
```

Expected:

```text
FAILED because ToolRegistry.execute does not accept artifact_store
```

- [ ] **Step 3: Implement auto-binding in ToolRegistry**

In `src/agent/tools/registry.py`, add imports:

```python
from src.agent.artifacts.executor import MaterializerRegistry, ProjectionExecutor
from src.agent.artifacts.models import ProjectionPolicy
from src.agent.artifacts.projectors import create_default_projector_registry
from src.agent.artifacts.resolver import ProjectionResolver
from src.agent.artifacts.store import ArtifactStore
```

Update the `execute` signature to include:

```python
        artifact_store: ArtifactStore | None = None,
```

before `**kwargs`.

Before ref resolution, add:

```python
        artifact_bindings: dict[str, str] = {}
        input_contract = getattr(tool, "input_contract", None)
        if artifact_store is not None and input_contract is not None:
            binding_result = self._bind_contract_arguments(
                input_contract=input_contract,
                artifact_store=artifact_store,
                explicit_kwargs=kwargs,
            )
            if not binding_result.success:
                return binding_result
            kwargs = {**kwargs, **binding_result.data["arguments"]}
            artifact_bindings = binding_result.data["artifact_bindings"]
```

After tool execution, when creating the final `ToolResult`, preserve binding metadata by adding:

```python
        if artifact_bindings:
            result = ToolResult(
                success=result.success,
                data=result.data,
                error=result.error,
                metadata={**result.metadata, "artifact_bindings": artifact_bindings},
            )
```

Add this private method to `ToolRegistry`:

```python
    def _bind_contract_arguments(
        self,
        *,
        input_contract: Any,
        artifact_store: ArtifactStore,
        explicit_kwargs: dict[str, Any],
    ) -> ToolResult:
        missing_fields = [
            field
            for field in input_contract.fields
            if field.required and field.name not in explicit_kwargs
        ]
        if not missing_fields:
            return ToolResult(success=True, data={"arguments": {}, "artifact_bindings": {}})

        partial_contract = input_contract.model_copy(update={"fields": tuple(missing_fields)})
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(
            partial_contract,
            artifact_store.list_projection_candidates(),
            ProjectionPolicy(),
        )
        if resolution.status != "resolved":
            messages = [diagnostic.message for diagnostic in resolution.diagnostics]
            return ToolResult(success=False, error="; ".join(messages))

        executor = ProjectionExecutor(
            projector_registry=create_default_projector_registry(),
            materializer_registry=MaterializerRegistry.default(),
            artifact_store=artifact_store,
        )
        arguments: dict[str, Any] = {}
        artifact_bindings: dict[str, str] = {}
        for name, plan in resolution.plans.items():
            binding = executor.execute(plan)
            arguments[name] = binding.value
            artifact_bindings[name] = binding.artifact_id
        return ToolResult(
            success=True,
            data={"arguments": arguments, "artifact_bindings": artifact_bindings},
        )
```

- [ ] **Step 4: Run ToolRegistry binding tests**

Run:

```bash
uv run pytest tests/agent/test_tool_contract_binding.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Run registry tests**

Run:

```bash
uv run pytest tests/agent/test_registry.py tests/agent/test_tool_contract_binding.py -v
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit ToolRegistry auto-binding**

```bash
git add src/agent/tools/registry.py tests/agent/test_tool_contract_binding.py
git commit -m "feat: auto-bind tool inputs from typed artifacts"
```

---

### Task 11: Prevent micro-compaction from creating read_cached_output recovery refs

**Files:**
- Modify: `src/agent/core/context_manager.py`
- Modify: `tests/agent/test_context_manager.py`

- [ ] **Step 1: Add micro-compaction regression test**

Append this test inside `class TestLayer2MicroCompact` in `tests/agent/test_context_manager.py`:

```python
    def test_read_cached_output_tool_message_is_not_repersisted(self, mgr):
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(
                role="tool",
                content='{"success": true, "data": ["debug data"], "error": null}',
                tool_call_id="t1",
                name="read_cached_output",
            ),
            Message(role="tool", content='{"data":"new1"}', tool_call_id="t2", name="tool2"),
            Message(role="tool", content='{"data":"new2"}', tool_call_id="t3", name="tool3"),
        )

        result = mgr.micro_compact(msgs)
        payload = json.loads(result[2].content)

        assert payload["_omitted"] is True
        assert payload["tool"] == "read_cached_output"
        assert "ref_id" not in payload
        assert "$ref:read_cached_output:1" not in mgr.state.ref_map
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
uv run pytest tests/agent/test_context_manager.py::TestLayer2MicroCompact::test_read_cached_output_tool_message_is_not_repersisted -v
```

Expected:

```text
FAILED because ref_id is present or ref_map contains read_cached_output ref
```

- [ ] **Step 3: Skip persistence for read_cached_output in micro-compaction**

In `src/agent/core/context_manager.py`, modify the compaction block around current lines 254-257 to:

```python
                ref_id = self._extract_ref_id(msg)
                if ref_id is None and tool_name != "read_cached_output":
                    ref_id = self._persist_tool_message(msg, tool_name)
```

Also change the placeholder note around lines 262-264 to:

```python
                    "note": "Result omitted by micro-compact (old tool result).",
```

Then add a conditional recovery note:

```python
                if tool_name != "read_cached_output":
                    placeholder["note"] += " Use read_cached_output with ref_id to recover full data."
                else:
                    placeholder["note"] += " Debug outputs are not re-persisted for chained recovery."
```

- [ ] **Step 4: Update ref instructions to discourage read_cached_output for argument construction**

In `ContextManager.get_ref_instructions`, replace the returned Chinese instruction with:

```python
        return (
            "工具结果可能保存为 $ref 缓存引用。构造下游工具参数时，"
            "优先直接把业务 $ref 传给目标工具，系统会自动加载或适配参数。"
            "不要为了构造下游工具参数调用 read_cached_output；"
            "read_cached_output 仅用于调试查看缓存内容。\n"
            "当工具返回 __persisted_output__ 标记时，预览通常已足够理解结果。"
            "只有在人工调试且确实需要查看原始缓存时，才调用 read_cached_output。\n"
            "旧工具结果可能被微压缩为 _omitted 占位符。"
            "普通业务流程不应依赖恢复这些调试内容。"
        )
```

- [ ] **Step 5: Run context manager tests**

Run:

```bash
uv run pytest tests/agent/test_context_manager.py -v
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit micro-compaction guard**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "fix: stop re-persisting debug cache outputs"
```

---

### Task 12: Register parsed document artifacts in orchestrator

**Files:**
- Modify: `src/agent/agents/orch.py`
- Modify: `src/agent/core/loop.py`
- Modify: `tests/agent/test_orchestrator.py`

- [ ] **Step 1: Add orchestrator artifact registration test**

Append this test to `tests/agent/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_orchestrator_registers_parsed_document_artifact(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from src.agent.agents.orch import OrchestratorAgent
    from src.agent.artifacts.store import ArtifactStore
    from src.agent.core.context_manager import ContextManager
    from src.agent.testing import MockModelClient

    parsed_doc = {
        "user_id": "",
        "doc_id": "d1",
        "total_page_num": 1,
        "pages": [],
    }
    parser = MagicMock()
    parser.name = "ParserAgent"
    parser.parse = AsyncMock(return_value=parsed_doc)
    model = MockModelClient(tool_calls=[])
    context_manager = ContextManager(model=model, cache_dir=str(tmp_path / "cache"))
    artifact_store = ArtifactStore()
    orch = OrchestratorAgent(parser=parser, model=model)

    await orch.run(
        task="Audit document",
        context={"file_path": "/fake.docx"},
        context_manager=context_manager,
        artifact_store=artifact_store,
    )

    artifact = artifact_store.get("$ref:parse_document:1")
    assert artifact is not None
    assert artifact.artifact_type == "docaudit.parsed_document"
    assert artifact.metadata.semantic_role == "primary_document"
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
uv run pytest tests/agent/test_orchestrator.py::test_orchestrator_registers_parsed_document_artifact -v
```

Expected:

```text
FAILED because OrchestratorAgent.run does not accept artifact_store
```

- [ ] **Step 3: Add artifact_store parameter to `agent_loop`**

In `src/agent/core/loop.py`, add TYPE_CHECKING import:

```python
    from ..artifacts.store import ArtifactStore
```

Add parameter to `agent_loop` signature:

```python
    artifact_store: ArtifactStore | None = None,
```

Pass it to `tool_registry.execute`:

```python
                    artifact_store=artifact_store,
```

- [ ] **Step 4: Add artifact_store parameter to `OrchestratorAgent.run`**

In `src/agent/agents/orch.py`, add parameter to `run`:

```python
        artifact_store: Any = None,
```

After `ref_id = context_manager.force_persist("parse_document", parsed_doc)` and before logging, add:

```python
                        if artifact_store is not None:
                            artifact_store.register_cached_ref(
                                ref_id=ref_id,
                                artifact_type="docaudit.parsed_document",
                                created_by="parse_document",
                                data=parsed_doc,
                                role="primary_document",
                                subject="current_upload",
                            )
```

When this run method later calls the parent agent loop, pass:

```python
artifact_store=artifact_store,
```

If there is no existing `agent_loop(...)` call visible near this block, search in this same method for `super().run` or `agent_loop` and thread the parameter through that call.

- [ ] **Step 5: Run orchestrator artifact test**

Run:

```bash
uv run pytest tests/agent/test_orchestrator.py::test_orchestrator_registers_parsed_document_artifact -v
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Run orchestrator tests**

Run:

```bash
uv run pytest tests/agent/test_orchestrator.py -v
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit orchestrator artifact registration**

```bash
git add src/agent/agents/orch.py src/agent/core/loop.py tests/agent/test_orchestrator.py
git commit -m "feat: register parsed documents as typed artifacts"
```

---

### Task 13: Add full plagiarism auto-binding regression

**Files:**
- Modify: `tests/agent/test_tool_contract_binding.py`

- [ ] **Step 1: Add regression test showing no read_cached_output is needed**

Append this test to `tests/agent/test_tool_contract_binding.py`:

```python
@pytest.mark.asyncio
async def test_plagiarism_auto_binding_uses_business_artifacts_not_debug_artifacts():
    store = ArtifactStore()
    store.register_cached_ref(
        ref_id="$ref:parse_document:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_document",
        data={
            "pages": [
                {
                    "page_content": {
                        "body": {
                            "main_text": [
                                {"elements": [{"font": {"text": "目标文档正文内容超过二十个字符。"}}]}
                            ]
                        }
                    }
                }
            ]
        },
        role="primary_document",
        subject="current_upload",
    )
    store.register_cached_ref(
        ref_id="$ref:search_documents:1",
        artifact_type="docaudit.search_results",
        created_by="search_documents",
        data={
            "hits": [
                {
                    "title": "参考文档",
                    "chunk_text": "目标文档正文内容超过二十个字符。",
                    "resource_id": 1,
                }
            ]
        },
        role="reference_document",
        subject="reference_library",
    )
    store.register_cached_ref(
        ref_id="$ref:read_cached_output:1",
        artifact_type="core.plain_text",
        created_by="read_cached_output",
        data={"text": "debug should not be used"},
        role="primary_document",
        subject="current_upload",
        projection_allowed=False,
        debug_only=True,
    )

    registry = ToolRegistry()
    registry.register(DetectPlagiarismTool())

    result = await registry.execute("detect_plagiarism", artifact_store=store)

    assert result.success is True
    assert result.metadata["artifact_bindings"]["new_doc"] != "$ref:read_cached_output:1"
    assert result.metadata["artifact_bindings"]["library_docs"] != "$ref:read_cached_output:1"
```

- [ ] **Step 2: Run regression test**

Run:

```bash
uv run pytest tests/agent/test_tool_contract_binding.py::test_plagiarism_auto_binding_uses_business_artifacts_not_debug_artifacts -v
```

Expected:

```text
1 passed
```

- [ ] **Step 3: Commit regression**

```bash
git add tests/agent/test_tool_contract_binding.py
git commit -m "test: prove plagiarism binding ignores debug artifacts"
```

---

### Task 14: Run focused verification suite

**Files:**
- No code changes expected.

- [ ] **Step 1: Run artifact tests**

Run:

```bash
uv run pytest tests/agent/artifacts -v
```

Expected:

```text
passed
```

- [ ] **Step 2: Run tool and loop regression tests**

Run:

```bash
uv run pytest tests/agent/test_loop.py tests/agent/test_context_manager.py tests/agent/test_registry.py tests/agent/test_tool_contract_binding.py tests/agent/tools/test_read_cached.py tests/agent/tools/test_search.py tests/agent/tools/test_plagiarism.py tests/agent/tools/test_plagiarism_contracts.py -v
```

Expected:

```text
passed
```

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 4: Commit any verification-only adjustments**

If no files changed, skip this commit. If test-only fixes were needed, commit them:

```bash
git add <changed-files>
git commit -m "test: stabilize artifact contract verification"
```

---

### Task 15: Request code review and address findings

**Files:**
- Files changed by previous tasks.

- [ ] **Step 1: Run Python reviewer**

Use the `python-reviewer` agent with this prompt:

```text
Review the artifact projector contract implementation for Python correctness, type clarity, pytest quality, and maintainability. Focus on changed files in src/agent/artifacts, ToolRegistry contract binding, ContextManager/read_cached_output loop guards, and plagiarism/search tool contracts. Return CRITICAL/HIGH/MEDIUM/LOW findings with file:line references.
```

Expected: reviewer returns findings.

- [ ] **Step 2: Run security reviewer**

Use the `security-reviewer` agent with this prompt:

```text
Review the artifact projector contract implementation for security and data isolation issues. Focus on cache ref handling, read_cached_output chain blocking, artifact auto-binding, and whether debug artifacts can leak into business tool inputs. Return CRITICAL/HIGH/MEDIUM/LOW findings with file:line references.
```

Expected: reviewer returns findings.

- [ ] **Step 3: Fix CRITICAL and HIGH findings**

For each CRITICAL/HIGH finding, write or update a failing test first, then implement the minimal fix. Use existing task patterns:

```bash
uv run pytest <specific-test> -v
```

Expected before fix: failing test reproduces finding.

Expected after fix: specific test passes.

- [ ] **Step 4: Re-run focused verification**

Run:

```bash
uv run pytest tests/agent/artifacts tests/agent/test_tool_contract_binding.py tests/agent/test_loop.py tests/agent/test_context_manager.py tests/agent/tools/test_read_cached.py tests/agent/tools/test_search.py -v
```

Expected:

```text
passed
```

- [ ] **Step 5: Commit review fixes**

If fixes were made:

```bash
git add <changed-files>
git commit -m "fix: address artifact contract review findings"
```

If no fixes were required, skip this commit.

---

## Self-Review

### Spec coverage

- Typed artifact models: Task 4.
- Artifact store / metadata wrapper: Task 5 and Task 12.
- Projector model and registry: Task 6.
- Initial projector set: Task 6.
- Resolver: Task 7.
- Executor/materializer: Task 8.
- Tool input/output contracts: Task 9.
- ToolRegistry auto-binding: Task 10.
- `read_cached_output` debug isolation: Tasks 2 and 11.
- Low-signal search guard: Task 3.
- Loop safety: Tasks 1 and 2.
- Plagiarism vertical slice: Tasks 9, 10, 13.
- Testing and verification: Tasks 14 and 15.

### Placeholder scan

This plan intentionally contains no unresolved placeholder markers or fill-in implementation steps. Where later review findings are unknown, Task 15 gives a concrete test-first process and exact verification commands.

### Type consistency

The plan consistently uses:

- `ArtifactStore`
- `ArtifactMetadata`
- `ToolInputContract`
- `ToolOutputContract`
- `ProjectionResolver`
- `ProjectionExecutor`
- `MaterializerRegistry`
- `MaterializedBinding`

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-02-artifact-projector-contract-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.

2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
