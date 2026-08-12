from __future__ import annotations

import pytest

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata, ProjectorSpec
from courtier.agent.artifacts.projectors import (
    Projector,
    ProjectorRegistry,
    audit_projector_graph,
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
                                "elements": [{"font": {"text": "关于开展安全生产检查的通知"}}]
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


def test_document_markdown_projects_to_plain_text():
    """convert_document output (core.document_markdown) → core.plain_text."""
    registry = create_default_projector_registry()
    projector = registry.get("core.document_markdown.to_plain_text")

    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.document_markdown",
        data={"markdown": "# 标题\n\n正文内容。", "format": "docx"},
        metadata=ArtifactMetadata(created_by="convert_document", semantic_role="document"),
    )
    result = projector.project(artifact, constraints={})

    assert result.artifact.artifact_type == "core.plain_text"
    assert result.artifact.data["text"] == "# 标题\n\n正文内容。"
    assert result.artifact.data["source_scope"] == "full_document"
    assert result.artifact.data["language"] == "zh"

    # Empty markdown → zero confidence, still a valid projection.
    empty = Artifact(
        artifact_id="a2",
        artifact_type="core.document_markdown",
        data={"markdown": ""},
        metadata=ArtifactMetadata(created_by="convert_document", semantic_role="document"),
    )
    empty_result = projector.project(empty, constraints={})
    assert empty_result.quality.confidence == 0.0


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


# -- Gap 10: calibrated projector quality scores -------------------------------


def test_default_projectors_have_distinct_quality_scores():
    registry = create_default_projector_registry()
    p1 = registry.get("docaudit.parsed_document.to_paragraph_list")
    p2 = registry.get("docaudit.paragraph_list.to_plain_text")
    p3 = registry.get("docaudit.search_results.to_reference_text_list")
    p4 = registry.get("docaudit.reference_text_list.to_text_collection")

    assert p1.spec.quality_score == 0.95
    assert p2.spec.quality_score == 0.98
    assert p3.spec.quality_score == 0.92
    assert p4.spec.quality_score == 0.97
    # All should be "lossy" not "lossless" since text extraction is inherently lossy
    assert p1.spec.lossiness == "lossy"
    assert p2.spec.lossiness == "lossy"
    assert p3.spec.lossiness == "lossy"
    assert p4.spec.lossiness == "lossy"


# -- Gap 11: layer promotion ---------------------------------------------------


def test_promote_projector_to_higher_layer():
    registry = create_default_projector_registry()
    name = "docaudit.parsed_document.to_paragraph_list"

    assert registry.get(name).spec.layer == "domain"
    registry.promote(name, "core")
    assert registry.get(name).spec.layer == "core"


def test_promote_rejects_lower_layer():
    registry = create_default_projector_registry()
    name = "docaudit.parsed_document.to_paragraph_list"

    with pytest.raises(ValueError, match="must move to a higher layer"):
        registry.promote(name, "local")


def test_promote_rejects_unknown_projector():
    registry = create_default_projector_registry()
    with pytest.raises(ValueError, match="Unknown projector"):
        registry.promote("nonexistent", "core")


def test_promote_rejects_experimental_projector():
    registry = ProjectorRegistry()
    # Create a dummy experimental projector
    spec = ProjectorSpec(
        name="test.experimental",
        source_type="core.plain_text",
        target_type="core.text_collection",
        owner="test",
        stability="experimental",
    )
    registry.register(Projector(spec, lambda a, c: ({}, None, ())))

    with pytest.raises(ValueError, match="only stable projectors"):
        registry.promote("test.experimental", "core")


# -- Gap 9: graph audit --------------------------------------------------------


def test_audit_projector_graph_report_structure():
    registry = create_default_projector_registry()
    report = audit_projector_graph(registry)

    assert report["total_projectors"] == 5
    assert isinstance(report["unproduced_types"], list)
    assert isinstance(report["unconsumed_types"], list)
    assert isinstance(report["duplicate_edges"], dict)
    assert report["unused_projectors"] == []  # none are deprecated


def test_audit_catches_duplicate_edges():
    registry = create_default_projector_registry()
    # Register a second projector with the same source->target edge
    original = registry.get("docaudit.paragraph_list.to_plain_text")
    spec = original.spec.model_copy(
        update={"name": "docaudit.paragraph_list.to_plain_text_v2", "version": "2.0.0"}
    )
    registry.register(Projector(spec, original._fn))

    report = audit_projector_graph(registry)
    edge_key = "docaudit.paragraph_list -> core.plain_text"
    assert edge_key in report["duplicate_edges"]
    assert len(report["duplicate_edges"][edge_key]) == 2


def test_audit_catches_deprecated_projector():
    registry = create_default_projector_registry()
    original = registry.get("docaudit.paragraph_list.to_plain_text")
    deprecated_spec = original.spec.model_copy(
        update={
            "name": "docaudit.paragraph_list.to_plain_text_old",
            "stability": "deprecated",
        },
    )
    registry.register(Projector(deprecated_spec, original._fn))

    report = audit_projector_graph(registry)
    assert "docaudit.paragraph_list.to_plain_text_old" in report["unused_projectors"]


# -- Gap 1: schema validation in projector output ------------------------------


def test_projector_validates_output_schema():
    """Ensure projector output that doesn't match target schema gets diagnostics."""
    registry = create_default_projector_registry()
    # Use a valid artifact so the projector runs, but the output must still be valid
    projector = registry.get("docaudit.parsed_document.to_paragraph_list")

    result = projector.project(_parsed_document_artifact(), constraints={})
    # Output should have "paragraphs" key (required by docaudit.paragraph_list schema)
    assert "paragraphs" in result.artifact.data
    # No schema errors should be emitted for valid output
    schema_errors = [d for d in result.diagnostics if d.code == "schema_validation_failed"]
    assert schema_errors == []


# -- Gap 1: registry indexes (by_target, by_edge, by_layer) --------------------


def test_registry_by_target_index():
    registry = create_default_projector_registry()

    # Projectors producing core.plain_text
    producers = registry.by_target("core.plain_text")
    assert len(producers) == 2
    assert {p.spec.name for p in producers} == {
        "docaudit.paragraph_list.to_plain_text",
        "core.document_markdown.to_plain_text",
    }


def test_registry_by_target_unknown_type_returns_empty():
    registry = create_default_projector_registry()
    assert registry.by_target("nonexistent.type") == []


def test_registry_by_edge_index():
    registry = create_default_projector_registry()

    edge = registry.by_edge("docaudit.parsed_document", "docaudit.paragraph_list")
    assert len(edge) == 1
    assert edge[0].spec.name == "docaudit.parsed_document.to_paragraph_list"


def test_registry_by_edge_unknown_returns_empty():
    registry = create_default_projector_registry()
    assert registry.by_edge("core.plain_text", "core.debug_view") == []


def test_registry_by_layer_index():
    registry = create_default_projector_registry()

    domain = registry.by_layer("domain")
    assert len(domain) == 5  # all default projectors are domain-layer


def test_register_and_promote_update_all_indexes():
    """Register and promote should keep all four indexes consistent."""
    registry = ProjectorRegistry()
    spec = ProjectorSpec(
        name="test.local_proj",
        source_type="core.plain_text",
        target_type="core.json_object",
        owner="test",
        layer="local",
    )
    registry.register(Projector(spec, lambda a, c: ({}, None, ())))

    assert len(registry.by_target("core.json_object")) == 1
    assert len(registry.by_edge("core.plain_text", "core.json_object")) == 1
    assert len(registry.by_layer("local")) == 1

    # Promote to domain
    registry.promote("test.local_proj", "domain")
    assert len(registry.by_layer("local")) == 0
    assert len(registry.by_layer("domain")) == 1
    # Edge and target indexes should remain consistent after promotion
    assert len(registry.by_target("core.json_object")) == 1
    assert len(registry.by_edge("core.plain_text", "core.json_object")) == 1
