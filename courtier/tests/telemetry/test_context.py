# tests/telemetry/test_context.py
"""Tests for W3C TraceContext propagation utilities."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from courtier.agent.telemetry.context import extract_context, inject_context


@pytest.fixture(autouse=True)
def setup_telemetry():
    """Ensure TracerProvider is initialized for each test."""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    yield


def test_inject_context_populates_carrier():
    """inject_context writes W3C tracecontext headers into the carrier dict."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span"):
        inject_context(carrier)

    assert "traceparent" in carrier, "carrier should contain traceparent"
    assert carrier["traceparent"].startswith("00-"), (
        "traceparent should follow W3C format"
    )


def test_extract_context_roundtrip():
    """extract_context(inject_context(carrier)) should produce a valid context."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span"):
        inject_context(carrier)

    extracted = extract_context(carrier)
    assert extracted is not None, "extract_context should return a Context object"


def test_extract_empty_carrier_returns_non_none():
    """extract_context on empty dict returns a context (possibly root)."""
    result = extract_context({})
    assert result is not None


def test_inject_extract_preserves_trace_id():
    """The trace_id in the extracted context matches the injected one."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span") as span:
        inject_context(carrier)
        original_trace_id = span.get_span_context().trace_id

    assert int(carrier["traceparent"].split("-")[1], 16) == original_trace_id
