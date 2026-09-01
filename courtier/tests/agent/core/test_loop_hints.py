from __future__ import annotations

from unittest.mock import MagicMock

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata, InputField
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.core.loop_hints import _build_terminal_ready_hints


def _make_tool(name: str, fields: tuple[InputField, ...]):
    tool = MagicMock()
    tool.name = name
    tool.input_fields = fields
    tool.output_artifact_type = None
    return tool


def _make_registry(*tools):
    registry = MagicMock()
    registry.list_tools.return_value = list(tools)
    return registry


def test_terminal_ready_hints_hide_source_ids_for_projection():
    """Projected artifact IDs are internal and must not appear in LLM hints,
    otherwise the model may copy them verbatim into tool arguments.

    This test uses a multi-step projection (parsed_layout -> plain_text)
    because that is where projected: IDs are generated.
    """
    store = ArtifactStore()
    store.put(
        Artifact(
            artifact_id="$ref:parse_layout:1",
            artifact_type="docaudit.parsed_layout",
            data={"pages": []},
            metadata=ArtifactMetadata(
                created_by="parse_layout",
                semantic_role="primary_document",
                subject="current_upload",
            ),
        )
    )

    tool = _make_tool(
        "detect_plagiarism",
        (
            InputField(
                name="new_doc",
                artifact_type="core.plain_text",
                materialize_as="string",
            ),
        ),
    )
    registry = _make_registry(tool)

    hints = _build_terminal_ready_hints(registry, store)

    assert hints is not None
    assert hints == (
        "- detect_plagiarism 可以立即调用。输入将自动绑定：\n"
        "  - new_doc（通过 2 步投影）"
    )
    assert "projected:" not in hints
    assert "$ref:parse_layout:1" not in hints


def test_terminal_ready_hints_hide_source_ids_for_direct_match():
    """Source artifact IDs must not leak even for direct type matches."""
    store = ArtifactStore()
    store.put(
        Artifact(
            artifact_id="$ref:extract_text:1",
            artifact_type="core.plain_text",
            data={"text": "hello"},
            metadata=ArtifactMetadata(
                created_by="extract_text",
                semantic_role="primary_document",
                subject="current_upload",
            ),
        )
    )

    tool = _make_tool(
        "detect_plagiarism",
        (
            InputField(
                name="new_doc",
                artifact_type="core.plain_text",
                materialize_as="string",
            ),
        ),
    )
    registry = _make_registry(tool)

    hints = _build_terminal_ready_hints(registry, store)

    assert hints is not None
    assert hints == (
        "- detect_plagiarism 可以立即调用。输入将自动绑定：\n"
        "  - new_doc（直接匹配）"
    )
    assert "$ref:extract_text:1" not in hints
