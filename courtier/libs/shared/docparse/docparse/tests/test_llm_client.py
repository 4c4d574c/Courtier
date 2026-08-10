"""Tests for the LLM client: retry policy, response parsing, timeout config."""

import httpx
import openai
import pytest
from docparse.parsers.base import ParserConfig
from docparse.parsers.llm_client import (
    _LLM_TIMEOUT,
    LLMClient,
    _call_with_retry,
    _is_retryable,
    _parse_response,
)


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    return openai.APIStatusError(
        f"HTTP {status}", response=httpx.Response(status, request=request), body=None
    )


def _connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    return openai.APIConnectionError(request=request)


class TestIsRetryable:
    """Retry classification for different failure types."""

    def test_400_not_retryable(self):
        assert _is_retryable(_status_error(400)) is False

    def test_401_not_retryable(self):
        assert _is_retryable(_status_error(401)) is False

    def test_404_not_retryable(self):
        assert _is_retryable(_status_error(404)) is False

    def test_422_not_retryable(self):
        assert _is_retryable(_status_error(422)) is False

    def test_429_retryable(self):
        assert _is_retryable(_status_error(429)) is True

    def test_500_retryable(self):
        assert _is_retryable(_status_error(500)) is True

    def test_503_retryable(self):
        assert _is_retryable(_status_error(503)) is True

    def test_network_error_retryable(self):
        assert _is_retryable(_connection_error()) is True

    def test_generic_error_retryable(self):
        assert _is_retryable(OSError("boom")) is True


class TestCallWithRetry:
    """Retry loop behavior: no retry on 4xx, backoff on transient errors."""

    def test_4xx_raises_immediately_without_retry(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _status_error(400)

        with pytest.raises(openai.APIStatusError):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 1

    def test_429_is_retried(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _status_error(429)

        with pytest.raises(RuntimeError, match="failed after 4 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 4

    def test_5xx_is_retried(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _status_error(500)

        with pytest.raises(RuntimeError, match="failed after 4 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 4

    def test_network_error_is_retried(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _connection_error()

        with pytest.raises(RuntimeError, match="failed after 4 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 4

    def test_retries_exhausted_raises_runtime_error(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _status_error(503)

        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            _call_with_retry(call, "test call", max_retries=2, base_delay=0)
        assert calls == 3

    def test_success_after_transient_failure(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _status_error(500)
            return "ok"

        assert _call_with_retry(call, "test call", base_delay=0) == "ok"
        assert calls == 2

    def test_4xx_does_not_mask_original_exception(self):
        def call():
            raise _status_error(422)

        with pytest.raises(openai.APIStatusError) as exc_info:
            _call_with_retry(call, "test call", base_delay=0)
        assert exc_info.value.status_code == 422


class TestParseResponse:
    """LLM response content parsing."""

    def test_non_json_content_raises_value_error(self):
        class _Message:
            content = "this is not JSON"

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_response(_Response())

    def test_empty_content_raises_value_error(self):
        class _Message:
            content = ""

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        with pytest.raises(ValueError, match="empty response"):
            _parse_response(_Response())


class TestClientTimeout:
    """The OpenAI client must have an explicit request timeout."""

    def test_timeout_configured(self):
        client = LLMClient(
            ParserConfig(
                llm_base_url="http://localhost:1/v1",
                llm_api_key="test-key",
                llm_model="test-model",
            )
        )
        assert client._client.timeout == _LLM_TIMEOUT
        assert _LLM_TIMEOUT > 0
