"""Tests for the ResNet font-model client (scanned pipeline)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from docparse.parsers.scanned.font_model import (
    _MAX_BATCH,
    FontModelClient,
    _map_label,
)
from PIL import Image as PILImage


def _word(text: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _line(line_no: int, chars: list[dict[str, Any]]) -> dict[str, Any]:
    return {"line_no": line_no, "text": "x", "chars": chars}


def _result(
    font: str,
    conf: float = 1.0,
    margin: float = 1.0,
    weight: str = "常规",
    slant: str = "常规",
) -> dict[str, Any]:
    return {
        "success": True,
        "result": {
            "font": font,
            "font_confidence": conf,
            "font_top2_margin": margin,
            "font_raw_pred": font,
            "weight": weight,
            "weight_confidence": 1.0,
            "slant": slant,
            "slant_confidence": 1.0,
        },
        "error": None,
    }


def _mock_post(monkeypatch: pytest.MonkeyPatch, specs: list[dict[str, Any]]) -> list[int]:
    """Patch requests.post; returns a list recording each call's image count.

    ``specs`` supplies one response item per char crop, in crop order.
    """
    call_sizes: list[int] = []
    cursor = {"pos": 0}

    def fake_post(url: str, files: list, timeout: float, **kwargs: Any):
        assert url.endswith("/predict_batch")
        n = len(files)
        assert n <= _MAX_BATCH
        call_sizes.append(n)
        data = specs[cursor["pos"] : cursor["pos"] + n]
        assert len(data) == n, "test spec must cover every crop"
        cursor["pos"] += n
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "success": True,
                "data": data,
                "summary": {"total": n, "success": n, "failed": 0},
            },
        )

    monkeypatch.setattr(
        "docparse.parsers.scanned.font_model.requests.post",
        fake_post,
    )
    return call_sizes


@pytest.fixture()
def page_image(tmp_path) -> str:
    img = PILImage.new("L", (400, 400), 255)
    path = tmp_path / "page.png"
    img.save(path)
    return str(path)


class TestMapLabel:
    def test_standard_labels(self):
        assert _map_label("仿宋") == "仿宋"
        assert _map_label("小标宋体") == "小标宋"
        assert _map_label("楷体") == "楷体"

    def test_ignored_labels(self):
        assert _map_label("其他") == ""
        assert _map_label("") == ""

    def test_unknown_ti_suffix_stripped(self):
        assert _map_label("黑体") == "黑体"


class TestCharBoxes:
    def test_fullwidth_word_split_evenly(self):
        client = FontModelClient("http://x")
        boxes = client._char_boxes(_line(0, [_word("abcd", 0, 0, 40, 10)]))
        # non-ASCII only; pure ASCII is skipped
        assert boxes == []

    def test_cjk_word_split(self):
        client = FontModelClient("http://x")
        boxes = client._char_boxes(_line(0, [_word("仿宋体", 0, 0, 30, 10)]))
        assert boxes == [(0, 0, 10, 10), (10, 0, 20, 10), (20, 0, 30, 10)]

    def test_mixed_words_skip_ascii(self):
        client = FontModelClient("http://x")
        boxes = client._char_boxes(
            _line(0, [_word("AB", 0, 0, 20, 10), _word("仿宋", 20, 0, 40, 10)])
        )
        assert boxes == [(20, 0, 30, 10), (30, 0, 40, 10)]


class TestRecognizeLines:
    def test_majority_vote_recognizes_line(self, monkeypatch, page_image):
        specs = [_result("仿宋")] * 3
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, [_word("仿宋字", 10, 10, 70, 30)])]
        )
        assert font_info == {0: {"font_family": "仿宋", "font_weight": False, "font_style": False}}
        assert unrecognized == []

    def test_too_few_qualified_chars_unrecognized(self, monkeypatch, page_image):
        specs = [_result("仿宋")] * 2
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, [_word("仿宋", 10, 10, 50, 30)])]
        )
        assert font_info == {}
        assert unrecognized == [0]

    def test_low_confidence_chars_excluded(self, monkeypatch, page_image):
        specs = [
            _result("黑体"),
            _result("黑体"),
            _result("黑体"),
            _result("仿宋", conf=0.3),  # below threshold — excluded
        ]
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, [_word("黑体标题", 10, 10, 90, 30)])]
        )
        assert font_info[0]["font_family"] == "黑体"
        assert unrecognized == []

    def test_ignored_label_not_counted(self, monkeypatch, page_image):
        specs = [_result("其他")] * 3
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, [_word("未知字", 10, 10, 70, 30)])]
        )
        assert font_info == {}
        assert unrecognized == [0]

    def test_bold_and_italic_votes(self, monkeypatch, page_image):
        specs = [
            _result("仿宋", weight="粗体"),
            _result("仿宋", weight="粗体"),
            _result("仿宋", slant="斜体"),
        ]
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        font_info, _ = client.recognize_lines(
            page_image, [_line(0, [_word("仿宋字", 10, 10, 70, 30)])]
        )
        assert font_info[0]["font_weight"] is True
        assert font_info[0]["font_style"] is False  # 1/3 is not a majority

    def test_batching_over_max_batch(self, monkeypatch, page_image):
        n = _MAX_BATCH + 6
        specs = [_result("仿宋")] * n
        call_sizes = _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x", min_qualified_chars=3)
        text = "字" * n
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, [_word(text, 0, 0, 400, 30)])]
        )
        assert call_sizes == [_MAX_BATCH, 6]
        assert font_info[0]["font_family"] == "仿宋"
        assert unrecognized == []

    def test_mixed_lines_split_recognized_and_fallback(self, monkeypatch, page_image):
        # line 0: 3 qualified 楷体 chars; line 1: only 2 chars
        specs = [_result("楷体")] * 3 + [_result("仿宋")] * 2
        _mock_post(monkeypatch, specs)
        client = FontModelClient("http://x")
        lines = [
            _line(0, [_word("楷体字", 10, 10, 70, 30)]),
            _line(1, [_word("仿宋", 10, 40, 50, 60)]),
        ]
        font_info, unrecognized = client.recognize_lines(page_image, lines)
        assert font_info[0]["font_family"] == "楷体"
        assert unrecognized == [1]

    def test_no_chars_means_no_http_call(self, monkeypatch, page_image):
        calls = _mock_post(monkeypatch, [])
        client = FontModelClient("http://x")
        font_info, unrecognized = client.recognize_lines(
            page_image, [_line(0, []), _line(1, None and [] or [])]
        )
        assert calls == []
        assert font_info == {}
        assert unrecognized == [0, 1]
