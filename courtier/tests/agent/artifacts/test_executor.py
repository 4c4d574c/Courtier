from __future__ import annotations

from courtier.agent.artifacts.executor import MaterializerRegistry, ProjectionExecutor
from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactMetadata,
    MaterializerSpec,
    ProjectionPlan,
    ProjectionStep,
)
from courtier.agent.artifacts.projectors import create_default_projector_registry
from courtier.agent.artifacts.store import ArtifactStore


def test_materializes_plain_text_as_string():
    registry = MaterializerRegistry.default()
    artifact = Artifact(
        artifact_id="plain",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    value = registry.materialize(
        artifact,
        MaterializerSpec(artifact_type="core.plain_text", materialize_as="string"),
    )

    assert value == "hello"


def test_executor_runs_two_step_plan_and_materializes_text():
    store = ArtifactStore()
    source = Artifact(
        artifact_id="$ref:parse_layout:1",
        artifact_type="docaudit.parsed_layout",
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
            created_by="parse_layout",
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
                projector_name="docaudit.parsed_layout.to_paragraph_list",
                source_type="docaudit.parsed_layout",
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
    # Verify trace is populated
    assert result.trace is not None
    assert result.trace.field == "new_doc"
    assert result.trace.source_artifact == source.artifact_id
    assert len(result.trace.steps) == 2
    assert result.trace.steps[0].projector.startswith("docaudit.parsed_layout.to_paragraph_list@")
    assert result.trace.steps[0].cache == "miss"
    assert result.trace.steps[1].projector.startswith("docaudit.paragraph_list.to_plain_text@")
    assert result.trace.materializer["output_type"] == "string"


def test_document_markdown_materializes_as_plain_text_string():
    """End-to-end: convert_document Markdown artifact → plagiarism new_doc string.

    Mirrors detect_plagiarism's declared input field
    (InputField(name="new_doc", artifact_type="core.plain_text",
    materialize_as="string")): the binder resolves the $ref through the
    single-step document_markdown → plain_text projection.
    """
    store = ArtifactStore()
    source = Artifact(
        artifact_id="$ref:convert_document:1",
        artifact_type="core.document_markdown",
        data={"markdown": "## 关于拨付经费的请示\n\n正文内容。", "format": "docx"},
        metadata=ArtifactMetadata(
            created_by="convert_document",
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
                projector_name="core.document_markdown.to_plain_text",
                source_type="core.document_markdown",
                target_type="core.plain_text",
                constraints={},
            ),
        ),
        materializer=MaterializerSpec(artifact_type="core.plain_text", materialize_as="string"),
    )

    result = ProjectionExecutor(
        projector_registry=create_default_projector_registry(),
        materializer_registry=MaterializerRegistry.default(),
        artifact_store=store,
    ).execute(plan)

    assert result.value == "## 关于拨付经费的请示\n\n正文内容。"
    assert store.get(result.artifact_id).artifact_type == "core.plain_text"
    assert len(result.trace.steps) == 1
    assert result.trace.steps[0].projector.startswith("core.document_markdown.to_plain_text@")


def test_materializer_uses_path_to_extract_field():
    """MaterializerSpec.path extracts a top-level key from artifact data."""
    registry = MaterializerRegistry.default()
    artifact = Artifact(
        artifact_id="plain",
        artifact_type="core.plain_text",
        data={"text": "extracted via path"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    value = registry.materialize(
        artifact,
        MaterializerSpec(artifact_type="core.plain_text", materialize_as="string", path="$.text"),
    )

    assert value == "extracted via path"


def test_materializer_falls_back_when_path_key_missing():
    """When the path key doesn't exist in data, fall through to registered materializer."""
    registry = MaterializerRegistry.default()
    artifact = Artifact(
        artifact_id="plain",
        artifact_type="core.plain_text",
        data={"text": "fallback"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    # Path key "nonexistent" is not in data, so registered materializer is used
    value = registry.materialize(
        artifact,
        MaterializerSpec(
            artifact_type="core.plain_text",
            materialize_as="string",
            path="$.nonexistent",
        ),
    )

    # Falls through to registered materializer: artifact.data.get("text", "")
    assert value == "fallback"


def _paragraph_list_artifact() -> Artifact:
    return Artifact(
        artifact_id="paras",
        artifact_type="docaudit.paragraph_list",
        data={
            "paragraphs": [
                {"index": 0, "text": "第一段", "section": "title"},
                {"index": 1, "text": "第二段", "section": "body"},
            ]
        },
        metadata=ArtifactMetadata(created_by="test"),
    )


def test_materializes_paragraph_list_as_list_string():
    """paragraph_list materializes to a list of paragraph text strings.

    Regression for the ``audit_content`` / ``correct_text`` tools, whose
    ``docaudit.paragraph_list`` input field must become a ``list[str]``.
    """
    registry = MaterializerRegistry.default()

    value = registry.materialize(
        _paragraph_list_artifact(),
        MaterializerSpec(artifact_type="docaudit.paragraph_list", materialize_as="list_string"),
    )

    assert value == ["第一段", "第二段"]


def test_materializes_paragraph_list_as_dict():
    """paragraph_list materializes to its raw data dict (get_artifact default)."""
    registry = MaterializerRegistry.default()
    artifact = _paragraph_list_artifact()

    value = registry.materialize(
        artifact,
        MaterializerSpec(artifact_type="docaudit.paragraph_list", materialize_as="dict"),
    )

    assert value == artifact.data


def test_binds_paragraph_list_field_as_list_string():
    """The full effective-field → resolve → execute chain yields list[str].

    Mirrors the runtime binding path (build_contract_from_input_fields +
    resolver + executor) that raised ``KeyError`` before the fix.
    """
    from courtier.agent.artifacts.models import (
        InputField,
        ProjectionPolicy,
        build_contract_from_input_fields,
    )
    from courtier.agent.artifacts.resolver import ProjectionResolver

    store = ArtifactStore()
    store.put(_paragraph_list_artifact())

    fields = build_contract_from_input_fields(
        "audit_content",
        (
            InputField(
                name="paragraphs",
                artifact_type="docaudit.paragraph_list",
                materialize_as="list_string",
            ),
        ),
    )
    resolver = ProjectionResolver(create_default_projector_registry())
    resolution = resolver.resolve(
        fields, "audit_content", store.list_projection_candidates(), ProjectionPolicy()
    )
    assert resolution.status == "resolved"

    binding = ProjectionExecutor(
        projector_registry=create_default_projector_registry(),
        materializer_registry=MaterializerRegistry.default(),
        artifact_store=store,
    ).execute(resolution.plans["paragraphs"])

    assert binding.value == ["第一段", "第二段"]
