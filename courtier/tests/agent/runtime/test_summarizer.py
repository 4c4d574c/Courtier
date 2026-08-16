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


@pytest.mark.asyncio
async def test_excerpts_prefer_compact_fields_over_blob_duplicates():
    """correct_text-shaped results: errors[] compact fields must appear in
    key_excerpts instead of two near-identical full-text source/target
    blobs (the original behavior, which forced the model to re-read the
    whole result via get_artifact)."""
    from courtier.agent.runtime.summarizer import RuleBasedSummaryStrategy

    full_text = "密级▲长期\n\nX办请[2025】 签发人：\n\n关于申请追加经费的请示。" + "字" * 1200
    data = {
        "results": [
            {
                "source": full_text,
                "target": full_text[:-1] + "。",  # near-duplicate of source
                "errors": [
                    {
                        "operation": "replace",
                        "context": "X办请[2025】",
                        "replacement": "X办请〔2025〕",
                    },
                    {
                        "operation": "replace",
                        "context": "关建阶段",
                        "replacement": "关键阶段",
                    },
                    {
                        "operation": "replace",
                        "context": "效溢分析",
                        "replacement": "效益分析",
                    },
                ],
            }
        ]
    }
    strategy = RuleBasedSummaryStrategy()
    _summary, excerpts = await strategy.summarize(data, "correct_text")
    assert excerpts
    joined = "\n".join(excerpts)
    # Compact comparison fields surface first...
    assert "operation: replace" in joined
    assert "replacement" in joined
    # ...and the two giant blobs do not both take slots.
    blob_hits = [e for e in excerpts if "results[0].source" in e or "results[0].target" in e]
    assert len(blob_hits) <= 1
    # Blob values are truncated instead of dumping 500 chars of body text.
    for entry in blob_hits:
        assert entry.endswith("…")


@pytest.mark.asyncio
async def test_excerpts_short_content_still_first_class():
    """Plain small results keep their informative strings (no regression)."""
    from courtier.agent.runtime.summarizer import RuleBasedSummaryStrategy

    data = {"title": "安全生产法", "summary_text": "第一条 为了加强安全生产工作……"}
    strategy = RuleBasedSummaryStrategy()
    _summary, excerpts = await strategy.summarize(data, "search_documents")
    joined = "\n".join(excerpts)
    assert "安全生产法" in joined
    assert "第一条" in joined
