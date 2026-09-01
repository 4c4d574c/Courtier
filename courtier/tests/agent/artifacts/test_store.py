from __future__ import annotations

from courtier.agent.artifacts.models import (
    Artifact,
    ArtifactContext,
    ArtifactContextEntry,
    ArtifactMetadata,
    ArtifactPermission,
)
from courtier.agent.artifacts.store import ArtifactStore, ScopedArtifactStore


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
            created_by="debug_tool",
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
        ref_id="$ref:parse_layout:1",
        artifact_type="docaudit.parsed_document",
        created_by="parse_layout",
        data={"pages": []},
        role="primary_document",
        subject="current_upload",
    )

    assert artifact.artifact_id == "$ref:parse_layout:1"
    assert artifact.artifact_type == "docaudit.parsed_document"
    assert artifact.metadata.semantic_role == "primary_document"
    assert artifact.metadata.subject == "current_upload"


# -- Gap 6: persist / llm_visible policy enforcement ---------------------------

def test_persist_never_drops_artifact():
    store = ArtifactStore()
    store.set_type_policy("core.debug_view", persist="never", llm_visible="hidden")
    artifact = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(created_by="debug_tool"),
    )

    returned = store.put(artifact)
    assert store.get("debug1") is None  # not stored
    assert returned.artifact_id == "debug1"  # returned but not kept


def test_persist_auto_stores_artifact():
    store = ArtifactStore()
    store.set_type_policy("core.plain_text", persist="auto", llm_visible="summary")
    artifact = Artifact(
        artifact_id="plain1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(artifact)
    assert store.get("plain1") is not None


def test_list_visible_excludes_hidden_types():
    store = ArtifactStore()
    store.set_type_policy("core.plain_text", persist="auto", llm_visible="summary")
    store.set_type_policy("core.debug_view", persist="never", llm_visible="hidden")

    visible = Artifact(
        artifact_id="v1",
        artifact_type="core.plain_text",
        data={"text": "visible"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    hidden = Artifact(
        artifact_id="h1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(created_by="test"),
    )

    store.put(visible)
    store.put(hidden)  # debug_view with persist=never won't be stored

    visible_ids = [a.artifact_id for a in store.list_visible()]
    assert "v1" in visible_ids
    assert "h1" not in visible_ids


# -- Gap 6: scoped artifact context for subagents ------------------------------

def test_scoped_store_allow_all_bypasses_restrictions():
    """allow_all=True gives unrestricted access."""
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    store.put(artifact)

    ctx = ArtifactContext(allow_all=True)
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None
    assert len(scoped.list_projection_candidates()) == 1


def test_scoped_store_restricts_access_by_artifact_id():
    """Only artifacts listed in the context are accessible."""
    store = ArtifactStore()
    for aid in ("a1", "a2"):
        store.put(Artifact(
            artifact_id=aid,
            artifact_type="core.plain_text",
            data={"text": aid},
            metadata=ArtifactMetadata(created_by="test"),
        ))

    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(ref="a1", permissions=ArtifactPermission()),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None
    assert scoped.get("a2") is None  # not in context


def test_scoped_store_project_permission():
    """Artifact without project permission is excluded from projection candidates."""
    store = ArtifactStore()
    artifact = Artifact(
        artifact_id="a1",
        artifact_type="core.plain_text",
        data={"text": "hello"},
        metadata=ArtifactMetadata(created_by="test"),
    )
    store.put(artifact)

    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(
                ref="a1",
                permissions=ArtifactPermission(project=False),
            ),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)

    assert scoped.get("a1") is not None  # can read
    assert scoped.list_projection_candidates() == []  # but can't project


def test_scoped_store_debug_read_permission():
    """Debug artifacts are excluded when debug_read=False (default)."""
    store = ArtifactStore()
    debug = Artifact(
        artifact_id="debug1",
        artifact_type="core.debug_view",
        data={"schema": {}},
        metadata=ArtifactMetadata(
            created_by="debug_tool",
            projection_allowed=False,
            debug_only=True,
        ),
    )
    store.put(debug)

    # Context without debug_read — debug artifact is invisible
    ctx = ArtifactContext(
        allowed_artifacts=(
            ArtifactContextEntry(
                ref="debug1",
                permissions=ArtifactPermission(debug_read=False),
            ),
        ),
    )
    scoped = ScopedArtifactStore(store, ctx)
    assert scoped.get("debug1") is not None  # explicit allow overrides

    # Context without listing debug artifact at all
    ctx2 = ArtifactContext()
    scoped2 = ScopedArtifactStore(store, ctx2)
    assert scoped2.get("debug1") is None  # not allowed
    assert scoped2.list_projection_candidates() == []  # debug excluded


def test_scoped_store_can_create_new_artifacts():
    """Subagent can always create new artifacts in the backing store."""
    store = ArtifactStore()
    ctx = ArtifactContext()  # empty context
    scoped = ScopedArtifactStore(store, ctx)

    new_artifact = scoped.register_cached_ref(
        ref_id="subagent_result",
        artifact_type="docaudit.plagiarism_report",
        created_by="detect_plagiarism",
        data={"is_plagiarism": False},
        role="report",
    )
    assert store.get("subagent_result") is not None
    assert new_artifact.artifact_type == "docaudit.plagiarism_report"


def test_find_by_type_returns_matching_artifacts():
    store = ArtifactStore()
    store.put(Artifact(
        artifact_id="a1", artifact_type="core.plain_text",
        data={"text": "hello"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a2", artifact_type="docaudit.parsed_document",
        data={"pages": []}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a3", artifact_type="core.plain_text",
        data={"text": "world"}, metadata=ArtifactMetadata(created_by="test"),
    ))

    assert len(store.find_by_type("core.plain_text")) == 2
    assert len(store.find_by_type("docaudit.parsed_document")) == 1
    assert store.find_by_type("nonexistent") == []


def test_list_all_returns_all_stored():
    store = ArtifactStore()
    store.put(Artifact(
        artifact_id="a1", artifact_type="core.plain_text",
        data={"text": "hello"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    store.put(Artifact(
        artifact_id="a2", artifact_type="core.plain_text",
        data={"text": "world"}, metadata=ArtifactMetadata(created_by="test"),
    ))
    assert len(store.list_all()) == 2


def test_require_raises_keyerror_for_missing_artifact():
    store = ArtifactStore()
    try:
        store.require("nonexistent")
        assert False, "Expected KeyError"
    except KeyError:
        pass


# -- Layer-1 large-output persistence (migrated from the legacy
#    tests/agent/test_context_manager.py; outline §2/plan task 10) ------------


class TestPersistLargeOutput:
    async def test_small_data_passes_through(self, tmp_path):
        """迁移：小于阈值的数据原样返回，不产生 ref。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        result = (await store.persist({"key": "value"}, "test_tool")).data
        assert result == {"key": "value"}
        assert store.ref_map == {}

    async def test_large_data_persisted_with_ref_id(self, tmp_path):
        """迁移：超过阈值 → __persisted_output__ 标记 + ref_id + 预览。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        result = (await store.persist({"text": "x" * 1000}, "search_documents")).data
        assert result["__persisted_output__"] is True
        assert result["ref_id"] == "$ref:search_documents:1"
        assert "file" in result
        assert result["size_chars"] > 900
        assert "preview" in result

    async def test_identical_data_deduped_to_same_ref(self, tmp_path):
        """迁移：同工具同内容去重复用缓存文件；不同工具独立编号。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        data = {"text": "x" * 1000}
        r1 = (await store.persist(data, "search_documents")).data
        r2 = (await store.persist(data, "search_documents")).data
        r3 = (await store.persist(data, "other_tool")).data
        assert r2["ref_id"] == r1["ref_id"] == "$ref:search_documents:1"
        assert r2.get("dedup_hit") is True
        assert r1["file"] == r2["file"]
        assert r3["ref_id"] == "$ref:other_tool:1"

    async def test_different_data_gets_new_ref(self, tmp_path):
        """迁移：不同内容递增 ref 序号。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        r1 = (await store.persist({"text": "a" * 1000}, "search_documents")).data
        r2 = (await store.persist({"text": "b" * 1000}, "search_documents")).data
        assert (r1["ref_id"], r2["ref_id"]) == (
            "$ref:search_documents:1",
            "$ref:search_documents:2",
        )

    async def test_ref_map_tracks_all_persisted_outputs(self, tmp_path):
        """迁移：ref_map 登记全部 ref → 文件路径。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        data = {"text": "x" * 1000}
        r1 = (await store.persist(data, "search_documents")).data
        r2 = (await store.persist(data, "other_tool")).data
        assert store.ref_map[r1["ref_id"]] == r1["file"]
        assert store.ref_map[r2["ref_id"]] == r2["file"]

    async def test_all_tools_persisted_no_passthrough(self, tmp_path):
        """迁移：审计类工具结果同样落盘（无白名单直通）。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        for tool_name in ["parse_layout", "audit_format", "audit_content"]:
            result = (await store.persist({"text": "x" * 1000}, tool_name)).data
            assert result["__persisted_output__"] is True

    async def test_none_data_passes_through_no_schema(self, tmp_path):
        """迁移：None 原样返回，不写文件也不产生 schema。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        result = (await store.persist(None, "test_tool")).data
        assert result is None
        assert store.ref_map == {}
        assert list(tmp_path.rglob("*.json")) == []

    async def test_file_on_disk_matches_data(self, tmp_path):
        """迁移：盘上 JSON 文件内容与原数据一致。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        data = {"text": "x" * 1000}
        result = (await store.persist(data, "search_documents")).data
        import json as _json
        from pathlib import Path

        loaded = _json.loads(Path(result["file"]).read_text(encoding="utf-8"))
        assert loaded == data


class TestPersistMany:
    async def test_batch_writes_all_items_with_unique_refs(self, tmp_path):
        """批量落盘：每项独立 ref 与盘上文件，ref_map 完整。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        results = await store.persist_many(
            [("tool_a", {"text": "x" * 900}), ("tool_b", {"text": "y" * 900})]
        )
        assert [r.persisted for r in results] == [True, True]
        assert results[0].ref_id == "$ref:tool_a:1"
        assert results[1].ref_id == "$ref:tool_b:1"
        assert results[0].data["file"] != results[1].data["file"]
        assert len(store.ref_map) == 2
        assert store.load("$ref:tool_a:1") == {"text": "x" * 900}

    async def test_batch_dedups_within_and_across_calls(self, tmp_path):
        """批内相同内容共享 ref（不重复写盘）；后续单次 persist 命中批内记录。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        data = {"text": "x" * 900}
        r1, r2 = await store.persist_many([("tool_a", data), ("tool_a", data)])
        assert r1.ref_id == r2.ref_id == "$ref:tool_a:1"
        assert r1.data["file"] == r2.data["file"]
        assert r1.data.get("dedup_hit") is None  # first occurrence is the write
        assert r2.data.get("dedup_hit") is True

        r3 = (await store.persist(data, "tool_a")).data
        assert r3["ref_id"] == "$ref:tool_a:1" and r3.get("dedup_hit") is True

    async def test_batch_degrades_per_item_on_write_failure(self, tmp_path, monkeypatch):
        """单项写失败只降级该项，其余项正常落盘。"""
        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        original = store._backend._write_payloads

        def _selective_fail(jobs):
            out = original(jobs)
            out[0] = None  # first item's write "fails"
            return out

        monkeypatch.setattr(store._backend, "_write_payloads", _selective_fail)
        results = await store.persist_many(
            [("tool_a", {"text": "x" * 900}), ("tool_b", {"text": "y" * 900})]
        )
        assert results[0].persisted is False and results[0].data == {"text": "x" * 900}
        assert results[1].persisted is True
        assert store.load("$ref:tool_b:1") == {"text": "y" * 900}

    async def test_batch_read_only_dir_degrades_all(self, tmp_path):
        """只读目录 → 全部降级为未持久化，不抛异常。"""
        import stat

        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)
        tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            results = await store.persist_many(
                [("tool_a", {"text": "x" * 900}), ("tool_b", {"text": "y" * 900})]
            )
        finally:
            tmp_path.chmod(stat.S_IRWXU)
        assert all(r.persisted is False for r in results)
        assert store.ref_map == {}


class TestConcurrentSharedStore:
    async def test_concurrent_batch_and_single_persists_share_store_safely(self, tmp_path):
        """父子代理共享同一 store 并发落盘：ref 不碰撞、全部可读。

        Covers the runtime scenario where the orchestrator (batching via
        micro-compact persist_many) and its sub-agents (single persists)
        hit the same ArtifactStore concurrently.
        """
        import asyncio

        store = ArtifactStore(cache_dir=str(tmp_path), large_output_threshold=500)

        async def batch():
            return await store.persist_many(
                [("parent_tool", {"text": "p" * 900, "n": i}) for i in range(5)]
            )

        async def single(i: int):
            payload = {"text": f"child-{i}-" + "y" * 400}
            return await store.persist(payload, "child_tool", force=True)

        gathered = await asyncio.gather(batch(), *[single(i) for i in range(10)])
        batch_results, single_results = gathered[0], gathered[1:]

        all_results = [*batch_results, *single_results]
        assert all(r.persisted for r in all_results)
        ref_ids = [r.ref_id for r in all_results]
        assert len(set(ref_ids)) == len(ref_ids), "ref ids must not collide"
        for r in all_results:
            assert store.load(r.ref_id) is not None
