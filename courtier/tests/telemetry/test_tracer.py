# tests/telemetry/test_tracer.py
"""Tests for AgentTracer span context managers."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from courtier.agent.telemetry.tracer import AgentTracer, get_tracer, init_telemetry


@pytest.fixture(autouse=True)
def setup_telemetry():
    """Fresh TracerProvider per test."""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)


class TestInitTelemetry:
    def test_init_idempotent(self):
        """Calling init_telemetry twice should not raise."""
        init_telemetry()
        init_telemetry()

    def test_get_tracer_initializes_if_needed(self):
        """get_tracer() calls init_telemetry() lazily."""
        t = get_tracer("test-tracer")
        assert t is not None

    def test_provider_reads_settings_snapshot(self, monkeypatch):
        """OTel knobs come from the ConfigService snapshot, not env reads."""
        from types import SimpleNamespace

        import courtier.agent.telemetry.tracer as tracer_mod

        monkeypatch.setattr(tracer_mod, "_provider", None)
        fake = SimpleNamespace(
            otel_service_name="courtier-test",
            deployment_env="development",
            otel_exporter_otlp_endpoint="http://collector:4317",
            otel_log_level="INFO",
        )
        monkeypatch.setattr(
            "courtier.config.get_settings", lambda: fake, raising=True
        )

        tracer_mod.init_telemetry()

        provider = tracer_mod._provider
        assert provider is not None
        attrs = provider.resource.attributes
        assert attrs["service.name"] == "courtier-test"
        assert attrs["deployment.environment"] == "development"


class TestAgentSpan:
    def test_agent_span_success(self):
        tracer = AgentTracer("test")
        with tracer.agent_span("test-agent", session_id="s1"):
            pass

    def test_agent_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(ValueError, match="test error"):
            with tracer.agent_span("test-agent"):
                raise ValueError("test error")

    def test_agent_span_sets_attributes(self):
        tracer = AgentTracer("test")
        with tracer.agent_span(
            "test-agent",
            session_id="abc123",
            custom_attr="value",
        ):
            pass


class TestLLMSpan:
    def test_llm_span_success(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o", temperature=0.3):
            pass

    def test_llm_span_token_usage(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o") as span:
            tracer.set_token_usage(span, 100, 50)

    def test_llm_span_prompt_completion(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o") as span:
            tracer.log_prompt(span, "Hello", role="user")
            tracer.log_completion(span, "Hi there!", finish_reason="stop")


class TestToolSpan:
    def test_tool_span_success(self):
        tracer = AgentTracer("test")
        with tracer.tool_span("search", {"query": "test"}):
            pass

    def test_tool_span_sets_result(self):
        tracer = AgentTracer("test")
        with tracer.tool_span("search") as span:
            tracer.set_tool_result(span, "found 5 results")

    def test_tool_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(RuntimeError, match="tool failed"):
            with tracer.tool_span("search"):
                raise RuntimeError("tool failed")


class TestPluginSpan:
    def test_plugin_span_success(self):
        tracer = AgentTracer("test")
        with tracer.plugin_span("format_audit", task="check formatting"):
            pass

    def test_plugin_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(ConnectionError, match="disconnected"):
            with tracer.plugin_span("format_audit"):
                raise ConnectionError("disconnected")
