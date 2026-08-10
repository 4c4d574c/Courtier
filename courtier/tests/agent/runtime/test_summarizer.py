"""Tests for ResultSummarizer."""

import pytest

from courtier.agent.core.cache_store import CacheStore
from courtier.agent.runtime.result import ExecutionResult
from courtier.agent.runtime.summarizer import ResultSummarizer


@pytest.fixture
def summarizer(tmp_path):
    cache = CacheStore(cache_dir=str(tmp_path))
    return ResultSummarizer(
        artifact_store=cache,
        raw_inline_max_chars=20,
        summary_inline_max_chars=100,
        max_key_excerpts=3,
    )


@pytest.mark.asyncio
async def test_small_result_returns_raw_data(summarizer):
    result = await summarizer.from_data(
        success=True,
        actor_type="tool",
        actor_name="echo",
        data="hello",
    )
    assert isinstance(result, ExecutionResult)
    assert result.success is True
    assert result.raw_data == "hello"
    assert result.result_id is None


@pytest.mark.asyncio
async def test_medium_result_returns_summary_and_excerpts(summarizer):
    """Above the inline limit the raw_data is dropped, but a recoverable $ref
    must be attached (no more 1500–6000 "death zone" without a result_id)."""
    data = "line1\nline2\nline3\nline4\nline5"
    result = await summarizer.from_data(
        success=True,
        actor_type="tool",
        actor_name="echo",
        data=data,
    )
    assert result.raw_data is None
    assert result.summary is not None
    assert len(result.key_excerpts) > 0
    # Dropped raw_data must always carry a recoverable result_id.
    assert result.result_id is not None
    assert result.metadata["stored"]["result_id"] == result.result_id


@pytest.mark.asyncio
async def test_medium_result_is_recoverable_from_store(summarizer):
    """The $ref attached to a summarised tool result resolves back to the
    full payload via the artifact store."""
    data = {"text": "x" * 50}  # > raw_inline_max_chars(20), < old death zone cap
    result = await summarizer.from_data(
        success=True,
        actor_type="tool",
        actor_name="echo",
        data=data,
    )
    assert result.raw_data is None
    assert result.result_id is not None
    assert summarizer.artifact_store.load(result.result_id) == data


@pytest.mark.asyncio
async def test_large_result_is_persisted(summarizer):
    data = "word " * 50  # > 100 chars, > summarizer.summary_inline_max_chars
    result = await summarizer.from_data(
        success=True,
        actor_type="tool",
        actor_name="echo",
        data=data,
    )
    assert result.raw_data is None
    assert result.result_id is not None
    assert result.summary is not None


@pytest.mark.asyncio
async def test_dict_result_summary(summarizer):
    data = {"issues": [{"id": 1}, {"id": 2}], "status": "ok"}
    result = await summarizer.from_data(
        success=True,
        actor_type="agent",
        actor_name="audit",
        data=data,
    )
    assert result.summary is not None
    assert "issues" in result.summary or "对象" in result.summary


@pytest.mark.asyncio
async def test_error_result(summarizer):
    result = await summarizer.from_data(
        success=False,
        actor_type="tool",
        actor_name="parse",
        data=None,
        error="boom",
    )
    assert result.success is False
    assert result.error == "boom"
