import json

import pytest

from courtier.agent.api.services.stream_service import rehydrate_artifact_store
from courtier.agent.artifacts.store import ArtifactStore


@pytest.fixture
def artifact_store(tmp_path):
    return ArtifactStore(cache_dir=str(tmp_path))


def test_rehydrate_registers_artifact_and_sets_ref(artifact_store, tmp_path):
    """rehydrate_artifact_store should register artifacts and set refs in ArtifactStore."""
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

    from courtier.agent.core.state import Message
    messages = (Message(role="tool", name="parse_document", content=json.dumps(msg_content)),)

    assert "$ref:parse_document:1" not in artifact_store.ref_map

    rehydrate_artifact_store(
        artifact_store=artifact_store,
        messages=messages,
        cache_dir=str(tmp_path),
    )

    # ArtifactStore now owns ref_map — ref should be set directly on it.
    assert "$ref:parse_document:1" in artifact_store.ref_map
    assert artifact_store.ref_map["$ref:parse_document:1"] == str(filepath)


def test_rehydrate_skips_unknown_tool(tmp_path):
    """Messages without __persisted_output__ should be skipped gracefully."""
    artifact_store = ArtifactStore(cache_dir=str(tmp_path))

    msg_content = {"data": {"not_persisted": True}}
    from courtier.agent.core.state import Message
    messages = (Message(role="tool", name="some_tool", content=json.dumps(msg_content)),)

    # Should not raise
    rehydrate_artifact_store(
        artifact_store=artifact_store,
        messages=messages,
        cache_dir=str(tmp_path),
    )
    assert len(artifact_store.ref_map) == 0
