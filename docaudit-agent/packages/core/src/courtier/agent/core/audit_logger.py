"""AuditLogger — structured, incremental audit logging for agent runs.

Each turn gets its own subdirectory with separate files for the LLM request,
LLM response, and tool executions.  This makes it easy to inspect individual
parts of a turn without parsing a single large JSON file.

Directory layout::

    .agent_logs/
    ├── orchestrator_abc12345/
    │   ├── turn_000/
    │   │   ├── llm_request.json
    │   │   ├── llm_response.json
    │   │   └── tool_executions.json
    │   ├── turn_001/
    │   │   └── ...
    │   └── run.json
    └── subagent_format_auditor_def67890/
        └── ...
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRequestRecord:
    """Record of the request sent to the LLM."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    model: str
    temperature: float | None


@dataclass(frozen=True)
class LLMResponseRecord:
    """Record of the response received from the LLM."""

    content: str | None
    reasoning: str | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int] | None
    finish_reason: str
    duration_ms: int


@dataclass(frozen=True)
class ToolExecutionRecord:
    """Record of a single tool execution within a turn."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]
    result_success: bool
    result_data: Any
    result_error: str | None
    duration_ms: int


@dataclass(frozen=True)
class TurnRecord:
    """Record of one complete think/act/observe turn."""

    turn_index: int
    timestamp: float
    request: LLMRequestRecord
    response: LLMResponseRecord
    tool_executions: tuple[ToolExecutionRecord, ...]
    trace_id: str = ""


@dataclass(frozen=True)
class RunManifest:
    """Manifest for an entire agent run, written after completion."""

    run_id: str
    agent_name: str
    started_at: float
    completed_at: float | None
    total_turns: int
    final_status: str
    final_termination_reason: str | None
    total_usage: dict[str, int]
    log_dir: str
    trace_id: str = ""


class AuditLogger:
    """Incremental audit logger that writes one subdirectory per turn.

    Usage::

        logger = AuditLogger.for_run(agent_name="OrchestratorAgent")
        logger.write_turn(turn_record)
        logger.finalize(final_status="completed")
    """

    def __init__(
        self,
        log_dir: Path | str,
        agent_name: str,
        run_id: str | None = None,
    ) -> None:
        self._log_dir = Path(log_dir) if isinstance(log_dir, str) else log_dir
        self._agent_name = agent_name
        self._run_id = run_id or f"{agent_name}_{uuid.uuid4().hex[:8]}"
        self._run_dir = self._log_dir / self._run_id
        self._turn_count = 0
        self._started_at = time.time()
        self._total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self._finalized = False
        self._lock = threading.Lock()

        self._run_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(
        cls,
        agent_name: str,
        base_dir: str = ".agent_logs",
        run_id: str | None = None,
    ) -> AuditLogger:
        """Factory: create logger for a new run."""
        return cls(
            log_dir=Path(base_dir),
            agent_name=agent_name,
            run_id=run_id,
        )

    def write_turn(self, turn: TurnRecord) -> None:
        """Persist a single turn to disk under turn_{n:03d}/."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("Cannot write turn after logger is finalized")

            turn_dir = self._run_dir / f"turn_{turn.turn_index:03d}"
            turn_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Write request
                (turn_dir / "llm_request.json").write_text(
                    json.dumps(
                        self._serialize(turn.request),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                # Write response
                (turn_dir / "llm_response.json").write_text(
                    json.dumps(
                        self._serialize(turn.response),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                # Write tool executions
                (turn_dir / "tool_executions.json").write_text(
                    json.dumps(
                        self._serialize(turn.tool_executions),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                logger.exception("Failed to write audit turn %s", turn.turn_index)
                raise

            self._turn_count += 1

            # Accumulate usage
            if turn.response.usage:
                for key in self._total_usage:
                    self._total_usage[key] += turn.response.usage.get(key, 0)

    def finalize(
        self,
        final_status: str,
        termination_reason: str | None = None,
    ) -> None:
        """Write run manifest.  Idempotent."""
        with self._lock:
            if self._finalized:
                return

            # Capture current OTel trace_id for cross-referencing
            try:
                from opentelemetry import trace as otel_trace
                current_span = otel_trace.get_current_span()
                span_ctx = current_span.get_span_context() if current_span else None
                trace_id = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
            except Exception:
                trace_id = ""  # OTel not configured in this context

            manifest = RunManifest(
                run_id=self._run_id,
                agent_name=self._agent_name,
                started_at=self._started_at,
                completed_at=time.time(),
                total_turns=self._turn_count,
                final_status=final_status,
                final_termination_reason=termination_reason,
                total_usage=dict(self._total_usage),
                log_dir=str(self._run_dir),
                trace_id=trace_id,
            )
            path = self._run_dir / "run.json"
            try:
                path.write_text(
                    json.dumps(
                        self._serialize(manifest),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self._finalized = True
            except Exception:
                logger.exception("Failed to write audit manifest")
                raise

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @staticmethod
    def _serialize(obj: Any, _seen: set[int] | None = None) -> Any:
        """Serialize dataclass/tuple to plain dict/list, with fallback for non-JSON types."""
        if _seen is None:
            _seen = set()

        obj_id = id(obj)
        if obj_id in _seen:
            return "<circular_reference>"

        # Primitives: return as-is (no need to track in seen)
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj

        _seen.add(obj_id)
        try:
            if hasattr(obj, "__dataclass_fields__"):
                return {
                    k: AuditLogger._serialize(v, _seen)
                    for k, v in asdict(obj).items()  # type: ignore[arg-type]
                }
            if isinstance(obj, tuple):
                return [AuditLogger._serialize(v, _seen) for v in obj]
            if isinstance(obj, list):
                return [AuditLogger._serialize(v, _seen) for v in obj]
            if isinstance(obj, dict):
                return {k: AuditLogger._serialize(v, _seen) for k, v in obj.items()}
            if isinstance(obj, bytes):
                return f"<bytes:{len(obj)}>"
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, Decimal):
                return str(obj)
            if isinstance(obj, set):
                return list(obj)
            # Fallback for any other non-JSON type
            return f"<{type(obj).__name__}>"
        finally:
            _seen.discard(obj_id)
