# src/agent/telemetry/metrics.py
"""Prometheus metrics definitions for Courtier.

All metrics follow Prometheus naming conventions and are registered
at module import time.  Helper functions provide typed recording.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Counters ──────────────────────────────────────────────

AGENT_REQUESTS_TOTAL = Counter(
    "agent_requests_total",
    "Total number of agent invocations",
    ["agent_name", "status"],
)

LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "Total number of LLM API calls",
    ["model", "agent_name"],
)

LLM_TOKEN_USAGE_TOTAL = Counter(
    "llm_token_usage_total",
    "Total tokens consumed by LLM calls",
    ["model", "direction"],  # direction: "input" or "output"
    unit="tokens",
)

TOOL_EXECUTIONS_TOTAL = Counter(
    "tool_executions_total",
    "Total number of tool executions",
    ["tool_name", "status"],
)

SUBAGENT_DISPATCH_TOTAL = Counter(
    "subagent_dispatch_total",
    "Total number of sub-agent dispatches",
    ["subagent_name", "status"],
)

EVENT_BUS_DROPPED_TOTAL = Counter(
    "event_bus_dropped_total",
    "Events dropped due to backpressure",
    ["event_type", "strategy"],
)

MODEL_ROUTER_FALLBACK_TOTAL = Counter(
    "model_router_fallback_total",
    "Model backend fallback events",
    ["from_backend", "to_backend", "reason"],
)

STATE_MACHINE_INVALID_TOTAL = Counter(
    "state_machine_invalid_total",
    "Invalid state transitions",
    ["from_status", "to_status"],
)

PLUGIN_LIFECYCLE_RESTART_TOTAL = Counter(
    "plugin_lifecycle_restart_total",
    "Plugin restart attempts",
    ["provider", "outcome"],
)

GUARDRAIL_BLOCKED_TOTAL = Counter(
    "guardrail_blocked_total",
    "Guardrail block actions",
    ["layer", "guard_name"],
)

MODEL_TOOL_ARG_REPAIR_TOTAL = Counter(
    "model_tool_arg_repair_total",
    "Tool call argument repairs applied while parsing model output",
    ["tier"],  # tier: "ref_quote_fix", "json_repair", "parse_error", "xml_fallback"
)

MODEL_STREAM_TOOL_CALLS_LOST_TOTAL = Counter(
    "model_stream_tool_calls_lost_total",
    "Streaming responses with finish_reason=tool_calls but no tool call data "
    "(fell back to non-streaming)",
    ["model"],
)

CONTEXT_COMPACTION_TOTAL = Counter(
    "context_compaction_total",
    "Context compactions by type",
    ["type"],  # type: "full", "micro", "fallback", "failed"
)

CONTEXT_REF_RECOVER_TOTAL = Counter(
    "context_ref_recover_total",
    "$ref resolution attempts by outcome",
    ["result"],  # result: "hit", "miss"
)

# ── Histograms ────────────────────────────────────────────

AGENT_LATENCY_SECONDS = Histogram(
    "agent_latency_seconds",
    "End-to-end agent run latency",
    ["agent_name"],
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

TOOL_LATENCY_SECONDS = Histogram(
    "tool_latency_seconds",
    "Tool execution latency",
    ["tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

CONTEXT_COMPACTION_DURATION_SECONDS = Histogram(
    "context_compaction_duration_seconds",
    "Full compaction duration (including the summary LLM call)",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# ── Gauges ────────────────────────────────────────────────

PLUGIN_STATE = Gauge(
    "plugin_state_changes",
    "Current state of each plugin (1=active, 0=other)",
    ["plugin_name", "state"],
)

CAPABILITY_REGISTRY_SIZE = Gauge(
    "capability_registry_size",
    "Number of registered capabilities",
    ["capability_type"],
)

CONVERSATION_TREE_BRANCHES = Gauge(
    "conversation_tree_branches",
    "Number of leaf nodes in conversation tree",
    ["session_id"],
)

CONTEXT_TOKENS = Gauge(
    "context_tokens",
    "Estimated context size in tokens before each think phase",
    ["agent_name"],
)

# ── Recording helpers ─────────────────────────────────────


def record_agent_request(agent_name: str, status: str) -> None:
    AGENT_REQUESTS_TOTAL.labels(agent_name=agent_name, status=status).inc()


def record_agent_latency(agent_name: str, seconds: float) -> None:
    AGENT_LATENCY_SECONDS.labels(agent_name=agent_name).observe(seconds)


def record_llm_call(model: str, agent_name: str) -> None:
    LLM_CALLS_TOTAL.labels(model=model, agent_name=agent_name).inc()


def record_llm_tokens(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    LLM_TOKEN_USAGE_TOTAL.labels(model=model, direction="input").inc(input_tokens)
    LLM_TOKEN_USAGE_TOTAL.labels(model=model, direction="output").inc(output_tokens)


def record_tool_execution(tool_name: str, status: str) -> None:
    TOOL_EXECUTIONS_TOTAL.labels(tool_name=tool_name, status=status).inc()


def record_tool_latency(tool_name: str, seconds: float) -> None:
    TOOL_LATENCY_SECONDS.labels(tool_name=tool_name).observe(seconds)


def record_subagent_dispatch(subagent_name: str, status: str) -> None:
    SUBAGENT_DISPATCH_TOTAL.labels(subagent_name=subagent_name, status=status).inc()


def record_event_bus_dropped(event_type: str, strategy: str) -> None:
    EVENT_BUS_DROPPED_TOTAL.labels(event_type=event_type, strategy=strategy).inc()


def record_model_router_fallback(from_backend: str, to_backend: str, reason: str) -> None:
    MODEL_ROUTER_FALLBACK_TOTAL.labels(
        from_backend=from_backend, to_backend=to_backend, reason=reason
    ).inc()


def record_state_machine_invalid(from_status: str, to_status: str) -> None:
    STATE_MACHINE_INVALID_TOTAL.labels(from_status=from_status, to_status=to_status).inc()


def record_plugin_lifecycle_restart(provider: str, outcome: str) -> None:
    PLUGIN_LIFECYCLE_RESTART_TOTAL.labels(provider=provider, outcome=outcome).inc()


def record_guardrail_blocked(layer: str, guard_name: str) -> None:
    GUARDRAIL_BLOCKED_TOTAL.labels(layer=layer, guard_name=guard_name).inc()


def record_tool_arg_repair(tier: str) -> None:
    MODEL_TOOL_ARG_REPAIR_TOTAL.labels(tier=tier).inc()


def record_stream_tool_calls_lost(model: str) -> None:
    MODEL_STREAM_TOOL_CALLS_LOST_TOTAL.labels(model=model).inc()


def set_capability_registry_size(capability_type: str, size: int) -> None:
    CAPABILITY_REGISTRY_SIZE.labels(capability_type=capability_type).set(size)


def set_conversation_tree_branches(session_id: str, branches: int) -> None:
    CONVERSATION_TREE_BRANCHES.labels(session_id=session_id).set(branches)


def set_context_tokens(agent_name: str, tokens: int) -> None:
    CONTEXT_TOKENS.labels(agent_name=agent_name).set(tokens)


def record_context_compaction(compact_type: str) -> None:
    CONTEXT_COMPACTION_TOTAL.labels(type=compact_type).inc()


def record_context_compaction_duration(seconds: float) -> None:
    CONTEXT_COMPACTION_DURATION_SECONDS.observe(seconds)


def record_context_ref_recover(result: str) -> None:
    CONTEXT_REF_RECOVER_TOTAL.labels(result=result).inc()
