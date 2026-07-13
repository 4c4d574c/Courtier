"""Tests for ExecutionResult."""

from courtier.agent.runtime.result import ExecutionResult


def test_execution_result_inline_raw():
    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="echo",
        raw_data="hello",
        summary="echo returned hello",
    )
    assert result.is_persisted() is False
    obs = result.to_observation_dict()
    assert obs["success"] is True
    assert obs["raw_data"] == "hello"


def test_execution_result_persisted():
    result = ExecutionResult(
        success=True,
        actor_type="agent",
        actor_name="format_audit",
        result_id="fmt-123",
        summary="format audit completed",
        key_excerpts=("issue 1", "issue 2"),
    )
    assert result.is_persisted() is True
    obs = result.to_observation_dict()
    assert obs["result_id"] == "fmt-123"
    assert obs["key_excerpts"] == ["issue 1", "issue 2"]


def test_execution_result_from_error():
    result = ExecutionResult.from_error(
        actor_type="tool",
        actor_name="parse",
        error="file not found",
    )
    assert result.success is False
    assert result.error == "file not found"
    assert result.raw_data is None
