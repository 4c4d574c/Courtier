"""Tests for the LLM client: retry policy, response parsing, timeout config."""

import base64
import io
from types import SimpleNamespace

import httpx
import openai
import pytest
import requests
from docparse.parsers._retry import _call_with_retry, _is_retryable
from docparse.parsers.base import ParserConfig
from docparse.parsers.llm_client import (
    _LLM_TIMEOUT,
    LLMClient,
    _parse_response,
)
from PIL import Image as PILImage
from PIL import ImageDraw


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

    def test_file_not_found_not_retryable(self):
        assert _is_retryable(FileNotFoundError("missing")) is False

    def test_requests_error_retryable(self):
        assert _is_retryable(requests.ConnectionError("boom")) is True


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

        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 5

    def test_5xx_is_retried(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _status_error(500)

        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 5

    def test_network_error_is_retried(self):
        calls = 0

        def call():
            nonlocal calls
            calls += 1
            raise _connection_error()

        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            _call_with_retry(call, "test call", base_delay=0)
        assert calls == 5

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


class TestRecognizeFontsFromCrops:
    """Crop-based font recognition: ordering contract and label-font fallback."""

    @staticmethod
    def _make_page(path) -> str:
        """700x400 grayscale page: dark band on top, light band below."""
        img = PILImage.new("L", (700, 400), 255)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 700, 199], fill=30)  # dark band: rows 0-199
        draw.rectangle([0, 200, 700, 399], fill=220)  # light band: rows 200-399
        img.save(path)
        return str(path)

    @staticmethod
    def _make_client(payload: str) -> tuple[LLMClient, SimpleNamespace]:
        """LLMClient whose HTTP layer is replaced by a recording stub."""
        client = LLMClient(
            ParserConfig(
                llm_base_url="http://localhost:1/v1",
                llm_api_key="test-key",
                llm_model="test-model",
            )
        )

        def _create(**kwargs):
            stub.kwargs = kwargs
            message = SimpleNamespace(content=payload)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        stub = SimpleNamespace(create=_create, kwargs={})
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
        return client, stub

    @staticmethod
    def _extract_composite(stub: SimpleNamespace) -> PILImage.Image:
        url = stub.kwargs["messages"][1]["content"][0]["image_url"]["url"]
        data = base64.b64decode(url.split(",", 1)[1])
        return PILImage.open(io.BytesIO(data)).convert("L")

    @staticmethod
    def _mean(img: PILImage.Image) -> float:
        hist = img.histogram()
        return sum(i * count for i, count in enumerate(hist)) / (img.width * img.height)

    def test_crops_sorted_by_line_no_ascending(self, tmp_path):
        """crops 按行号升序拼接（与输入顺序无关），font_info 键为行号 int。"""
        page = self._make_page(tmp_path / "page.png")
        payload = (
            '{"font_info": {'
            '"2": {"font_family": "黑体", "font_weight": false, "font_style": false},'
            '"5": {"font_family": "仿宋", "font_weight": true, "font_style": false}'
            "}}"
        )
        client, stub = self._make_client(payload)

        # 输入乱序：行号 5（深色带区域）在前，行号 2（浅色带区域）在后
        lines = [
            {"line_no": 5, "text": "甲", "x0": 10, "y0": 10, "x1": 690, "y1": 180},
            {"line_no": 2, "text": "乙", "x0": 10, "y0": 220, "x1": 690, "y1": 390},
        ]
        font_info = client.recognize_fonts_from_crops(page, lines)

        assert set(font_info) == {2, 5}  # 键为行号 int
        assert font_info[5]["font_weight"] is True

        # 合成图上行号 2 的裁剪块（浅色带）必须在行号 5（深色带）之上
        composite = self._extract_composite(stub)
        w, h = composite.size
        top = composite.crop((0, 0, w, h // 2 - 1))
        bottom = composite.crop((0, h - h // 2 + 1, w, h))
        assert self._mean(top) > self._mean(bottom)

    def test_missing_label_font_skips_annotation(self, tmp_path, monkeypatch):
        """无候选标注字体时静默跳过行号标注（不中断、不报错）。"""
        monkeypatch.setattr("docparse.parsers.llm_client._LABEL_FONT_CANDIDATES", ())
        page = self._make_page(tmp_path / "page.png")
        payload = (
            '{"font_info": {'
            '"2": {"font_family": "仿宋", "font_weight": false, "font_style": false}'
            "}}"
        )
        client, _stub = self._make_client(payload)

        lines = [{"line_no": 2, "text": "乙", "x0": 10, "y0": 60, "x1": 190, "y1": 90}]
        font_info = client.recognize_fonts_from_crops(page, lines)

        assert set(font_info) == {2}
        assert font_info[2]["font_family"] == "仿宋"
