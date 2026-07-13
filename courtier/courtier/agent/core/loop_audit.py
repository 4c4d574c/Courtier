"""Audit logging helpers for agent_loop."""

from __future__ import annotations

from .audit_logger import (
    AuditLogger,
    LLMRequestRecord,
    LLMResponseRecord,
    ToolExecutionRecord,
    TurnRecord,
)


def write_audit_turn(
    audit_logger: AuditLogger,
    turn_index: int,
    timestamp: float,
    llm_request: LLMRequestRecord,
    llm_response: LLMResponseRecord,
    tool_executions: tuple[ToolExecutionRecord, ...] = (),
) -> None:
    """Helper to create and write a TurnRecord to the audit logger."""
    # Capture current OTel trace_id for cross-referencing with Langfuse traces
    try:
        from opentelemetry import trace as otel_trace
        current_span = otel_trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        trace_id = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
    except Exception:
        trace_id = ""

    turn = TurnRecord(
        turn_index=turn_index,
        timestamp=timestamp,
        request=llm_request,
        response=llm_response,
        tool_executions=tool_executions,
        trace_id=trace_id,
    )
    audit_logger.write_turn(turn)
