# src/agent/telemetry/metrics.py
"""Prometheus metrics definitions for Courtier.

All metrics follow Prometheus naming conventions and are registered
at module import time.  Helper functions provide typed recording.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge

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

# ── Gauges ────────────────────────────────────────────────

PLUGIN_STATE = Gauge(
    "plugin_state_changes",
    "Current state of each plugin (1=active, 0=other)",
    ["plugin_name", "state"],
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
    SUBAGENT_DISPATCH_TOTAL.labels(
        subagent_name=subagent_name, status=status
    ).inc()
