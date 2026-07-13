# tests/telemetry/test_cross_process.py
"""Integration tests for cross-process trace context propagation."""

import pytest
from opentelemetry import trace, context
from opentelemetry.sdk.trace import TracerProvider

from courtier.agent.telemetry.context import inject_context, extract_context
from courtier.agent.telemetry.tracer import AgentTracer, init_telemetry


@pytest.fixture(autouse=True)
def setup_telemetry():
    """Fresh TracerProvider per test."""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)


class TestCrossProcessTracePropagation:
    """Simulates orchestrator → plugin trace context propagation."""

    def test_inject_extract_creates_child_spans(self):
        """A child span created with extracted context should be linked to parent."""
        tracer = AgentTracer("test")
        carrier: dict[str, str] = {}
        parent_trace_id = None

        # Orchestrator side: create span and inject context
        with tracer.agent_span("orchestrator") as parent_span:
            parent_trace_id = format(
                parent_span.get_span_context().trace_id, "032x"
            )
            inject_context(carrier)

        assert "traceparent" in carrier
        assert parent_trace_id is not None

        # Plugin side: extract context and create dispatch_subagent span
        parent_ctx = extract_context(carrier)
        assert parent_ctx is not None

        token = context.attach(parent_ctx)
        try:
            with tracer.plugin_span("format_audit", task="check"):
                child_span = trace.get_current_span()
                child_ctx = child_span.get_span_context()
                child_trace_id = format(child_ctx.trace_id, "032x")
                assert child_trace_id == parent_trace_id, (
                    "Child span should share the same trace_id as parent"
                )
        finally:
            context.detach(token)

    def test_empty_carrier_does_not_break_span_creation(self):
        """When no trace_context is sent, the plugin still creates spans normally."""
        tracer = AgentTracer("test")
        # No parent context attached
        with tracer.plugin_span("format_audit", task="check") as span:
            span_ctx = span.get_span_context()
            assert span_ctx.is_valid
            # Without parent, this is a root span with its own trace_id
            trace_id = format(span_ctx.trace_id, "032x")
            assert len(trace_id) == 32

    def test_nested_carrier_propagation(self):
        """Context can be injected multiple times (orchestrator → worker chain)."""
        tracer = AgentTracer("test")
        carrier1: dict[str, str] = {}
        carrier2: dict[str, str] = {}
        root_trace_id = None

        # Layer 1: root span
        with tracer.agent_span("orchestrator") as root_span:
            root_trace_id = format(
                root_span.get_span_context().trace_id, "032x"
            )
            inject_context(carrier1)

        # Layer 2: child span (simulating sub-agent)
        ctx1 = extract_context(carrier1)
        token1 = context.attach(ctx1)
        try:
            with tracer.plugin_span("worker_a") as mid_span:
                inject_context(carrier2)
                mid_trace_id = format(
                    mid_span.get_span_context().trace_id, "032x"
                )
                assert mid_trace_id == root_trace_id
        finally:
            context.detach(token1)

        # Layer 3: grandchild span (simulating nested sub-agent)
        ctx2 = extract_context(carrier2)
        token2 = context.attach(ctx2)
        try:
            with tracer.plugin_span("worker_b") as leaf_span:
                leaf_trace_id = format(
                    leaf_span.get_span_context().trace_id, "032x"
                )
                assert leaf_trace_id == root_trace_id, (
                    "All spans in the chain should share the root trace_id"
                )
        finally:
            context.detach(token2)

    def test_trace_context_survives_serialization_roundtrip(self):
        """The carrier dict should survive JSON serialize/deserialize."""
        import json
        tracer = AgentTracer("test")
        carrier: dict[str, str] = {}

        with tracer.agent_span("orchestrator"):
            inject_context(carrier)

        # Simulate JSON-RPC boundary: serialize to JSON and back
        serialized = json.dumps(carrier)
        deserialized = json.loads(serialized)

        # Verify the deserialized carrier still produces valid context
        extracted = extract_context(deserialized)
        assert extracted is not None

        token = context.attach(extracted)
        try:
            with tracer.plugin_span("receiver") as span:
                span_ctx = span.get_span_context()
                assert span_ctx.is_valid
        finally:
            context.detach(token)
