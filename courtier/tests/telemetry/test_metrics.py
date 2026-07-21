# tests/telemetry/test_metrics.py
"""Tests for Prometheus metrics definitions and recording helpers."""

from courtier.agent.telemetry.metrics import (
    AGENT_REQUESTS_TOTAL,
    LLM_CALLS_TOTAL,
    LLM_TOKEN_USAGE_TOTAL,
    MODEL_STREAM_TOOL_CALLS_LOST_TOTAL,
    MODEL_TOOL_ARG_REPAIR_TOTAL,
    SUBAGENT_DISPATCH_TOTAL,
    TOOL_EXECUTIONS_TOTAL,
    record_agent_latency,
    record_agent_request,
    record_llm_call,
    record_llm_tokens,
    record_stream_tool_calls_lost,
    record_subagent_dispatch,
    record_tool_arg_repair,
    record_tool_execution,
    record_tool_latency,
)


def _get_counter_value(counter, labels: dict[str, str]) -> float:
    """Safely get counter value, returning 0 if labels don't match.

    Prometheus client stores samples as {labels_tuple: value}.
    For a Counter with labels ["agent_name", "status"], the key
    would be ("test", "success").
    """
    label_names = counter._labelnames
    expected = tuple(labels.get(ln, "") for ln in label_names)
    for sample_labels, value in counter._metrics.items():
        if sample_labels == expected:
            return value._value.get()
    return 0.0


class TestAgentMetrics:
    def test_record_agent_request_increments(self):
        before = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "success"}
        )
        record_agent_request("test", "success")
        after = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "success"}
        )
        assert after == before + 1

    def test_record_agent_request_error(self):
        before = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "error"}
        )
        record_agent_request("test", "error")
        after = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "error"}
        )
        assert after == before + 1

    def test_record_agent_latency_observes(self):
        record_agent_latency("test", 1.5)
        record_agent_latency("test", 3.0)


class TestLLMMetrics:
    def test_record_llm_call_increments(self):
        before = _get_counter_value(
            LLM_CALLS_TOTAL, {"model": "gpt-4o", "agent_name": "orchestrator"}
        )
        record_llm_call("gpt-4o", "orchestrator")
        after = _get_counter_value(
            LLM_CALLS_TOTAL, {"model": "gpt-4o", "agent_name": "orchestrator"}
        )
        assert after == before + 1

    def test_record_llm_tokens_increments(self):
        before_in = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "input"}
        )
        before_out = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "output"}
        )
        record_llm_tokens("gpt-4o", 500, 200)
        after_in = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "input"}
        )
        after_out = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "output"}
        )
        assert after_in == before_in + 500
        assert after_out == before_out + 200


class TestToolMetrics:
    def test_record_tool_execution_increments(self):
        before = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "success"}
        )
        record_tool_execution("search", "success")
        after = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "success"}
        )
        assert after == before + 1

    def test_record_tool_execution_error(self):
        before = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "error"}
        )
        record_tool_execution("search", "error")
        after = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "error"}
        )
        assert after == before + 1

    def test_record_tool_latency(self):
        record_tool_latency("search", 0.25)
        record_tool_latency("search", 1.0)


class TestSubAgentMetrics:
    def test_record_subagent_dispatch_success(self):
        before = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "success"},
        )
        record_subagent_dispatch("format_audit", "success")
        after = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "success"},
        )
        assert after == before + 1

    def test_record_subagent_dispatch_error(self):
        before = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "error"},
        )
        record_subagent_dispatch("format_audit", "error")
        after = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "error"},
        )
        assert after == before + 1


class TestModelParsingMetrics:
    def test_record_tool_arg_repair_increments_per_tier(self):
        for tier in ("ref_quote_fix", "json_repair", "parse_error", "xml_fallback"):
            before = _get_counter_value(MODEL_TOOL_ARG_REPAIR_TOTAL, {"tier": tier})
            record_tool_arg_repair(tier)
            after = _get_counter_value(MODEL_TOOL_ARG_REPAIR_TOTAL, {"tier": tier})
            assert after == before + 1

    def test_record_stream_tool_calls_lost_increments(self):
        before = _get_counter_value(
            MODEL_STREAM_TOOL_CALLS_LOST_TOTAL, {"model": "qwen3-vllm"}
        )
        record_stream_tool_calls_lost("qwen3-vllm")
        after = _get_counter_value(
            MODEL_STREAM_TOOL_CALLS_LOST_TOTAL, {"model": "qwen3-vllm"}
        )
        assert after == before + 1
