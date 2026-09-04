# src/agent/telemetry/__init__.py
"""OpenTelemetry-based observability for Courtier.

Provides distributed tracing (AgentTracer), Prometheus metrics,
and W3C TraceContext propagation for cross-process (plugin) tracing.
"""

from .context import extract_context, inject_context
from .metrics import (
    record_agent_latency,
    record_agent_request,
    record_llm_call,
    record_llm_tokens,
    record_subagent_dispatch,
    record_tool_execution,
    record_tool_latency,
)
from .tracer import AgentTracer, get_tracer, init_telemetry

__all__ = [
    "AgentTracer",
    "init_telemetry",
    "get_tracer",
    "inject_context",
    "extract_context",
    "record_agent_request",
    "record_agent_latency",
    "record_llm_call",
    "record_llm_tokens",
    "record_tool_execution",
    "record_tool_latency",
    "record_subagent_dispatch",
]
