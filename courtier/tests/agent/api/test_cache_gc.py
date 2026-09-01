"""Tests for SessionStore.gc_orphan_cache_files and cache hash dedup."""

import json
import random

import pytest

from courtier.agent.api.session_store import SessionStore
from courtier.agent.artifacts.store import ArtifactStore


def _rid() -> str:
    return "".join(random.choices("abcdef0123456789", k=12))


def _snapshot(ref_map: dict[str, str]) -> str:
    return json.dumps({"version": 1, "ref_map": ref_map, "artifacts": []}, ensure_ascii=False)


@pytest.mark.asyncio
class TestGcOrphanCacheFiles:
    async def test_removes_unreferenced_files(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        cache_dir = tmp_path / "cache"
        sessions_dir.mkdir()
        cache_dir.mkdir()
        store = SessionStore(str(sessions_dir))

        victim = cache_dir / "parse_layout_1_1.json"
        victim.write_text('{"a": 1}', encoding="utf-8")
        (cache_dir / "parse_layout_1_1.schema.json").write_text("{}", encoding="utf-8")
        keep = cache_dir / "search_documents_1_2.json"
        keep.write_text('{"b": 2}', encoding="utf-8")

        # Remaining session references `keep` only.
        remaining_id = f"sess_{_rid()}"
        await store.create(remaining_id, "t", "", owner="alice")
        await store.update(
            remaining_id,
            artifact_snapshot=_snapshot({"$ref:search_documents:1": str(keep)}),
        )

        deleted = await store.gc_orphan_cache_files(
            _snapshot({"$ref:parse_layout:1": str(victim)}), str(cache_dir)
        )

        assert deleted == 1
        assert not victim.exists()
        assert not (cache_dir / "parse_layout_1_1.schema.json").exists()
        assert keep.exists()

    async def test_keeps_files_shared_with_other_sessions(self, tmp_path):
        """Hash dedup means two sessions can reference the same file — the
        file must survive until the LAST referencing session is deleted."""
        sessions_dir = tmp_path / "sessions"
        cache_dir = tmp_path / "cache"
        sessions_dir.mkdir()
        cache_dir.mkdir()
        store = SessionStore(str(sessions_dir))

        shared = cache_dir / "parse_layout_1_1.json"
        shared.write_text('{"a": 1}', encoding="utf-8")

        remaining_id = f"sess_{_rid()}"
        await store.create(remaining_id, "t", "", owner="alice")
        await store.update(
            remaining_id,
            artifact_snapshot=_snapshot({"$ref:parse_layout:1": str(shared)}),
        )

        deleted = await store.gc_orphan_cache_files(
            _snapshot({"$ref:parse_layout:1": str(shared)}), str(cache_dir)
        )

        assert deleted == 0
        assert shared.exists()

    async def test_rejects_paths_outside_cache_dir(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        cache_dir = tmp_path / "cache"
        sessions_dir.mkdir()
        cache_dir.mkdir()
        store = SessionStore(str(sessions_dir))

        outside = tmp_path / "secret.json"
        outside.write_text("{}", encoding="utf-8")

        deleted = await store.gc_orphan_cache_files(
            _snapshot({"$ref:x:1": "../secret.json"}), str(cache_dir)
        )

        assert deleted == 0
        assert outside.exists()

    async def test_empty_snapshot_is_noop(self, tmp_path):
        store = SessionStore(str(tmp_path / "sessions"))
        assert await store.gc_orphan_cache_files("", str(tmp_path)) == 0
        assert await store.gc_orphan_cache_files("not json", str(tmp_path)) == 0


@pytest.mark.asyncio
class TestHashDedup:
    async def test_dedup_across_store_instances(self, tmp_path):
        """The hash index lives in the cache dir, so a fresh store (new
        request) dedups against files written by a previous instance."""
        data = {"text": "x" * 1000}
        store1 = ArtifactStore(cache_dir=str(tmp_path))
        r1 = await store1.persist(data, "parse_layout", force=True)

        store2 = ArtifactStore(cache_dir=str(tmp_path))
        r2 = await store2.persist(data, "parse_layout", force=True)

        assert r1.persisted and r2.persisted
        assert r1.ref_id == r2.ref_id
        assert r2.data["dedup_hit"] is True
        # Only one data file on disk.
        data_files = [
            p
            for p in tmp_path.iterdir()
            if p.suffix == ".json"
            and not p.name.endswith(".schema.json")
            and p.name != ".hash_index.json"
        ]
        assert len(data_files) == 1

    async def test_stale_index_entry_self_heals(self, tmp_path):
        """If the referenced file was deleted externally, the index entry is
        dropped and the data is re-persisted under a new ref."""
        data = {"text": "x" * 1000}
        store1 = ArtifactStore(cache_dir=str(tmp_path))
        await store1.persist(data, "parse_layout", force=True)
        for p in tmp_path.iterdir():
            if p.name.startswith("parse_layout_") and p.suffix == ".json":
                p.unlink()

        store2 = ArtifactStore(cache_dir=str(tmp_path))
        r2 = await store2.persist(data, "parse_layout", force=True)

        assert r2.persisted
        assert r2.data.get("dedup_hit") is not True
        # The data was re-written to disk (a fresh store restarts ref
        # numbering, so the ref_id itself may coincide).
        assert (tmp_path / r2.data["file"].split("/")[-1]).exists() or any(
            p.name.startswith("parse_layout_") and p.suffix == ".json" for p in tmp_path.iterdir()
        )
