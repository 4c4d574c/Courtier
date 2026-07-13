# src/agent/telemetry/context.py
"""W3C TraceContext propagation utilities for cross-process tracing.

Orchestrator → plugin subprocess communication uses JSON-RPC over stdio,
not HTTP.  We serialize/deserialize the trace context into a plain dict
carrier that travels as a JSON-RPC params field.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


def inject_context(carrier: dict[str, str]) -> None:
    """Inject current trace context into *carrier* dict (mutates in place).

    Usage in orchestrator before JSON-RPC call::

        trace_carrier: dict[str, str] = {}
        inject_context(trace_carrier)
        params["trace_context"] = trace_carrier
    """
    _propagator.inject(carrier)


def extract_context(carrier: dict[str, Any]) -> Any:
    """Extract trace context from *carrier* dict.

    Returns an OpenTelemetry Context object that can be passed as
    ``context=`` to ``start_as_current_span()``.

    Usage in a plugin tool handler::

        trace_carrier = params.get("trace_context", {})
        parent_ctx = extract_context(trace_carrier)
        with tracer.start_as_current_span(..., context=parent_ctx):
            ...
    """
    return _propagator.extract(carrier)
