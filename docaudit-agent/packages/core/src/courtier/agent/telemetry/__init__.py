# src/agent/telemetry/__init__.py
"""OpenTelemetry-based observability for Courtier.

Provides distributed tracing (AgentTracer), Prometheus metrics,
and W3C TraceContext propagation for cross-process (plugin) tracing.
"""

from .tracer import AgentTracer, init_telemetry, get_tracer
from .context import inject_context, extract_context
from .decorators import traced_agent, traced_llm, traced_tool
from .metrics import (
    record_agent_request,
    record_agent_latency,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
    record_subagent_dispatch,
)

__all__ = [
    "AgentTracer",
    "init_telemetry",
    "get_tracer",
    "inject_context",
    "extract_context",
    "traced_agent",
    "traced_llm",
    "traced_tool",
    "record_agent_request",
    "record_agent_latency",
    "record_llm_call",
    "record_llm_tokens",
    "record_tool_execution",
    "record_tool_latency",
    "record_subagent_dispatch",
]
