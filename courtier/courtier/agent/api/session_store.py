"""SessionStore — in-memory + JSON file persistence for agent sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import SessionRecord, StepRecord

logger = logging.getLogger(__name__)

# Session IDs are server-generated: sess_ + 12 hex chars
_SESSION_ID_RE = re.compile(r"^sess_[a-f0-9]{12}$")


def _advance_watermark(session: SessionRecord, event_seq: int | None) -> SessionRecord:
    """Stamp the event-log watermark onto a record being mutated.

    Called inside the same in-memory mutation as a content change so a
    concurrent snapshot reader can never see content without its watermark
    (or vice versa). Only ever advances — a stale stamp must not regress.
    """
    if event_seq is None or event_seq <= session.event_seq:
        return session
    return replace(session, event_seq=event_seq)


class SessionStore:
    """Thread-safe session storage backed by JSON files.

    Sessions are kept in memory during the agent run and persisted to disk
    on each update for crash recovery and historical queries.
    """

    def __init__(self, storage_dir: str) -> None:
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, SessionRecord] = {}
        # asyncio.Lock protects in-memory dict operations.
        self._lock = asyncio.Lock()
        # _persist_lock serializes disk writes so concurrent updates cannot
        # leave the JSON file with an older snapshot after a newer one was
        # already committed to memory.
        self._persist_lock = asyncio.Lock()

    # -- CRUD ----------------------------------------------------------------

    async def create(
        self,
        session_id: str,
        task: str,
        file_id: str,
        file_name: str = "",
        model_name: str = "",
        created_at: float = 0.0,
        owner: str = "",
        status: str = "running",
    ) -> SessionRecord:
        import time as _time

        now = created_at or _time.time()
        session = SessionRecord(
            id=session_id,
            task=task,
            file_id=file_id,
            file_name=file_name,
            model_name=model_name,
            status=status,  # type: ignore[arg-type]
            created_at=now,
            owner=owner,
            turn_messages=[
                {
                    "text": task,
                    "timestamp": now,
                    "fileName": file_name or None,
                    "fileId": file_id or None,
                }
            ],
            turn_step_starts=[0],
            turn_conclusions=[],
        )
        async with self._lock:
            self._sessions[session_id] = session
        async with self._persist_lock:
            await self._persist(session)
        return session

    async def get(self, session_id: str) -> SessionRecord | None:
        if not _SESSION_ID_RE.match(session_id):
            logger.warning("Rejected invalid session_id: %s", session_id)
            return None
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            return session
        return self._load(session_id)

    async def get_owned(
        self, session_id: str, current_user: str, is_admin: bool = False
    ) -> SessionRecord | None:
        session = await self.get(session_id)
        if session is None:
            return None
        if not is_admin and (not session.owner or session.owner != current_user):
            return None
        return session

    async def list_all(
        self,
        current_user: str = "",
        is_admin: bool = False,
        skip: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return sessions as summary dicts, pinned first then most recent first.

        Non-admin users only see sessions they own.  ``skip``/``limit``
        are applied after filtering and sorting.
        """
        sessions: list[SessionRecord] = []
        async with self._lock:
            sessions = list(self._sessions.values())

        # Also load any sessions not currently in memory
        for path in sorted(self._dir.glob("*.json"), reverse=True):
            sid = path.stem
            if sid in self._sessions:
                continue
            loaded = self._load(sid)
            if loaded:
                sessions.append(loaded)

        if not is_admin:
            sessions = [s for s in sessions if s.owner == current_user]

        sessions.sort(key=lambda s: (0 if s.pinned else 1, -s.created_at))
        if skip:
            sessions = sessions[skip:]
        if limit is not None and limit >= 0:
            sessions = sessions[:limit]
        return [s.to_summary_dict() for s in sessions]

    async def delete(self, session_id: str) -> bool:
        if not _SESSION_ID_RE.match(session_id):
            logger.warning("Rejected invalid session_id: %s", session_id)
            return False
        async with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
        file_path = self._dir / f"{session_id}.json"
        if file_path.exists():
            file_path.unlink()
            removed = True
        return removed

    async def gc_orphan_cache_files(self, deleted_artifact_snapshot: str, cache_dir: str) -> int:
        """Delete cache files referenced only by a just-deleted session.

        Parses the deleted session's artifact snapshot ``ref_map``; any cache
        file no remaining session references (via their snapshots) is
        unlinked together with its ``.schema.json`` companion.  Returns the
        number of data files deleted.  Best-effort: errors are logged, never
        raised — session deletion must not fail because of GC.
        """
        if not deleted_artifact_snapshot:
            return 0
        try:
            ref_map = json.loads(deleted_artifact_snapshot).get("ref_map") or {}
        except (json.JSONDecodeError, AttributeError):
            return 0
        if not ref_map:
            return 0

        # Files still referenced by any remaining session.
        referenced: set[str] = set()
        for path in self._dir.glob("sess_*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                snapshot = raw.get("artifact_snapshot") or ""
                if snapshot:
                    referenced.update((json.loads(snapshot).get("ref_map") or {}).values())
            except (OSError, json.JSONDecodeError, AttributeError):
                continue

        cache_root = Path(cache_dir).resolve()
        deleted_count = 0
        for filepath in set(ref_map.values()):
            if filepath in referenced:
                continue
            target = (cache_root / filepath).resolve()
            try:
                target.relative_to(cache_root)
            except ValueError:
                logger.warning("GC rejected path outside cache_dir: %s", filepath)
                continue
            candidates = [target]
            # Schema companion written alongside the data file.
            schema = (
                target.with_suffix(".schema.json")
                if target.suffix == ".json"
                else Path(str(target) + ".schema.json")
            )
            candidates.append(schema)
            for candidate in candidates:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    logger.warning("GC failed to remove %s", candidate, exc_info=True)
            deleted_count += 1
        if deleted_count:
            logger.info(
                "GC removed %d orphaned cache file(s) after session deletion",
                deleted_count,
            )
        return deleted_count

    async def update(self, session_id: str, **kwargs: Any) -> SessionRecord | None:
        """Update a session record's fields and persist to disk.

        Accepts an optional ``event_seq`` kwarg: the event-log watermark is
        advanced (never regressed) atomically with this mutation.
        """
        if not _SESSION_ID_RE.match(session_id):
            logger.warning("Rejected invalid session_id in update: %s", session_id)
            return None
        stamp = kwargs.pop("event_seq", None)
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._load(session_id)
                if session is None:
                    return None
                self._sessions[session_id] = session

            session = replace(session, **{k: v for k, v in kwargs.items() if hasattr(session, k)})
            session = _advance_watermark(session, stamp)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)
        return session

    async def add_step(self, session_id: str, step, *, event_seq: int | None = None) -> None:
        """Append a step record to the session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session = replace(session, steps=session.steps + [step])
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    async def add_thought(self, session_id: str, thought, *, event_seq: int | None = None) -> None:
        """Append a thought record to the session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session = replace(session, thoughts=session.thoughts + [thought])
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session
        # Don't persist on every thought token — too frequent (would cause
        # excessive disk I/O during streaming).  Thought tokens are ephemeral:
        # they are lost on crash between step boundaries.  Persistence happens
        # at step boundaries and session end.

    async def add_tool_info(
        self, session_id: str, tool_info, *, event_seq: int | None = None
    ) -> None:
        """Append a tool info record to the current step of the session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.steps:
                return
            last_step = session.steps[-1]
            updated_step = replace(last_step, tools=last_step.tools + [tool_info])
            new_steps = list(session.steps)
            new_steps[-1] = updated_step
            session = replace(session, steps=new_steps)
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    async def add_turn(
        self,
        session_id: str,
        task: str,
        file_name: str | None = None,
        file_id: str | None = None,
        *,
        event_seq: int | None = None,
    ) -> None:
        """Record a new turn boundary for multi-turn continuation.

        ``file_name``/``file_id`` keep the upload association when a turn is
        re-registered after an edit-truncation (the original turn carried an
        upload), and let a restored session re-send the exact file reference
        on a later edit.
        """
        import time as _time

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            snapshots = session.turn_artifact_snapshots
            # The first turn is registered by create() (no snapshot yet);
            # every later turn records the store state at its start, keeping
            # len(snapshots) == len(turn_messages) - 1.
            if len(session.turn_messages) >= 1:
                snapshots = snapshots + [session.artifact_snapshot]
            session = replace(
                session,
                turn_messages=session.turn_messages
                + [
                    {
                        "text": task,
                        "timestamp": _time.time(),
                        "fileName": file_name,
                        "fileId": file_id,
                    }
                ],
                turn_step_starts=session.turn_step_starts + [len(session.steps)],
                turn_artifact_snapshots=snapshots,
            )
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    async def finalize_turn_conclusion(
        self, session_id: str, conclusion: str, *, event_seq: int | None = None
    ) -> SessionRecord | None:
        """Append or update the conclusion for the current turn.

        For multi-turn sessions each completed turn gets its own conclusion,
        so earlier turns remain visible in history instead of being overwritten
        by the final turn's conclusion.
        """
        if not _SESSION_ID_RE.match(session_id):
            logger.warning(
                "Rejected invalid session_id in finalize_turn_conclusion: %s",
                session_id,
            )
            return None
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._load(session_id)
                if session is None:
                    return None
                self._sessions[session_id] = session

            conclusions = list(session.turn_conclusions)
            if len(conclusions) < len(session.turn_messages):
                conclusions.append(conclusion)
            elif conclusions:
                conclusions[-1] = conclusion
            else:
                conclusions = [conclusion]

            session = replace(session, turn_conclusions=conclusions)
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)
        return session

    async def set_verdict(
        self, session_id: str, verdict: str, *, event_seq: int | None = None
    ) -> None:
        """Set the verdict text on the current step."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.steps:
                return
            last_step = session.steps[-1]
            updated_step = replace(last_step, verdict=verdict)
            new_steps = list(session.steps)
            new_steps[-1] = updated_step
            session = replace(session, steps=new_steps)
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    async def set_step_subagents(
        self,
        session_id: str,
        step_index: int,
        subagents: list,
        *,
        event_seq: int | None = None,
    ) -> None:
        """Persist the live sub-agent tree onto a step that is still running.

        Incremental counterpart of ``finalize_step``: called per sub-agent
        event so the session snapshot keeps honouring the watermark invariant
        (snapshot(W) + events>W ≡ full state).  Without it the tree reaches
        the store only at observe, and a mid-run attach replays sub-agent
        events into a snapshot step that never carried them.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            new_steps = list(session.steps)
            for i, step in enumerate(new_steps):
                if step.index != step_index:
                    continue
                new_steps[i] = replace(step, subagents=list(subagents))
                break
            session = replace(session, steps=new_steps)
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    async def finalize_step(
        self,
        session_id: str,
        step_index: int,
        *,
        subagents: list | None = None,
        end_segment_index: int | None = None,
        event_seq: int | None = None,
    ) -> None:
        """Update a step with sub-agent tree and segment boundaries after observe.

        Args:
            session_id: Session identifier.
            step_index: Global step index (1-based) to update.
            subagents: Optional list of SubagentRunRecord to attach.
            end_segment_index: End segment index for thought-to-step matching.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            new_steps = list(session.steps)
            for i, step in enumerate(new_steps):
                if step.index != step_index:
                    continue
                kwargs: dict[str, Any] = {}
                if subagents is not None:
                    kwargs["subagents"] = list(subagents)
                if end_segment_index is not None:
                    kwargs["end_segment_index"] = end_segment_index
                new_steps[i] = replace(step, **kwargs)
                break
            session = replace(session, steps=new_steps)
            session = _advance_watermark(session, event_seq)
            self._sessions[session_id] = session

        async with self._persist_lock:
            await self._persist(session)

    # -- Internal -------------------------------------------------------------

    async def _persist(self, session: SessionRecord) -> None:
        file_path = self._dir / f"{session.id}.json"
        data = session.to_detail_dict()
        data["file_id"] = session.file_id
        data["file_name"] = session.file_name
        data["error_detail"] = session.error_detail
        data["messages_json"] = session.messages_json
        data["artifact_snapshot"] = session.artifact_snapshot
        data["context_state"] = session.context_state
        data["tree_json"] = session.tree_json
        data["current_node_id"] = session.current_node_id
        data["owner"] = session.owner
        data["turn_messages"] = session.turn_messages
        data["turn_step_starts"] = session.turn_step_starts
        data["turn_conclusions"] = session.turn_conclusions
        data["turn_artifact_snapshots"] = list(session.turn_artifact_snapshots)
        data["pinned"] = session.pinned
        data["active_domains"] = list(session.active_domains)
        data["event_seq"] = session.event_seq
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            tmp_path = file_path.with_suffix(".json.tmp")
            await asyncio.to_thread(tmp_path.write_text, text, encoding="utf-8")
            await asyncio.to_thread(tmp_path.rename, file_path)
        except Exception:
            # Non-fatal: the in-memory session stays authoritative and every
            # subsequent mutation re-persists the full snapshot.  The log
            # record is the alert channel (SessionRecord is frozen, so no
            # per-record dirty flag can be attached here).
            logger.exception("Failed to persist session %s", session.id)

    def _load(self, session_id: str) -> SessionRecord | None:
        file_path = self._dir / f"{session_id}.json"
        if not file_path.exists():
            return None
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to load session %s", session_id)
            return None

        from .models import (
            SubagentRunRecord,
            ThoughtRecord,
            ToolInfo,
            normalize_tool_call_classification,
        )

        session = SessionRecord(
            id=raw.get("id", session_id),
            task=raw.get("task", ""),
            file_id=raw.get("file_id", raw.get("file_path", "")),
            file_name=raw.get("file_name", ""),
            model_name=raw.get("modelName", ""),
            status=raw.get("status", "completed"),
            tokens_in=raw.get("stats", {}).get("tokensIn", 0),
            tokens_out=raw.get("stats", {}).get("tokensOut", 0),
            created_at=raw.get("createdAt", 0.0),
            finished_at=raw.get("finishedAt"),
            error_detail=raw.get("error_detail"),
            conclusion=raw.get("conclusion", ""),
            messages_json=raw.get("messages_json", ""),
            artifact_snapshot=raw.get("artifact_snapshot", ""),
            context_state=raw.get("context_state", ""),
            tree_json=raw.get("tree_json", ""),
            current_node_id=raw.get("current_node_id"),
            owner=raw.get("owner", ""),
            turn_messages=raw.get("turn_messages", []),
            turn_step_starts=raw.get("turn_step_starts", []),
            turn_conclusions=raw.get("turn_conclusions", []),
            turn_artifact_snapshots=list(raw.get("turn_artifact_snapshots", [])),
            pinned=raw.get("pinned", False),
            active_domains=list(raw.get("active_domains", [])),
            event_seq=int(raw.get("event_seq", 0) or 0),
        )

        for s in raw.get("steps", []):
            tools = []
            for t in s.get("tools", []):
                classification = normalize_tool_call_classification(
                    {
                        "call_kind": t.get("callKind", t.get("call_kind")),
                        "call_scope": t.get("callScope", t.get("call_scope")),
                        "subagent_name": t.get("subagentName", t.get("subagent_name")),
                    }
                )
                tools.append(
                    ToolInfo(
                        name=t["name"],
                        skill=t.get("skill", ""),
                        status=t.get("status", "done"),
                        duration=t.get("duration", 0.0),
                        summary=t.get("summary", ""),
                        id=t.get("id", ""),
                        detail=t.get("detail"),
                        call_kind=classification["call_kind"],
                        call_scope=classification["call_scope"],
                        subagent_name=classification["subagent_name"],
                        display_name=t.get("displayName"),
                        parent_subagent_name=t.get("parentSubagentName"),
                        handle_id=t.get("handleId"),
                        parent_handle_id=t.get("parentHandleId"),
                        skill_description=t.get("skillDescription", ""),
                        issue_counts=t.get("issueCounts"),
                    )
                )
            session.steps.append(
                StepRecord(
                    index=s["index"],
                    label=s.get("label", ""),
                    skill=s.get("skill", ""),
                    tools=tools,
                    verdict=s.get("verdict", ""),
                    subagents=[SubagentRunRecord.from_dict(sa) for sa in s.get("subagents", [])],
                    turn_index=s.get("turnIndex", 0),
                    start_segment_index=s.get("startSegmentIndex", 0),
                    end_segment_index=s.get("endSegmentIndex"),
                )
            )

        for th in raw.get("thoughts", []):
            session.thoughts.append(
                ThoughtRecord(
                    id=th["id"],
                    text=th["text"],
                    turn=th.get("turn", 0),
                    timestamp=th.get("timestamp", 0.0),
                    segment_index=th.get("segmentIndex", 0),
                    segment_type=th.get("segmentType", "observe"),
                    step_index=th.get("stepIndex"),
                    turn_index=th.get("turnIndex", 0),
                )
            )

        return session
