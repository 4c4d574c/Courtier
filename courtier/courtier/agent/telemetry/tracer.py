# src/agent/telemetry/tracer.py
"""Framework-agnostic OpenTelemetry tracing for Courtier.

Provides AgentTracer with agent_span, llm_span, tool_span, and plugin_span
context managers.  All methods operate directly on opentelemetry.trace API
with no intermediate framework dependency.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

_provider: Optional[TracerProvider] = None


def init_telemetry() -> None:
    """Initialize the global TracerProvider.

    Idempotent — safe to call multiple times (e.g. in tests).
    Must be called once at application startup before any tracing occurs.

    Configuration comes from the shared Settings snapshot (ConfigService),
    not direct env reads, so the telemetry knobs are part of the unified
    configuration surface.
    """
    global _provider
    if _provider is not None:
        return

    from courtier.config import get_settings

    settings = get_settings()
    resource = Resource.create({
        "service.name": settings.otel_service_name,
        "service.version": "0.1.0",
        "deployment.environment": settings.deployment_env,
    })

    _provider = TracerProvider(resource=resource)

    otlp_endpoint = settings.otel_exporter_otlp_endpoint
    otlp_exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=not otlp_endpoint.startswith("https"),
        timeout=10,
    )
    _provider.add_span_processor(
        BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )

    if settings.otel_log_level.upper() == "DEBUG":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        _provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter())
        )

    trace.set_tracer_provider(_provider)


def get_tracer(name: str = "courtier") -> trace.Tracer:
    """Get a named tracer, initializing telemetry if needed."""
    if _provider is None:
        init_telemetry()
    return trace.get_tracer(name)


class AgentTracer:
    """Agent-span tracer wrapping OTel API with gen_ai semantic conventions."""

    def __init__(self, tracer_name: str = "courtier") -> None:
        self._tracer = get_tracer(tracer_name)

    # ── Agent Span ────────────────────────────────────────

    @contextmanager
    def agent_span(
        self,
        agent_name: str,
        session_id: str = "",
        **extra_attrs: Any,
    ) -> Generator[trace.Span, None, None]:
        """Create an invoke_agent span.  Yields the span for attribute setting."""
        with self._tracer.start_as_current_span(
            name=f"invoke_agent {agent_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", agent_name)
            span.set_attribute("agent.session_id", session_id)
            span.set_attribute("agent.start_time_iso", _now_iso())
            for k, v in extra_attrs.items():
                if v is not None:
                    span.set_attribute(f"agent.{k}", _safe_str(v))
            try:
                yield span
                span.set_attribute("agent.status", "success")
                span.set_attribute("agent.end_time_iso", _now_iso())
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("agent.status", "error")
                span.set_attribute("agent.error.type", type(e).__name__)
                span.set_attribute("agent.error.message", _safe_str(e, 500))
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise

    # ── LLM Span ──────────────────────────────────────────

    @contextmanager
    def llm_span(
        self,
        model: str = "unknown",
        temperature: float | None = None,
        system: str = "openai",
        **extra_attrs: Any,
    ) -> Generator[trace.Span, None, None]:
        """Create a chat span for an LLM call."""
        with self._tracer.start_as_current_span(
            name=f"chat {model}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.system", system)
            span.set_attribute("gen_ai.request.model", model)
            if temperature is not None:
                span.set_attribute("gen_ai.request.temperature", temperature)
            for k, v in extra_attrs.items():
                if v is not None:
                    span.set_attribute(f"gen_ai.request.{k}", _safe_str(v))
            try:
                yield span
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("gen_ai.response.finish_reasons", ["error"])
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise

    def set_token_usage(
        self,
        span: trace.Span,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Record token usage on an LLM span."""
        span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
        span.set_attribute(
            "gen_ai.usage.total_tokens",
            prompt_tokens + completion_tokens,
        )

    def log_prompt(self, span: trace.Span, content: str, role: str = "user") -> None:
        """Record prompt content as a span event."""
        span.add_event("gen_ai.content.prompt", {
            "role": role,
            "content": _safe_str(content, 4000),
        })

    def log_completion(
        self,
        span: trace.Span,
        content: str,
        finish_reason: str = "stop",
    ) -> None:
        """Record completion content as a span event."""
        span.add_event("gen_ai.content.completion", {
            "content": _safe_str(content, 4000),
            "finish_reason": finish_reason,
        })

    # ── Tool Span ─────────────────────────────────────────

    @contextmanager
    def tool_span(
        self,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
    ) -> Generator[trace.Span, None, None]:
        """Create an execute_tool span."""
        start = time.time()
        with self._tracer.start_as_current_span(
            name=f"execute_tool {tool_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", tool_name)
            if parameters:
                span.set_attribute(
                    "gen_ai.tool.parameters",
                    _safe_str(parameters, 1000),
                )
            try:
                yield span
            except Exception as e:
                span.set_attribute("gen_ai.tool.status", "error")
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, "tool execution failed"))
                raise
            finally:
                span.set_attribute("gen_ai.tool.duration_seconds", time.time() - start)

    def set_tool_result(
        self,
        span: trace.Span,
        result: Any,
    ) -> None:
        """Record tool result preview on the span."""
        preview = _safe_str(result, 500) if result is not None else ""
        span.set_attribute("gen_ai.tool.result_preview", preview)

    # ── Plugin / Sub-Agent Span ───────────────────────────

    @contextmanager
    def plugin_span(
        self,
        subagent_name: str,
        task: str = "",
    ) -> Generator[trace.Span, None, None]:
        """Create a dispatch_subagent span for cross-process sub-agent calls."""
        start = time.time()
        with self._tracer.start_as_current_span(
            name=f"dispatch_subagent {subagent_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "dispatch_subagent")
            span.set_attribute("subagent.name", subagent_name)
            if task:
                span.set_attribute("subagent.task", _safe_str(task, 500))
            try:
                yield span
                latency_ms = (time.time() - start) * 1000
                span.set_attribute("subagent.latency_ms", latency_ms)
                span.set_attribute("subagent.status", "success")
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("subagent.status", "error")
                span.set_attribute("subagent.error", _safe_str(e, 500))
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise


# ── Helpers ───────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(obj: Any, max_len: int = 1000) -> str:
    s = str(obj)
    return s[:max_len] if len(s) > max_len else s
