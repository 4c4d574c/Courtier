"""Account deletion service — the cascading cleanup pipeline.

One entry point (:func:`execute_account_deletion`) for both triggers
(self-request approved by an admin / admin direct deletion).  Ordering
follows `docs/architecture/account-deletion-plan.md`:

1. stop the user's in-flight runs
2. ES result docs + chat session records (per-session cleanup)
3. uploaded files (registry + disk)
4. personal resources (MySQL + ES chunks + MinIO via delete_resource);
   public resources defensively kept with owner_id set to NULL
5. one final transaction: audit_results rows, user memory (+ actor
   anonymization), settings audit/updated_by anonymization, the user row
   itself (refresh tokens go via FK CASCADE), pending request rows, and
   the deletion receipt.

Failure semantics: external cleanup is best-effort — a failing step is
recorded in the receipt's ``failures`` and does not abort; the final
transaction either fully commits or rolls back, so the worst case is
"external data over-deleted, rows remain" which a rerun converges —
never "account gone but rows still referencing it".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, update

from courtier.db.tables import (
    REQUEST_EXECUTED,
    REQUEST_PENDING,
    ContentAuditResultTable,
    CorrectionAuditResultTable,
    DeletionLogTable,
    DeletionRequestTable,
    FormatAuditResultTable,
    MemoryChangeTable,
    PlagiarismAuditResultTable,
    ResourceTable,
    SettingsChangeTable,
    SettingsTable,
    UserTable,
    WritingStyleAuditResultTable,
)
from courtier.db.tables.base import utcnow

logger = logging.getLogger(__name__)

#: audit_results tables key their owner by username string.
_AUDIT_RESULT_TABLES = (
    FormatAuditResultTable,
    ContentAuditResultTable,
    PlagiarismAuditResultTable,
    CorrectionAuditResultTable,
    WritingStyleAuditResultTable,
)


@dataclass
class DeletionResult:
    """What the receipt records for one executed account deletion."""

    user_id: int
    trigger: str  # "self_approved" | "admin"
    executed_by: str
    counts: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def to_counts_dict(self) -> dict:
        return {"counts": dict(self.counts), "failures": dict(self.failures)}


async def execute_account_deletion(
    db: Any,
    settings: Any,
    session_store: Any,
    file_store: Any,
    *,
    user_id: int,
    username: str,
    trigger: str,
    executed_by: str,
    run_manager: Any | None = None,
    resource_deleter: Any | None = None,
    session_results_cleaner: Any | None = None,
) -> DeletionResult:
    """Wipe every trace of *user_id* and write the deletion receipt.

    ``resource_deleter`` defaults to resource_service.delete_resource
    (MySQL row + ES chunks + MinIO originals); ``session_results_cleaner``
    defaults to es_backend.delete_results_by_sessions.  Both are
    injectable for tests.
    """
    if resource_deleter is None:
        from .resource_service import delete_resource as _default_resource_deleter

        resource_deleter = _default_resource_deleter
    if session_results_cleaner is None:
        from ...runtime.es_backend import (
            delete_results_by_sessions as _default_session_results_cleaner,
        )

        session_results_cleaner = _default_session_results_cleaner

    result = DeletionResult(user_id=user_id, trigger=trigger, executed_by=executed_by)

    # -- 1. stop in-flight runs ------------------------------------------------
    if run_manager is not None:
        try:
            stopped = await run_manager.stop_user(username)
            result.counts["runs_stopped"] = len(stopped)
        except Exception as exc:
            logger.warning("stop_user failed for %s", username, exc_info=True)
            result.failures["runs"] = str(exc)

    # -- 2. session records + their ES result docs -----------------------------
    session_ids: list[str] = []
    try:
        summaries = await session_store.list_all(
            current_user=username, is_admin=False, limit=-1
        )
        session_ids = [str(s.get("id")) for s in summaries if s.get("id")]
    except Exception as exc:
        result.failures["sessions_list"] = str(exc)

    if session_ids:
        try:
            deleted_results = session_results_cleaner(session_ids)
            result.counts["es_results"] = max(0, int(deleted_results))
        except Exception as exc:
            logger.warning("ES result cleanup failed for %s", username, exc_info=True)
            result.failures["es_results"] = str(exc)

    for sid in session_ids:
        try:
            from .session_service import delete_session

            if await delete_session(
                session_store, sid, username, cache_dir=str(settings.cache_dir)
            ):
                result.counts["sessions"] = result.counts.get("sessions", 0) + 1
        except Exception as exc:
            logger.warning("Session deletion failed for %s", sid, exc_info=True)
            result.failures[f"session:{sid}"] = str(exc)

    # -- 3. uploaded files ------------------------------------------------------
    try:
        removed_files = await file_store.delete_owned(username, str(settings.upload_dir))
        result.counts["files"] = len(removed_files)
    except Exception as exc:
        logger.warning("File cleanup failed for %s", username, exc_info=True)
        result.failures["files"] = str(exc)

    # -- 4. personal resources (public kept, owner anonymized) -----------------
    try:
        async with db.session() as session:
            rows = (
                await session.execute(
                    select(ResourceTable.id).where(
                        ResourceTable.owner_id == user_id,
                        ResourceTable.visibility == "personal",
                    )
                )
            ).scalars().all()
        deleted_resources = 0
        for rid in rows:
            try:
                if await resource_deleter(db, settings, int(rid), owner_id=user_id) is not None:
                    deleted_resources += 1
            except Exception as exc:
                logger.warning("Resource %s deletion failed", rid, exc_info=True)
                result.failures[f"resource:{rid}"] = str(exc)
        result.counts["resources"] = deleted_resources
    except Exception as exc:
        result.failures["resources_list"] = str(exc)

    # -- 5-9. the final transaction ---------------------------------------------
    anonymized_as = f"deleted-user:{user_id}"
    actor_values = [username, f"agent:{username}"]
    async with db.session() as session:
        public_update = await session.execute(
            update(ResourceTable)
            .where(ResourceTable.owner_id == user_id, ResourceTable.visibility == "public")
            .values(owner_id=None)
        )
        result.counts["public_resources_anonymized"] = public_update.rowcount or 0

        audit_rows = 0
        for table in _AUDIT_RESULT_TABLES:
            res = await session.execute(delete(table).where(table.user_id == username))
            audit_rows += res.rowcount or 0
        result.counts["audit_results"] = audit_rows

        result.counts["memories"] = await _delete_user_memory(
            session, user_id, commit=False
        )
        result.counts["memory_audit_anonymized"] = await _anonymize_actor(
            session, MemoryChangeTable, actor_values, anonymized_as
        )
        result.counts["settings_audit_anonymized"] = await _anonymize_actor(
            session, SettingsChangeTable, actor_values, anonymized_as
        )
        settings_rows = await session.execute(
            update(SettingsTable)
            .where(SettingsTable.updated_by == username)
            .values(updated_by=anonymized_as)
        )
        result.counts["settings_rows_anonymized"] = settings_rows.rowcount or 0

        user_row = await session.get(UserTable, user_id)
        result.counts["user"] = 1 if user_row is not None else 0
        if user_row is not None:
            await session.delete(user_row)

        pending = (
            await session.execute(
                update(DeletionRequestTable)
                .where(
                    DeletionRequestTable.user_id == user_id,
                    DeletionRequestTable.status == REQUEST_PENDING,
                )
                .values(status=REQUEST_EXECUTED, decided_by=executed_by, decided_at=utcnow())
            )
        ).rowcount or 0
        result.counts["requests_executed"] = pending

        session.add(
            DeletionLogTable(
                user_id=user_id,
                trigger=trigger,
                executed_by=executed_by,
                counts=result.counts,
                failures=result.failures,
            )
        )
        await session.commit()

    return result


async def _delete_user_memory(session: Any, user_id: int, *, commit: bool = True) -> int:
    from .memory_service import delete_user_memory

    try:
        return await delete_user_memory(session, user_id, commit=commit)
    except Exception:
        logger.warning("memory cleanup failed for user %s", user_id, exc_info=True)
        return 0


async def _anonymize_actor(
    session: Any, table: Any, actor_values: list[str], anonymized_as: str
) -> int:
    res = await session.execute(
        update(table).where(table.actor.in_(actor_values)).values(actor=anonymized_as)
    )
    return res.rowcount or 0
