"""MemoryService suite — permission matrix, audit, upsert, injection query.

Runs on a per-test sqlite database (same pattern as test_setup_routes);
the service is the single enforcement point for UI routes and the
memory_store host service alike, so the matrix is asserted here once.
"""

from __future__ import annotations

import pytest

from courtier.agent.api.services.memory_service import (
    Identity,
    MemoryAccessError,
    MemoryNotFoundError,
    MemoryValidationError,
    clear_layer,
    collect_for_injection,
    delete_entry,
    delete_user_memory,
    get_entry,
    list_changes,
    tool_action,
    update_entry,
    upsert_entry,
)
from courtier.db.db_manager import AsyncDatabase

ADMIN = Identity(owner_id=1, is_admin=True, actor="admin")
USER_A = Identity(owner_id=2, is_admin=False, actor="alice")
USER_B = Identity(owner_id=3, is_admin=False, actor="bob")


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


class TestPermissionMatrix:
    async def test_global_write_admin_only(self, db):
        async with db.session() as session:
            entry = await upsert_entry(
                session, layer="global", title="规范", content="c", identity=ADMIN
            )
            assert entry["layer"] == "global"
            with pytest.raises(MemoryAccessError):
                await upsert_entry(
                    session, layer="global", title="x", content="c", identity=USER_A
                )

    async def test_global_readable_by_everyone(self, db):
        async with db.session() as session:
            entry = await upsert_entry(
                session, layer="global", title="规范", content="c", identity=ADMIN
            )
            row = await get_entry(session, entry["id"], identity=USER_A)
            assert row["title"] == "规范"

    async def test_user_layers_are_invisible_to_others_including_admin(self, db):
        async with db.session() as session:
            mine = await upsert_entry(
                session, layer="user", title="偏好", content="简洁", identity=USER_A
            )
            # B 看不到 A 的条目（404 语义，不暴露存在性）
            with pytest.raises(MemoryNotFoundError):
                await get_entry(session, mine["id"], identity=USER_B)
            # admin 也看不到（对齐结论 7）
            with pytest.raises(MemoryNotFoundError):
                await get_entry(session, mine["id"], identity=ADMIN)
            # 本人可写可读
            assert (await get_entry(session, mine["id"], identity=USER_A))["content"] == "简洁"


class TestUpsertAndAudit:
    async def test_same_title_upserts_not_duplicates(self, db):
        async with db.session() as session:
            first = await upsert_entry(
                session, layer="user", title="语言", content="中文", identity=USER_A
            )
            second = await upsert_entry(
                session, layer="user", title="语言", content="中文、简洁", identity=USER_A
            )
            assert first["id"] == second["id"]
            assert second["content"] == "中文、简洁"

    async def test_identical_write_does_not_audit(self, db):
        async with db.session() as session:
            entry = await upsert_entry(
                session, layer="user", title="语言", content="中文", identity=USER_A
            )
            await upsert_entry(
                session, layer="user", title="语言", content="中文", identity=USER_A
            )
            changes = await list_changes(session)
            assert len(changes) == 1 and changes[0]["action"] == "create"
            _ = entry

    async def test_audit_records_hashes_and_actor(self, db):
        async with db.session() as session:
            entry = await upsert_entry(
                session, layer="user", title="语言", content="中文", identity=USER_A
            )
            await update_entry(
                session,
                entry["id"],
                identity=USER_A,
                content="中文、简洁",
            )
            await delete_entry(session, entry["id"], identity=USER_A)
            changes = await list_changes(session)
            assert [c["action"] for c in changes] == ["delete", "update", "create"]
            assert all(c["actor"] == "alice" for c in changes)
            assert changes[0]["newHash"] is None and changes[0]["oldHash"] is not None
            assert changes[2]["oldHash"] is None and changes[2]["newHash"] is not None

    async def test_domain_must_be_known_or_common(self, db):
        async with db.session() as session:
            ok = await upsert_entry(
                session,
                layer="user",
                title="口径",
                content="c",
                identity=USER_A,
                known_domains={"docaudit"},
            )
            assert ok["domain"] == "common"
            with pytest.raises(MemoryValidationError):
                await upsert_entry(
                    session,
                    layer="user",
                    title="口径",
                    content="c",
                    domain="docauditt",
                    identity=USER_A,
                    known_domains={"docaudit"},
                )

    async def test_empty_title_or_content_rejected(self, db):
        async with db.session() as session:
            with pytest.raises(MemoryValidationError):
                await upsert_entry(session, layer="user", title="  ", content="c", identity=USER_A)
            with pytest.raises(MemoryValidationError):
                await upsert_entry(session, layer="user", title="t", content="  ", identity=USER_A)


class TestToolAction:
    async def test_list_scopes_layers(self, db):
        async with db.session() as session:
            await upsert_entry(session, layer="global", title="规范", content="c", identity=ADMIN)
            await upsert_entry(session, layer="user", title="偏好", content="c", identity=USER_A)
            view = await tool_action(session, identity=USER_B, action="list")
            assert [e["title"] for e in view["global"]] == ["规范"]
            assert view["user"] == []

    async def test_read_prefers_own_user_layer(self, db):
        async with db.session() as session:
            await upsert_entry(session, layer="global", title="口径", content="全局", identity=ADMIN)
            mine = await upsert_entry(
                session, layer="user", title="口径", content="个人", identity=USER_A
            )
            row = await tool_action(session, identity=USER_A, action="read", title="口径")
            assert row["id"] == mine["id"] and row["content"] == "个人"

    async def test_write_global_via_tool_requires_admin(self, db):
        async with db.session() as session:
            with pytest.raises(MemoryAccessError):
                await tool_action(
                    session,
                    identity=USER_A,
                    action="write",
                    layer="global",
                    title="t",
                    content="c",
                )
            row = await tool_action(
                session,
                identity=ADMIN,
                action="write",
                layer="global",
                title="t",
                content="c",
            )
            assert row["layer"] == "global"

    async def test_unknown_action_rejected(self, db):
        async with db.session() as session:
            with pytest.raises(MemoryValidationError):
                await tool_action(session, identity=USER_A, action="wipe")


class TestInjectionCollection:
    async def test_collect_scopes_owner_and_active_domains(self, db):
        async with db.session() as session:
            await upsert_entry(
                session, layer="user", title="A通用", content="a", identity=USER_A
            )
            await upsert_entry(
                session, layer="user", title="A领域", content="b", domain="docaudit", identity=USER_A
            )
            await upsert_entry(
                session, layer="user", title="B通用", content="c", identity=USER_B
            )
            await upsert_entry(session, layer="global", title="规范", content="d", identity=ADMIN)

            grouped = await collect_for_injection(session, owner_id=USER_A.owner_id, domains=["docaudit"])
            user_titles = [e["title"] for e in grouped["user"]]
            assert user_titles == ["A通用", "A领域"]  # common 在前；不含 B 的
            assert [e["title"] for e in grouped["global"]] == ["规范"]

            narrow = await collect_for_injection(session, owner_id=USER_A.owner_id, domains=[])
            assert [e["title"] for e in narrow["user"]] == ["A通用"]


class TestUserCascade:
    async def test_delete_user_memory_removes_only_that_owner(self, db):
        async with db.session() as session:
            await upsert_entry(session, layer="user", title="A1", content="c", identity=USER_A)
            await upsert_entry(session, layer="user", title="B1", content="c", identity=USER_B)
            await upsert_entry(session, layer="global", title="规范", content="c", identity=ADMIN)

            removed = await delete_user_memory(session, USER_A.owner_id)
            assert removed == 1
            view = await tool_action(session, identity=USER_B, action="list")
            assert [e["title"] for e in view["user"]] == ["B1"]
            assert [e["title"] for e in view["global"]] == ["规范"]


class TestClearLayer:
    async def test_clear_user_layer_scoped_to_owner(self, db):
        async with db.session() as session:
            await upsert_entry(session, layer="user", title="A1", content="c", identity=USER_A)
            await upsert_entry(session, layer="user", title="A2", content="c", identity=USER_A)
            await upsert_entry(session, layer="user", title="B1", content="c", identity=USER_B)
            cleared = await clear_layer(session, layer="user", identity=USER_A)
            assert cleared == 2
            view = await tool_action(session, identity=USER_B, action="list")
            assert [e["title"] for e in view["user"]] == ["B1"]

    async def test_clear_global_requires_admin(self, db):
        async with db.session() as session:
            with pytest.raises(MemoryAccessError):
                await clear_layer(session, layer="global", identity=USER_A)
