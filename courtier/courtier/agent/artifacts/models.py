"""Typed artifact and projection contract models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

# InputField is defined in the plugin SDK so plugins can declare artifact
# input contracts without depending on the core application.  Re-exported
# here to keep a single definition on both sides of the JSON-RPC boundary.
from courtier_plugin_sdk.models import InputField  # noqa: F401
from pydantic import BaseModel, Field

ProjectorStability = Literal["stable", "experimental", "deprecated", "disabled"]
ProjectorLayer = Literal["core", "domain", "plugin", "local"]
ProjectorCost = Literal["free", "cheap", "moderate", "expensive"]
Lossiness = Literal["lossless", "lossy", "summary"]


class ArtifactSchema(BaseModel, frozen=True):
    """Describes the data shape for an ArtifactType.

    Used to validate artifact data at rest and after projection steps.
    May be represented as JSON Schema, Pydantic model reference, or
    a simple type-name hint for lightweight validation.
    """

    schema_version: str = "1.0"
    schema_format: Literal["json_schema", "pydantic", "type_hint"] = "type_hint"
    schema_body: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


_artifact_type_schemas_cache: dict[str, ArtifactSchema] | None = None


def get_artifact_type_schemas() -> dict[str, ArtifactSchema]:
    """Return the registered artifact type schemas, lazy-initialised.

    This avoids import-time side effects (pydantic model instantiation).
    """
    global _artifact_type_schemas_cache
    if _artifact_type_schemas_cache is None:
        _artifact_type_schemas_cache = _build_artifact_type_schemas()
    return _artifact_type_schemas_cache


def _build_artifact_type_schemas() -> dict[str, ArtifactSchema]:
    """Build the full artifact type schema registry."""
    schemas: dict[str, ArtifactSchema] = {}

    # -- Core types ----------------------------------------------------------
    schemas["core.plain_text"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string"},
                "source_scope": {
                    "type": "string",
                    "enum": [
                        "full_document",
                        "body",
                        "header",
                        "footer",
                        "mixed",
                        "unknown",
                    ],
                },
            },
        },
        description="Plain text with optional language and scope metadata.",
    )

    schemas["core.text_collection"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                    },
                },
            },
        },
        description="Ordered collection of text items with optional per-item metadata.",
    )

    schemas["core.json_object"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description=(
            "Fallback type for arbitrary JSON objects. "
            "Tool contracts should not casually require this."
        ),
    )

    schemas["core.document_markdown"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["markdown"],
            "properties": {
                "markdown": {"type": "string"},
                "format": {"type": "string"},
                "ocr": {"type": "boolean"},
                "pages": {"type": "integer"},
            },
        },
        description=("GitHub-Flavored Markdown rendering of a document (optionally OCR'd)."),
    )

    schemas["core.error_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["errors"],
            "properties": {
                "errors": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "string"},
            },
        },
        description="Structured error/warning report from an audit or validation tool.",
    )

    schemas["core.debug_view"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description="Debug-only view of cached tool output. Not for business data flow.",
    )

    schemas["core.cached_output"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={"type": "object"},
        description=(
            "Fallback type for tool outputs without an explicit artifact type contract.  "
            "Always projection-allowed so downstream tools can discover and use the data."
        ),
    )

    # -- Docaudit types ------------------------------------------------------
    schemas["docaudit.parsed_layout"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["pages"],
            "properties": {"pages": {"type": "array"}},
        },
        description="Parsed document with pages, each containing body/header/footer structure.",
    )

    schemas["docaudit.paragraph_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["paragraphs"],
            "properties": {
                "paragraphs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text", "section"],
                        "properties": {
                            "index": {"type": "integer"},
                            "text": {"type": "string"},
                            "section": {"type": "string"},
                            "source_path": {"type": "string"},
                            "style": {"type": "object"},
                        },
                    },
                },
            },
        },
        description="Ordered list of document paragraphs extracted from parsed document.",
    )

    schemas["docaudit.search_results"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["total", "hits"],
            "properties": {
                "total": {"type": "integer"},
                "took_ms": {"type": "integer"},
                "hits": {"type": "array"},
            },
        },
        description="Raw search results from document search, containing total count and hit list.",
    )

    schemas["docaudit.document_structure"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["sections"],
            "properties": {
                "sections": {"type": "array"},
                "outline": {"type": "array"},
                "page_count": {"type": "integer"},
            },
        },
        description="Structural analysis of a document (sections, outline, page layout).",
    )

    schemas["docaudit.audit_finding_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "message"],
                        "properties": {
                            "severity": {"type": "string"},
                            "message": {"type": "string"},
                            "location": {"type": "string"},
                            "rule": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
        description="List of audit findings produced by any audit agent.",
    )

    schemas["docaudit.audit_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["auditor", "findings"],
            "properties": {
                "auditor": {"type": "string"},
                "doc_type": {"type": "string"},
                "findings": {"type": "array"},
                "passed": {"type": "boolean"},
                "summary": {"type": "string"},
                "details": {"type": "object"},
            },
        },
        description="Aggregated audit report from a single auditor.",
    )

    schemas["docaudit.reference_text_list"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "title": {"type": "string"},
                            "resource_id": {"type": "integer"},
                            "source_id": {"type": "string"},
                            "chunk_no": {"type": "integer"},
                            "score": {"type": "number"},
                        },
                    },
                },
            },
        },
        description="Filtered reference text items extracted from search results.",
    )

    schemas["docaudit.plagiarism_report"] = ArtifactSchema(
        schema_version="1.0",
        schema_format="type_hint",
        schema_body={
            "type": "object",
            "required": ["is_plagiarism", "max_similarity"],
            "properties": {
                "is_plagiarism": {"type": "boolean"},
                "max_similarity": {"type": "number"},
                "dynamic_threshold": {"type": "number"},
                "matched_doc_index": {"type": "integer"},
                "matched_substring_length": {"type": "integer"},
                "matched_text": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        description="Plagiarism detection report.",
    )

    return schemas


def get_artifact_schema(artifact_type: str) -> ArtifactSchema | None:
    """Return the registered ArtifactSchema for *artifact_type*, or None."""
    return get_artifact_type_schemas().get(artifact_type)


def register_artifact_schema(artifact_type: str, schema: ArtifactSchema) -> None:
    """Register a new artifact type schema (idempotent — last write wins)."""
    get_artifact_type_schemas()[artifact_type] = schema


def validate_artifact_data(artifact_type: str, data: Any) -> list[str]:
    """Lightweight structural validation of *data* against the registered schema.

    Returns a list of validation error messages (empty = valid).
    Only enforces ``required`` top-level keys and their declared types when the
    schema has ``schema_format="type_hint"``.  Full JSON Schema validation can
    be added later without changing the call sites.
    """
    schema = get_artifact_type_schemas().get(artifact_type)
    if schema is None:
        return []  # unknown types pass through
    if schema.schema_format != "type_hint":
        return []
    body = schema.schema_body or {}
    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append(f"{artifact_type}: expected dict, got {type(data).__name__}")
        return errors
    for key in body.get("required", []):
        if key not in data:
            errors.append(f"{artifact_type}: missing required key '{key}'")
    for key, prop_schema in body.get("properties", {}).items():
        if key not in data:
            continue
        value = data[key]
        expected_type = prop_schema.get("type", "")
        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"{artifact_type}.{key}: expected string, got {type(value).__name__}")
        elif expected_type == "integer" and not isinstance(value, int):
            errors.append(f"{artifact_type}.{key}: expected integer, got {type(value).__name__}")
        elif expected_type == "number" and not isinstance(value, (int, float)):
            errors.append(f"{artifact_type}.{key}: expected number, got {type(value).__name__}")
        elif expected_type == "boolean" and not isinstance(value, bool):
            errors.append(f"{artifact_type}.{key}: expected boolean, got {type(value).__name__}")
        elif expected_type == "array" and not isinstance(value, list):
            errors.append(f"{artifact_type}.{key}: expected array, got {type(value).__name__}")
        elif expected_type == "object" and not isinstance(value, dict):
            errors.append(f"{artifact_type}.{key}: expected object, got {type(value).__name__}")
    return errors


class ArtifactMetadata(BaseModel, frozen=True):
    """Metadata attached to an artifact instance."""

    created_by: str
    source_refs: tuple[str, ...] = ()
    content_hash: str = ""
    schema_version: str = "1.0"
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
    source_schema_version: str = ">=1.0,<2.0"
    target_schema_version: str = "1.0"
    owner: str
    layer: ProjectorLayer = "domain"
    stability: ProjectorStability = "stable"
    deterministic: bool = True
    lossiness: Lossiness = "lossy"
    cost: ProjectorCost = "cheap"
    quality_score: float = 1.0
    supported_constraints: tuple[str, ...] = ()
    test_fixtures: tuple[str, ...] = ()
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
    path: str = ""  # JSONPath or dotted key into artifact data (e.g. "$.text", "text")


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


class ProjectionFeatureFlags(BaseModel, frozen=True):
    """Feature flags for progressive rollout (per Section 20).

    All features default to enabled now, but flags allow gradual rollback
    if issues are discovered in production.
    """

    artifact_contracts_enabled: bool = True
    projection_resolver_enabled: bool = True
    tool_auto_binding_enabled: bool = True
    hide_debug_tools_for_task_agents: bool = True
    projector_schema_validation_enabled: bool = True
    resolver_dry_run: bool = False  # when True, log plans but don't bind
    plagiarism_pipeline_mode: bool = (
        False  # when True, PlagiarismAuditor runs as deterministic pipeline
    )


class ProjectionPolicy(BaseModel, frozen=True):
    """Resolver policy controlling graph search and candidate selection.

    All fields are now enforced by ProjectionResolver.
    """

    max_depth: int = 3
    min_quality: float = 0.7
    allow_lossy: bool = True
    allow_experimental: bool = False
    allow_deprecated: bool = False
    allow_debug_artifacts: bool = False
    prefer_cached: bool = True
    strict_ambiguity: bool = False
    features: ProjectionFeatureFlags = Field(default_factory=ProjectionFeatureFlags)


class ProjectionEvent(BaseModel, frozen=True):
    """Structured event emitted during artifact projection lifecycle."""

    event: str
    timestamp: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


# Upstream producers are now derived dynamically by derive_upstream_producers()
# (see below), which scans ToolRegistry for output_artifact_type declarations
# and uses the projector graph for indirect reachability.  This replaces the
# old _UPSTREAM_PRODUCERS static mapping that was impossible to keep in sync
# and contained semantically-incorrect entries (e.g. "core.plain_text" →
# "parse_layout" when parse_layout actually outputs parsed_layout).


class ProjectionTraceStep(BaseModel, frozen=True):
    """One step in a projection trace (per Section 9.3)."""

    projector: str
    cache: str = "miss"  # "hit" | "miss"
    output_ref: str = ""
    quality: dict[str, Any] = Field(default_factory=dict)


class ProjectionTrace(BaseModel, frozen=True):
    """Complete trace for one projection execution (per Section 9.3)."""

    field: str = ""
    source_artifact: str = ""
    steps: tuple[ProjectionTraceStep, ...] = ()
    materializer: dict[str, str] = Field(default_factory=dict)


class MaterializedBinding(BaseModel, frozen=True):
    """Concrete tool argument plus artifact provenance."""

    value: Any
    artifact_id: str
    materializer: str
    trace: ProjectionTrace | None = None


@dataclass(frozen=True)
class RuntimePolicy:
    """Runtime safety policy for a tool.

    Compared to the legacy ``ToolRuntimePolicy``, this removes:
    - allow_chaining (never set to False)
    - hidden_from_task_agents_by_default (handled by ProjectionPolicy)
    - require_debug_policy (never set to True)
    """

    max_calls: int | None = None
    max_consecutive: int | None = None


# Default materialize_as values per artifact type when InputField does not
# specify one explicitly.  Without this mapping the old ``f.materialize_as or
# f.artifact_type`` fallback would produce keys like
# ("core.plain_text", "core.plain_text") that MaterializerRegistry cannot
# resolve, causing a KeyError at materialization time.
_MATERIALIZE_AS_DEFAULTS: dict[str, str] = {
    "core.plain_text": "string",
    "core.text_collection": "list_string",
    "core.json_object": "dict",
    "core.error_report": "dict",
    "core.debug_view": "dict",
    "docaudit.parsed_layout": "dict",
    "docaudit.paragraph_list": "list_dict",
    "docaudit.search_results": "dict",
    "docaudit.document_structure": "dict",
    "docaudit.audit_finding_list": "list_dict",
    "docaudit.audit_report": "dict",
    "docaudit.reference_text_list": "list_dict",
    "docaudit.plagiarism_report": "dict",
}


def build_contract_from_input_fields(
    tool_name: str,
    input_fields: tuple[InputField, ...],
) -> tuple[InputField, ...]:
    """标准化 input_fields，补全 materialize_as 默认值。

    tool_name 参数保留以兼容调用方签名，当前仅用于未来扩展。
    """
    return tuple(
        InputField(
            name=f.name,
            artifact_type=f.artifact_type,
            materialize_as=f.materialize_as
            or _MATERIALIZE_AS_DEFAULTS.get(f.artifact_type, "dict"),
            required=f.required,
            constraints=f.constraints,
        )
        for f in input_fields
    )


def derive_upstream_producers(
    tools: list[Any],
    projector_registry: Any,  # ProjectorRegistry
) -> dict[str, list[str]]:
    """从工具列表和投影注册表动态推导上游生产者映射。

    直接生产者：工具的 output_artifact_type → [tool.name]
    间接生产者：通过投影图从已生产类型反向推导可达路径。
    """
    producers: dict[str, list[str]] = {}

    # 直接生产者
    for tool in tools:
        oat = getattr(tool, "output_artifact_type", None)
        if oat and isinstance(oat, str):
            producers.setdefault(oat, []).append(tool.name)

    # 间接生产者：对未直接生产的类型，查找投影路径
    all_produced = set(producers.keys())
    # 收集所有被工具 input_fields 需要的类型
    required_types: set[str] = set()
    for tool in tools:
        input_fields = getattr(tool, "input_fields", None)
        if input_fields:
            for f in input_fields:
                required_types.add(f.artifact_type)

    for target_type in required_types:
        if target_type in producers:
            continue
        for produced_type in all_produced:
            if _has_projection_path(produced_type, target_type, projector_registry):
                producers.setdefault(target_type, []).extend(producers[produced_type])
                break  # 找到一条路径即可

    return producers


def _has_projection_path(
    source_type: str,
    target_type: str,
    projector_registry: Any,
    max_depth: int = 3,
) -> bool:
    """BFS 检查是否存在从 source_type 到 target_type 的投影路径."""
    from collections import deque

    if source_type == target_type:
        return True
    visited: set[str] = {source_type}
    queue: deque[tuple[str, int]] = deque([(source_type, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for projector in projector_registry.outgoing(current):
            if projector.spec.target_type == target_type:
                return True
            if projector.spec.target_type not in visited:
                visited.add(projector.spec.target_type)
                queue.append((projector.spec.target_type, depth + 1))
    return False


def stable_content_hash(data: Any) -> str:
    """Return a deterministic SHA-256 hash for JSON-compatible data."""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ArtifactPermission(BaseModel, frozen=True):
    """Permissions for a single artifact in a subagent context."""

    project: bool = True  # allow this artifact to be projected
    materialize: bool = True  # allow materialization into tool args
    debug_read: bool = False  # allow debug_read access on this artifact


class ArtifactContextEntry(BaseModel, frozen=True):
    """One artifact entry in a scoped artifact context."""

    ref: str  # artifact_id
    permissions: ArtifactPermission = Field(default_factory=ArtifactPermission)


class ArtifactContext(BaseModel, frozen=True):
    """Scoped artifact context passed to a subagent (per Section 14).

    Limits which artifacts a subagent can access and what operations
    it can perform on them. Debug artifacts should not cross agent
    boundaries by default.
    """

    allowed_artifacts: tuple[ArtifactContextEntry, ...] = ()
    allow_all: bool = False  # when True, bypass all restrictions (for parent agent)


# -- ArtifactRef URL scheme (Section 4.4) ---------------------------------------

_ARTIFACT_REF_RE = __import__("re").compile(
    r"^artifact://([a-zA-Z_][a-zA-Z0-9_.]*)/([a-zA-Z_][a-zA-Z0-9_]*)/(\d+)$"
)


def parse_artifact_ref(ref: str) -> dict[str, str] | None:
    """Parse an ``artifact://type/tool/n`` URI, returning {type, tool, n} or None."""
    m = _ARTIFACT_REF_RE.match(ref)
    if m:
        return {"type": m.group(1), "tool": m.group(2), "n": m.group(3)}
    return None


def make_artifact_ref(artifact_type: str, tool: str, n: int) -> str:
    """Build an ``artifact://type/tool/n`` URI."""
    return f"artifact://{artifact_type}/{tool}/{n}"


def check_schema_version_compatible(
    artifact_schema_version: str,
    projector_source_constraint: str,
) -> bool:
    """Check whether *artifact_schema_version* satisfies *projector_source_constraint*.

    Supports simple comma-separated constraints like ``>=1.0,<2.0``.
    Each constraint is one of: ``>=X.Y``, ``<=X.Y``, ``>X.Y``, ``<X.Y``, ``==X.Y``.

    Returns True if all constraints are satisfied.
    """

    def _pad_version(
        a: tuple[int, ...], b: tuple[int, ...]
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Pad the shorter version tuple with zeros so they have the same length."""
        max_len = max(len(a), len(b))
        return (
            a + (0,) * (max_len - len(a)),
            b + (0,) * (max_len - len(b)),
        )

    if not projector_source_constraint or not artifact_schema_version:
        return True  # no constraint → compatible
    try:
        artifact_parts = tuple(int(x) for x in artifact_schema_version.split("."))
    except (ValueError, TypeError):
        return False
    for part in projector_source_constraint.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith(">="):
            ver = tuple(int(x) for x in part[2:].split("."))
            a, b = _pad_version(artifact_parts, ver)
            if not (a >= b):
                return False
        elif part.startswith("<="):
            ver = tuple(int(x) for x in part[2:].split("."))
            a, b = _pad_version(artifact_parts, ver)
            if not (a <= b):
                return False
        elif part.startswith(">"):
            ver = tuple(int(x) for x in part[1:].split("."))
            a, b = _pad_version(artifact_parts, ver)
            if not (a > b):
                return False
        elif part.startswith("<"):
            ver = tuple(int(x) for x in part[1:].split("."))
            a, b = _pad_version(artifact_parts, ver)
            if not (a < b):
                return False
        elif part.startswith("=="):
            ver = tuple(int(x) for x in part[2:].split("."))
            a, b = _pad_version(artifact_parts, ver)
            if not (a == b):
                return False
    return True
