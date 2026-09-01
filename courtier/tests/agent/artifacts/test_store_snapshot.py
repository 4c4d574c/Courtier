"""Snapshot / restore round-trip tests for ArtifactStore.

Covers full-fidelity multi-turn restore: typed artifacts (disk-backed refs
re-loaded from cache files, in-memory artifacts inlined), type policies,
and ref_map — plus graceful degradation on missing files / bad versions.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from courtier.agent.artifacts.models import Artifact, ArtifactMetadata
from courtier.agent.artifacts.store import ArtifactStore


def _make_disk_ref_artifact(
    store: ArtifactStore,
    cache_dir,
    ref_id: str,
    data: dict,
    **metadata_kwargs,
) -> Artifact:
    """Register an artifact whose data lives in a cache file on disk."""
    path = cache_dir / f"{ref_id.replace(':', '_')}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    store.set_ref(ref_id, str(path))
    return store.register_cached_ref(
        ref_id=ref_id,
        artifact_type="docaudit.parsed_layout",
        created_by="parse_layout",
        data=data,
        **metadata_kwargs,
    )


def test_snapshot_restore_roundtrip(tmp_path):
    store = ArtifactStore(cache_dir=str(tmp_path))
    store.set_type_policy("docaudit.parsed_layout", persist="always", llm_visible="summary")
    disk_data = {"pages": [{"index": 0, "text": "正文"}]}
    _make_disk_ref_artifact(
        store,
        tmp_path,
        "ref-parse-1",
        disk_data,
        role="primary_document",
        subject="current_upload",
    )
    inline = Artifact(
        artifact_id="inline-1",
        artifact_type="core.plain_text",
        data={"text": "内存工件"},
        metadata=ArtifactMetadata(
            created_by="test",
            lineage=("ref-parse-1",),
            source_refs=("ref-parse-1",),
        ),
    )
    store.put(inline)

    # Simulate the real persistence path: snapshot must survive JSON.
    snapshot = json.loads(json.dumps(store.snapshot(), ensure_ascii=False))

    restored = ArtifactStore.restore(snapshot, cache_dir=str(tmp_path))

    # Disk-backed artifact: metadata intact, data re-loaded from the file.
    disk_artifact = restored.get("ref-parse-1")
    assert disk_artifact is not None
    assert disk_artifact.artifact_type == "docaudit.parsed_layout"
    assert disk_artifact.data == disk_data
    assert disk_artifact.metadata.semantic_role == "primary_document"
    assert disk_artifact.metadata.subject == "current_upload"

    # Inlined artifact: data and lineage (the artifact graph) intact.
    inline_artifact = restored.get("inline-1")
    assert inline_artifact is not None
    assert inline_artifact.data == {"text": "内存工件"}
    assert inline_artifact.metadata.lineage == ("ref-parse-1",)
    assert inline_artifact.metadata.source_refs == ("ref-parse-1",)

    # Type policies and ref_map are restored too.
    assert restored._persist_policies["docaudit.parsed_layout"] == "always"
    assert restored._visible_policies["docaudit.parsed_layout"] == "summary"
    assert restored.ref_map == store.ref_map


def test_restore_skips_artifact_when_cache_file_missing(tmp_path):
    store = ArtifactStore(cache_dir=str(tmp_path))
    _make_disk_ref_artifact(store, tmp_path, "ref-gone", {"a": 1})
    store.put(
        Artifact(
            artifact_id="inline-1",
            artifact_type="core.plain_text",
            data={"text": "still here"},
            metadata=ArtifactMetadata(created_by="test"),
        )
    )
    snapshot = store.snapshot()

    # Cache file deleted between persist and restore.
    (tmp_path / "ref-gone.json").unlink()

    restored = ArtifactStore.restore(snapshot, cache_dir=str(tmp_path))
    assert restored.get("ref-gone") is None
    assert restored.get("inline-1") is not None


def test_snapshot_excludes_unserializable_inline_data(tmp_path):
    store = ArtifactStore(cache_dir=str(tmp_path))
    store.put(
        Artifact(
            artifact_id="bad-data",
            artifact_type="core.plain_text",
            data={"payload": object()},  # not JSON-serializable
            metadata=ArtifactMetadata(created_by="test"),
        )
    )
    store.put(
        Artifact(
            artifact_id="good-data",
            artifact_type="core.plain_text",
            data={"text": "ok"},
            metadata=ArtifactMetadata(created_by="test"),
        )
    )

    snapshot = json.loads(json.dumps(store.snapshot(), ensure_ascii=False))
    ids = [a["artifact_id"] for a in snapshot["artifacts"]]
    assert "bad-data" not in ids
    assert "good-data" in ids


def test_load_snapshot_ignores_wrong_version(tmp_path):
    store = ArtifactStore(cache_dir=str(tmp_path))
    accepted = store.load_snapshot({"version": 999, "artifacts": [{"artifact_id": "x"}]})
    assert accepted is False
    assert store.list_all() == []

    # Non-dict / empty input is rejected too.
    assert store.load_snapshot({}) is False
    assert store.load_snapshot(None) is False
    assert store.list_all() == []


def test_prepare_artifact_store_prefers_snapshot(tmp_path):
    from courtier.agent.api.services import run_manager

    source = ArtifactStore(cache_dir=str(tmp_path))
    _make_disk_ref_artifact(source, tmp_path, "ref-parse-1", {"pages": []})
    snapshot_json = json.dumps(source.snapshot(), ensure_ascii=False)

    context_manager = SimpleNamespace(_cache=ArtifactStore(cache_dir=str(tmp_path)))
    store = run_manager._prepare_artifact_store_for_session(
        prior_state=None,
        context_manager=context_manager,
        artifact_snapshot=snapshot_json,
    )

    assert store.get("ref-parse-1") is not None


def test_prepare_artifact_store_ignores_unusable_snapshot(tmp_path):
    """An unusable snapshot leaves an empty store — restore is snapshot-only,
    there is no marker-based fallback anymore."""
    from courtier.agent.api.services import run_manager

    context_manager = SimpleNamespace(_cache=ArtifactStore(cache_dir=str(tmp_path)))
    prior_state = SimpleNamespace(messages=[object()])
    store = run_manager._prepare_artifact_store_for_session(
        prior_state=prior_state,
        context_manager=context_manager,
        artifact_snapshot='{"version": 999}',  # unusable snapshot
    )

    assert store.list_all() == []


@pytest.mark.asyncio
async def test_load_snapshot_restores_ref_counters_multi_prefix(tmp_path):
    """Session restore must not reset ref numbering — a new persist after
    restore continues each tool's sequence instead of reissuing
    ``$ref:<tool>:1`` and silently overwriting the restored entry."""
    source = ArtifactStore(cache_dir=str(tmp_path))
    big = {"text": "x" * 4000}  # above the default 3000-char persist threshold
    r1 = await source.persist(dict(big), "parse_layout")
    r2 = await source.persist({"text": "y" * 4000}, "parse_layout")
    r3 = await source.persist(dict(big), "search_documents")
    assert r1.ref_id == "$ref:parse_layout:1"
    assert r2.ref_id == "$ref:parse_layout:2"
    assert r3.ref_id == "$ref:search_documents:1"
    parse_file_v2 = source.ref_map[r2.ref_id]

    snapshot = json.loads(json.dumps(source.snapshot(), ensure_ascii=False))
    restored = ArtifactStore.restore(snapshot, cache_dir=str(tmp_path))

    # Counters were inferred from the restored ref_map keys.
    assert restored._backend.ref_counters["parse_layout"] == 2
    assert restored._backend.ref_counters["search_documents"] == 1

    # New persists continue each sequence instead of wrapping to 1.
    r4 = await restored.persist({"text": "z" * 4000}, "parse_layout")
    r5 = await restored.persist({"text": "w" * 4000}, "search_documents")
    assert r4.ref_id == "$ref:parse_layout:3"
    assert r5.ref_id == "$ref:search_documents:2"

    # The previously persisted document is still intact under its old ref.
    assert restored.ref_map[r2.ref_id] == parse_file_v2
    assert restored.load(r2.ref_id) is not None


@pytest.mark.asyncio
async def test_ref_counter_restore_ignores_non_numeric_refs(tmp_path):
    """``$ref:<tool>:latest`` style aliases must not disturb numbering."""
    store = ArtifactStore(cache_dir=str(tmp_path))
    store.set_ref("$ref:parse_layout:latest", str(tmp_path / "f.json"))
    store.set_ref("$ref:parse_layout:4", str(tmp_path / "g.json"))
    store.set_ref("plain-artifact-id", str(tmp_path / "h.json"))
    assert store._backend.ref_counters == {"parse_layout": 4}


def test_snapshot_carries_ref_counters(tmp_path):
    """Snapshot includes per-tool numbering so continuation keeps sequence."""
    import asyncio

    store = ArtifactStore(cache_dir=str(tmp_path), session_id="sess_a")
    asyncio.run(store.persist({"x": 1}, "echo", force=True))
    asyncio.run(store.persist({"x": 2}, "echo", force=True))
    snapshot = store.snapshot()
    assert snapshot["ref_counters"] == {"echo": 2}
    assert snapshot["version"] == 2

    restored = ArtifactStore.restore(snapshot, cache_dir=str(tmp_path), session_id="sess_a")
    assert restored._backend.ref_counters == {"echo": 2}
