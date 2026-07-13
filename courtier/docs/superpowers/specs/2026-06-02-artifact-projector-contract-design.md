# Artifact Projector Contract Framework Design

Date: 2026-06-02
Status: Proposed design approved in brainstorming
Scope: Framework-level design for typed artifacts, projectors, projection resolution, tool contracts, and loop safety in `docaudit-agent`

## 1. Problem Statement

The recent `.agent_logs` run for `subagent_plagiarism_auditor_656e4ea8` exposed a systemic failure mode:

- The plagiarism subagent searched reference documents successfully.
- It repeatedly tried to inspect cached outputs and guess JSON/JQ paths for parsed document text.
- It eventually discovered useful paths, but then fell into a `read_cached_output(query=".")` chain.
- It never called the terminal `detect_plagiarism` tool.
- The run consumed many turns and generated a growing cache chain without task progress.

This was not just a prompt failure. It revealed architectural gaps:

1. Tool outputs are cached as loosely typed refs such as `$ref:parse_document:1` or `$ref:read_cached_output:30`; the system does not know their semantic role.
2. Downstream tools need simple semantic inputs, such as `PlainText` or `TextCollection`, while upstream tools often produce deeply nested structures.
3. LLMs are asked to bridge that gap by reading cached output and writing ad hoc JQ queries.
4. `read_cached_output` results can be persisted again, allowing self-referential cache chains.
5. The agent loop lacks sufficient safeguards for repeated tool-call turns, duplicate tool calls, no-progress loops, and terminal-tool readiness.

The framework should move data adaptation out of LLM free-form exploration and into deterministic, typed, testable runtime components.

## 2. Design Goals

The design should:

1. Represent important tool outputs as typed artifacts with schema, metadata, lineage, and semantic role.
2. Let tools declare semantic input and output contracts, not just JSON parameter shapes.
3. Define projectors as deterministic `ArtifactType -> ArtifactType` transformations.
4. Resolve projection paths automatically through a typed projection graph.
5. Execute projection plans with step-level caching, schema validation, diagnostics, and trace events.
6. Materialize final artifacts into concrete tool parameters, such as `string` or `list[string]`.
7. Prevent projector growth from becoming `producer tools × consumer tools` adapter sprawl.
8. Prevent debug/cache tools, especially `read_cached_output`, from participating in business data flow.
9. Add loop safety so repeated tool calls and no-progress debug loops terminate predictably.
10. Support staged migration from the current ContextManager/ref system.

## 3. Non-goals

This design does not aim to:

1. Build a complete ontology for every future agent tool.
2. Let the projection resolver call upstream tools automatically.
3. Replace every existing tool schema at once.
4. Turn lightweight constraints into a general query language.
5. Make all agents deterministic pipelines.
6. Eliminate LLM decision-making; LLMs still choose strategies, queries, and final explanations where appropriate.
7. Remove the existing cache/context system immediately.

## 4. Core Concepts

### 4.1 ArtifactType

`ArtifactType` is a semantic type, not a tool-output name.

Recommended naming:

```text
<namespace>.<semantic_name>
```

Suggested namespaces:

- `core.*`: framework-level types.
- `docaudit.*`: document-audit domain types.
- `plugin.*`: plugin or vendor-specific types.
- `local.*`: project-local temporary types.

Good examples:

```text
core.plain_text
core.text_collection
docaudit.parsed_document
docaudit.paragraph_list
docaudit.search_results
docaudit.reference_text_list
```

Avoid tool-coupled names:

```text
parse_document_output
detect_plagiarism_input
search_results_for_plagiarism
```

### 4.2 ArtifactSchema

`ArtifactSchema` describes the data shape for an `ArtifactType`. It may be represented by JSON Schema, Pydantic, or a typed dict.

Schema should describe structure, not business strategy.

For example, `core.plain_text` may be shaped as:

```json
{
  "type": "object",
  "required": ["text"],
  "properties": {
    "text": { "type": "string" },
    "language": { "type": "string" },
    "source_scope": {
      "type": "string",
      "enum": ["full_document", "body", "header", "footer", "mixed", "unknown"]
    }
  }
}
```

### 4.3 Artifact

An `Artifact` is a typed data instance:

```python
Artifact(
    id="artifact_...",
    type="docaudit.parsed_document",
    schema_version="1.0",
    data={...},
    metadata={
        "created_by": "parse_document",
        "source_refs": [],
        "content_hash": "sha256:...",
        "semantic_role": "primary_document",
        "subject": "current_upload",
        "projection_allowed": True,
        "debug_only": False,
        "lineage": [],
    },
)
```

Important metadata:

- `created_by`
- `source_refs`
- `content_hash`
- `schema_version`
- `semantic_role`
- `subject`
- `scope`
- `sensitivity`
- `projection_allowed`
- `debug_only`
- `quality`
- `lineage`

### 4.4 ArtifactRef

`ArtifactRef` should identify both the stored data and its semantic type. It replaces loosely typed refs like `$ref:tool:n` as the primary business data reference.

Conceptual form:

```text
artifact://docaudit.parsed_document/parse_document/1
artifact://core.plain_text/projected/3
```

The exact string format can remain implementation-specific, but runtime must be able to resolve:

- artifact id
- artifact type
- schema version
- producer
- metadata
- storage location

### 4.5 ToolInputContract

A tool input contract declares semantic requirements for each field.

Example for `detect_plagiarism`:

```yaml
tool: detect_plagiarism
requires:
  new_doc:
    json_field: new_doc
    artifact_type: core.plain_text
    role: primary_document
    subject: current_upload
    materialize_as: string
    required: true
    constraints:
      source_scope: body_or_full_document
      min_chars: 20
      normalize_whitespace: true

  library_docs:
    json_field: library_docs
    artifact_type: core.text_collection
    role: reference_document
    subject: reference_library
    materialize_as: list_string
    required: true
    constraints:
      min_items: 1
      item_min_chars: 20
      max_items: 20
      dedupe: true
```

### 4.6 ToolOutputContract

A tool output contract declares which artifact type a tool result produces.

Examples:

```yaml
tool: parse_document
outputs:
  result:
    artifact_type: docaudit.parsed_document
    role: primary_document
    subject: current_upload
    schema_version: "1.0"
    persist: always
    llm_visible: summary
    projection_allowed: true
```

```yaml
tool: search_documents
outputs:
  result:
    artifact_type: docaudit.search_results
    role: reference_document
    subject: reference_library
    schema_version: "1.0"
    persist: auto
    llm_visible: summary
    projection_allowed: true
```

```yaml
tool: read_cached_output
outputs:
  result:
    artifact_type: core.debug_view
    role: debug
    subject: debug
    persist: auto
    llm_visible: preview
    projection_allowed: false
    debug_only: true
```

## 5. Initial Artifact Type Set

Keep the first type set small.

### 5.1 Core types

```text
core.plain_text
core.text_collection
core.json_object
core.error_report
core.debug_view
```

`core.json_object` is a fallback type, not a normal integration path. Tool contracts should not casually require it.

### 5.2 Docaudit types

```text
docaudit.parsed_document
docaudit.document_metadata
docaudit.paragraph_list
docaudit.document_structure
docaudit.search_results
docaudit.reference_text_list
docaudit.audit_finding_list
docaudit.audit_report
docaudit.plagiarism_report
```

These types are enough to solve the current plagiarism failure while leaving room for format, font, citation, and report workflows.

## 6. Projector Model

A projector is a deterministic, testable transformation from one artifact type to another:

```text
ArtifactType A -> ArtifactType B
```

It is not a tool adapter and should not know the downstream tool that will consume its output.

Example projectors:

```text
docaudit.parsed_document -> docaudit.paragraph_list
docaudit.paragraph_list -> core.plain_text
docaudit.search_results -> docaudit.reference_text_list
docaudit.reference_text_list -> core.text_collection
```

### 6.1 Projector declaration

Conceptual shape:

```python
Projector(
    name="docaudit.parsed_document.to_paragraph_list",
    source_type="docaudit.parsed_document",
    target_type="docaudit.paragraph_list",
    source_schema_version=">=1.0,<2.0",
    target_schema_version="1.0",
    version="1.0.0",
    owner="document",
    layer="domain",
    stability="stable",
    deterministic=True,
    lossiness="lossy",
    cost="cheap",
    quality_score=0.95,
    supported_constraints=["source_scope", "include_empty"],
    fn=parsed_document_to_paragraph_list,
)
```

### 6.2 Projector rules

Projectors should:

1. Be deterministic by default.
2. Avoid external I/O.
3. Avoid LLM calls.
4. Avoid mutating input artifacts.
5. Validate output schema.
6. Return diagnostics instead of silently dropping important data.
7. Declare lossiness, cost, stability, owner, version, and supported constraints.
8. Use semantic names, not downstream tool names.

Projectors should not:

1. Call `search_documents`, databases, LLMs, or external services.
2. Produce concrete tool-specific payloads like `DetectPlagiarismInput`.
3. Hide business logic that belongs in a tool or pipeline.
4. Accept arbitrary JQ/Python snippets as constraints.

### 6.3 Materializer distinction

Projector:

```text
ArtifactType -> ArtifactType
```

Materializer:

```text
Artifact -> concrete tool argument
```

Example:

```text
docaudit.parsed_document -> core.plain_text      # projector
core.plain_text -> string                        # materializer
```

This avoids projectors like `parsed_document_to_plagiarism_new_doc`.

## 7. Projector Registry

`ProjectorRegistry` manages projector definitions and the typed projection graph.

Responsibilities:

- Register projectors.
- Validate projector metadata.
- Index by source, target, edge, name, and layer.
- Detect duplicate or indistinguishable edges.
- Enforce naming and version rules.
- Expose graph introspection for debugging and audit.

It does not:

- Choose final paths.
- Execute projectors.
- Cache projection results.
- Materialize concrete tool arguments.

### 7.1 Registry indexes

Conceptual indexes:

```python
by_name: dict[str, Projector]
by_source: dict[ArtifactType, list[Projector]]
by_target: dict[ArtifactType, list[Projector]]
by_edge: dict[tuple[ArtifactType, ArtifactType], list[Projector]]
by_layer: dict[str, list[Projector]]
```

### 7.2 Edge multiplicity

Multiple projectors can share a source and target type only if metadata and constraints clearly distinguish them.

Example:

```text
docaudit.parsed_document.to_plain_text_full
docaudit.parsed_document.to_plain_text_body_only
docaudit.parsed_document.to_plain_text_preview_fallback
```

The preview fallback should have lower quality and should not be selected by strict production policies unless explicitly allowed.

## 8. Projection Resolver

`ProjectionResolver` is a planner. It reads a tool input contract, available artifacts, registry graph, and policy, then returns a `ProjectionPlan`.

It does not execute projectors or call tools.

### 8.1 Resolver input

```python
resolve(
    required_contract: ToolInputContract,
    available_artifacts: list[ArtifactRef],
    registry: ProjectorRegistry,
    policy: ProjectionPolicy,
) -> ProjectionResolution
```

### 8.2 ProjectionPlan

A plan describes how to satisfy one tool field.

Example for `detect_plagiarism.new_doc`:

```yaml
field: new_doc
required_type: core.plain_text
required_role: primary_document
source_artifact: artifact://docaudit.parsed_document/parse_document/1
steps:
  - projector: docaudit.parsed_document.to_paragraph_list@1.0.0
    source_type: docaudit.parsed_document
    target_type: docaudit.paragraph_list
    constraints:
      source_scope: body_or_full_document
  - projector: docaudit.paragraph_list.to_plain_text@1.0.0
    source_type: docaudit.paragraph_list
    target_type: core.plain_text
    constraints:
      normalize_whitespace: true
materializer:
  artifact_type: core.plain_text
  materialize_as: string
  path: $.text
```

### 8.3 Path search

Use bounded graph search, default `max_depth=3`.

Search should consider:

- type compatibility
- role and subject match
- schema version compatibility
- projector stability
- policy constraints
- path depth
- lossiness
- cost
- quality
- cache availability

Role and subject must outrank cache and path length. For example, a cached `core.plain_text(role=reference_document)` must not satisfy `new_doc(role=primary_document)`.

### 8.4 Failure explanation

Resolver failures must be actionable.

Example:

```json
{
  "field": "library_docs",
  "required_type": "core.text_collection",
  "required_role": "reference_document",
  "status": "unresolved",
  "available_artifacts": [
    "docaudit.parsed_document(role=primary_document)"
  ],
  "suggested_actions": [
    {
      "action": "call_tool",
      "tool": "search_documents",
      "reason": "search_documents outputs docaudit.search_results, which can project to core.text_collection"
    }
  ]
}
```

Resolver may suggest tools but must not call them.

## 9. Projection Executor

`ProjectionExecutor` executes a `ProjectionPlan`.

Responsibilities:

1. Load source artifact.
2. Check step-level cache.
3. Execute projector steps.
4. Validate output schema.
5. Persist projected artifacts.
6. Record diagnostics and trace.
7. Run final materializer.
8. Return concrete tool argument.

### 9.1 Step-level cache

Cache every projector step, not just final outputs.

Cache key should include:

- source artifact id
- source content hash
- source schema version
- projector name
- projector version
- target type
- target schema version
- normalized constraints
- relevant policy flags

This allows `ParsedDocument -> ParagraphList` to be reused by plagiarism, format, font, and citation tools.

### 9.2 Artifact persistence policy

Artifacts should declare persistence and visibility:

```yaml
persist: always | auto | never
llm_visible: full | preview | summary | hidden
projection_allowed: true | false
debug_only: true | false
```

`read_cached_output` outputs must be `debug_only: true` and `projection_allowed: false`.

### 9.3 Projection trace

Each execution should produce trace data:

```json
{
  "tool": "detect_plagiarism",
  "field": "new_doc",
  "source_artifact": "artifact://docaudit.parsed_document/parse_document/1",
  "steps": [
    {
      "projector": "docaudit.parsed_document.to_paragraph_list@1.0.0",
      "cache": "miss",
      "output_ref": "artifact://docaudit.paragraph_list/projected/2",
      "quality": { "confidence": 0.96, "lossiness": "lossy" }
    },
    {
      "projector": "docaudit.paragraph_list.to_plain_text@1.0.0",
      "cache": "miss",
      "output_ref": "artifact://core.plain_text/projected/3",
      "quality": { "confidence": 0.98, "lossiness": "lossy" }
    }
  ],
  "materializer": {
    "name": "core.plain_text.as_string",
    "path": "$.text",
    "output_type": "string"
  }
}
```

## 10. Materialization

Materializers convert final artifacts into concrete tool arguments.

Examples:

```text
core.plain_text -> string
core.text_collection -> list[string]
docaudit.document_metadata -> dict
```

Materialization must validate final values against tool schema and input constraints. If `min_chars=20` and text is 5 characters, the tool must not be called.

## 11. Tool Integration

Tool definitions should be extended with:

```python
ToolDefinition(
    name="...",
    description="...",
    parameters_schema={...},
    input_contract=ToolInputContract(...),
    output_contract=ToolOutputContract(...),
    runtime_policy=ToolRuntimePolicy(...),
)
```

### 11.1 ToolRegistry pre-execution flow

Before calling the actual tool:

1. Load tool definition.
2. Validate raw JSON schema.
3. Apply runtime policy pre-checks.
4. Inspect input contract.
5. For each required field:
   - accept explicit concrete value if valid;
   - resolve explicit `ArtifactRef` if provided;
   - otherwise try auto-binding from scoped artifact context.
6. If all required fields bind, call the tool.
7. Register outputs as artifacts.
8. Return business result plus auto-binding summary.

### 11.2 Argument source priority

Recommended priority:

1. Explicit concrete argument.
2. Explicit ArtifactRef argument.
3. Auto-bound artifact from current task context.
4. Tool schema default.
5. ContractBindingError.

High-risk fields may set `allow_explicit_override: false`.

## 12. LLM Interaction Model

The LLM should see capability-level information, not cache/JQ internals.

Old behavior encouraged:

```text
read_cached_output + jq + manual argument construction
```

New behavior should communicate:

```text
Tool results are typed artifacts.
Downstream tool arguments can be auto-bound through artifact contracts.
Do not call read_cached_output to prepare formal tool arguments.
read_cached_output is debug-only and cannot satisfy tool input contracts.
If a target tool is missing required artifacts, the runtime will suggest the proper upstream tool.
```

### 12.1 Tool readiness hints

The loop can inject concise readiness summaries:

```text
Ready tools:
- detect_plagiarism can be called now.
  Inputs will be auto-bound:
  new_doc <- docaudit.parsed_document
  library_docs <- docaudit.search_results

Do not call read_cached_output to prepare these arguments.
```

If blocked:

```text
Blocked tools:
- detect_plagiarism missing library_docs.
  Suggested next action: search_documents.
```

### 12.2 read_cached_output visibility

Default for normal task agents:

```text
read_cached_output hidden
```

Debug/developer agents may see it with strict policy:

```yaml
max_calls_per_agent_run: 5
max_consecutive_calls: 2
allow_chaining: false
require_debug_policy: true
```

## 13. Agent Loop Safety

Projection contracts reduce loops but do not replace loop safeguards.

Required safeguards:

1. `max_steps` applies to every model response, including tool-call responses.
2. Duplicate tool-call guard keyed by `(tool_name, normalized_arguments_hash)`.
3. `read_cached_output -> read_cached_output` chain guard.
4. No-progress guard based on business artifacts, contract resolution, terminal tool calls, and materially new outputs.
5. Terminal-tool readiness guard for task-specific terminal tools.
6. Subagent watchdog/timeout so a runaway subagent cannot block the parent forever.

For plagiarism:

```yaml
task_type: plagiarism_audit
terminal_tools:
  - detect_plagiarism
required_pre_tools:
  - search_documents
```

If `detect_plagiarism` is ready but the model continues debug/cache calls, the runtime should block or terminate with a no-progress error.

## 14. Parent/Subagent Artifact Passing

Subagents should receive a scoped `ArtifactContext`, not global cache access.

Example:

```yaml
allowed_artifacts:
  - ref: artifact://docaudit.parsed_document/parse_document/1
    permissions:
      project: true
      materialize: false
      debug_read: false
```

Debug artifacts should not cross agent boundaries by default.

A subagent can also declare task-level contracts:

```yaml
subagent: plagiarism_auditor
requires:
  - artifact_type: docaudit.parsed_document
    role: primary_document
    subject: current_upload
produces:
  - artifact_type: docaudit.plagiarism_report
terminal_tools:
  - detect_plagiarism
allowed_tools:
  - search_documents
  - detect_plagiarism
hidden_tools:
  - read_cached_output
```

## 15. Plagiarism Auditor Target Flow

Current failed behavior:

```text
search_documents -> read_cached_output -> jq guessing -> read_cached_output chain -> no detect_plagiarism
```

Target behavior:

```text
parse_document outputs docaudit.parsed_document(role=primary_document)
search_documents outputs docaudit.search_results(role=reference_document)
ToolRegistry resolves detect_plagiarism inputs:
  new_doc <- ParsedDocument -> ParagraphList -> PlainText -> string
  library_docs <- SearchResults -> ReferenceTextList -> TextCollection -> list[string]
detect_plagiarism executes
output registered as docaudit.plagiarism_report
```

Recommended initial projectors:

```text
docaudit.parsed_document -> docaudit.paragraph_list
docaudit.paragraph_list -> core.plain_text
docaudit.search_results -> docaudit.reference_text_list
docaudit.reference_text_list -> core.text_collection
```

Recommended materializers:

```text
core.plain_text -> string
core.text_collection -> list_string
```

For this task, `PlagiarismAuditorAgent` should move toward deterministic pipeline mode:

1. Use parsed document metadata/text to derive search query.
2. Call `search_documents`.
3. Call `detect_plagiarism` with auto-bound inputs.
4. Let LLM explain the final report.

## 16. Governance

### 16.1 Artifact type governance

Before adding an artifact type, answer:

1. Is this a stable semantic concept?
2. Does an existing type already cover it?
3. Will multiple tools/projectors reuse it?
4. Is the schema clear?
5. Are role, subject, sensitivity, and visibility clear?
6. Does it need versioning?

Reject tool-specific types like:

```text
docaudit.plagiarism_new_doc
docaudit.format_checker_payload
docaudit.qwen_doc_summary
```

### 16.2 Projector governance

Every stable projector must declare:

```yaml
name:
source_type:
target_type:
version:
owner:
layer:
stability:
lossiness:
deterministic:
cost:
quality_score:
supported_constraints:
test_fixtures:
description:
```

Reject projector names containing downstream tool names, such as:

```text
parsed_document_to_detect_plagiarism_input
search_results_to_plagiarism_docs
```

### 16.3 Layer promotion

Projector layers:

```text
local -> plugin/domain -> core
```

Promotion requires tests, schema clarity, semantic reuse, and owner responsibility.

### 16.4 Graph audit

Add an audit command/tool that reports:

- artifact types with no producers
- artifact types with no consumers
- unused projectors
- duplicate source-target edges with indistinguishable metadata
- deprecated projectors still used
- local projectors in production paths
- paths exceeding max depth
- tool contracts requiring `core.json_object`
- tools missing contracts
- debug artifacts used in business paths

## 17. Testing Strategy

### 17.1 Projector unit tests

Examples:

- `ParsedDocument -> ParagraphList`
  - extracts `body.main_text[].elements[].font.text`
  - preserves order
  - records `source_path`
  - handles missing body/header/footer
  - validates output schema
- `ParagraphList -> PlainText`
  - joins paragraphs in order
  - respects `source_scope`
  - normalizes whitespace
  - validates min/max text constraints
- `SearchResults -> ReferenceTextList`
  - extracts `hits[].chunk_text`
  - preserves title/resource/source/chunk metadata
  - filters empty chunks
  - dedupes when requested
  - validates min text length

### 17.2 Resolver tests

Required tests:

- direct artifact match creates zero-step plan
- one-step and two-step paths resolve
- max depth blocks long paths
- role mismatch rejects candidate artifact
- subject mismatch rejects candidate artifact
- stable projector beats experimental projector
- debug/read_cached_output artifact is ignored
- unsupported constraints fail clearly
- ambiguity warnings/errors behave by policy

### 17.3 Tool binding integration tests

For `detect_plagiarism`:

- parsed document + search results auto-bind required fields
- explicit ArtifactRef arguments project/materialize correctly
- missing search results fail without calling the tool
- failure suggests `search_documents`
- debug artifacts cannot satisfy fields

### 17.4 Agent loop regression tests

Tests matching the current logs:

1. Tool-call turns count against `max_steps`.
2. Repeated `read_cached_output(ref, query=".")` is blocked.
3. `read_cached_output` cannot read a `read_cached_output` ref by default.
4. Repeated identical tool calls terminate.
5. Several turns creating only debug artifacts trigger no-progress termination.
6. Plagiarism run reaches `detect_plagiarism` without using `read_cached_output` for argument construction.

## 18. Structured Events and Observability

Add structured events:

```text
artifact_created
projection_resolution
projection_step_started
projection_step_cache_hit
projection_step_completed
projection_step_failed
projection_materialized
tool_arguments_bound
tool_binding_failed
tool_ready
tool_blocked_missing_artifact
debug_tool_chain_blocked
repeated_tool_call_blocked
loop_no_progress_detected
terminal_tool_ready
terminal_tool_called
```

Future `.agent_logs` analysis should show whether a tool was blocked by missing artifacts, failed projection, repeated debug calls, or ignored terminal readiness.

## 19. Migration Plan

### Phase 0: Safety hotfixes

Goal: immediately prevent runaway loops.

Implement:

1. all-turn `max_steps`
2. duplicate tool-call guard
3. `read_cached_output` chain guard
4. low-signal search query rejection
5. subagent watchdog/timeout
6. no-progress guard for debug-only outputs

### Phase 1: Artifact metadata + contracts

Goal: start typing tool outputs and declaring semantic tool requirements.

Implement:

1. `ArtifactType`, `Artifact`, and `ArtifactRef` model
2. artifact metadata wrapper around existing ContextManager refs
3. output contracts for `parse_document`, `search_documents`, `detect_plagiarism`, `read_cached_output`
4. input contract for `detect_plagiarism`
5. `read_cached_output` as `core.debug_view`, `projection_allowed=false`

### Phase 2: Projector registry + initial projectors

Goal: support the minimal reusable projection graph.

Implement:

```text
docaudit.parsed_document -> docaudit.paragraph_list
docaudit.paragraph_list -> core.plain_text
docaudit.search_results -> docaudit.reference_text_list
docaudit.reference_text_list -> core.text_collection
```

Implement materializers:

```text
core.plain_text -> string
core.text_collection -> list_string
```

### Phase 3: Resolver + executor + materializer

Goal: automatically turn artifacts into tool arguments.

Implement:

1. bounded resolver
2. `ProjectionPlan`
3. step-level projection cache
4. executor
5. materializer registry
6. projection trace/events

### Phase 4: ToolRegistry integration + plagiarism pipeline

Goal: fix the plagiarism auditor path end-to-end.

Implement:

1. pre-execution contract auto-binding
2. ArtifactRef arguments for contract fields
3. updated plagiarism tool descriptions/prompts
4. hide `read_cached_output` from normal plagiarism subagent
5. deterministic plagiarism pipeline mode where appropriate

### Phase 5: Governance + audit tooling

Goal: prevent long-term projector graph corruption.

Implement:

1. projector graph audit command/tool
2. registry introspection
3. contract completeness checks
4. deprecated/local projector warnings
5. docs for artifact types/projectors/contracts
6. CI checks for projector metadata and naming anti-patterns

## 20. Rollout Strategy

Use feature flags:

```yaml
artifact_contracts_enabled: true
projection_resolver_enabled: true
tool_auto_binding_enabled: false
hide_read_cached_output_for_task_agents: true
plagiarism_pipeline_mode: false
```

Recommended rollout:

1. Enable metadata only.
2. Enable resolver in dry-run mode.
3. Compare resolver plans with current behavior.
4. Enable auto-binding for `detect_plagiarism` only.
5. Expand to other tools once tests pass.

Dry-run mode should log:

```text
Would auto-bind detect_plagiarism.new_doc via ParsedDocument -> ParagraphList -> PlainText.
```

without changing behavior.

## 21. Backward Compatibility

Existing tools should continue working.

Rules:

1. Tool without input contract uses current schema behavior.
2. Tool with output contract registers artifacts.
3. Tool with input contract can use auto-binding.
4. Projection failure is enforced only for tools with `require_contract_binding=true`.
5. `detect_plagiarism` should become the first enforced contract-binding tool after Phase 3 is stable.

## 22. Success Criteria

The design succeeds when:

1. The current plagiarism audit scenario completes without `read_cached_output` loops.
2. `detect_plagiarism` receives correct `new_doc` and `library_docs` automatically.
3. The initial solution requires only a small reusable projector set.
4. Adding another text-consuming tool does not require a new `ParsedDocument` projector.
5. `read_cached_output` output cannot satisfy business input contracts.
6. `max_steps` stops tool-call loops.
7. Structured logs explain projection decisions and failures.
8. Resolver failures suggest concrete upstream actions.
9. Projector graph audit catches tool-specific adapter sprawl.

## 23. Open Implementation Decisions

These should be settled during implementation planning:

1. Exact Python class/module layout.
2. Exact artifact ref string format.
3. Whether schemas use Pydantic, JSON Schema, or both.
4. How much of existing `ContextManager` becomes `ArtifactStore` versus wrapping it.
5. Exact resolver scoring weights.
6. Feature flag storage location.
7. Whether plagiarism pipeline mode is implemented immediately or after auto-binding stabilizes.

## 24. Final Recommendation

Adopt a typed projection graph with a small canonical artifact set and light constraint-aware tool contracts.

The key architectural rule is:

```text
Tools produce and consume semantic artifacts.
Projectors convert between artifact types.
Materializers bind artifacts to concrete tool fields.
LLMs do not guess JSON paths to construct downstream tool arguments.
Debug/cache tools do not participate in business data flow.
```

This shifts complexity from uncontrolled LLM exploration into deterministic, testable, observable framework components, while keeping projector growth bounded by stable semantic artifact types rather than by tool pair combinations.
