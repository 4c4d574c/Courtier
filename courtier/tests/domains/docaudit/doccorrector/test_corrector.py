"""doccorrector 规则层单测 — 引号开闭配对与数字内小数点豁免。

对应实施计划 Phase 0 / Task 0.1（docs/superpowers/plans/2026-08-16-...）。
只测纯函数 _detect_punctuation_mixing，不构造 ErrorCorrect、不发起网络。
"""

from doccorrector.corrector import _detect_punctuation_mixing


def _double_quotes(text: str) -> list[dict]:
    return [e for e in _detect_punctuation_mixing(text) if e["original"] == '"']


def _single_quotes(text: str) -> list[dict]:
    return [e for e in _detect_punctuation_mixing(text) if e["original"] == "'"]


def test_double_quote_pairing_alternates_open_close():
    text = '"加快建设数字中国"和"一网通办"'
    quotes = _double_quotes(text)
    assert [e["corrected"] for e in quotes] == ["“", "”", "“", "”"]
    # 位置必须落在原文每个半角引号处，且顺序一致
    assert [e["position"] for e in quotes] == [i for i, ch in enumerate(text) if ch == '"']


def test_single_quote_pairing_alternates_open_close():
    text = "'单引号'"
    quotes = _single_quotes(text)
    assert [e["corrected"] for e in quotes] == ["‘", "’"]


def test_quote_state_resets_between_calls():
    first = _double_quotes('"甲"')
    second = _double_quotes('"乙"')
    assert [e["corrected"] for e in first] == ["“", "”"]
    assert [e["corrected"] for e in second] == ["“", "”"]


def test_decimal_point_between_digits_exempt():
    text = "本项目总投资估算为人民币5.8亿元。"
    errors = _detect_punctuation_mixing(text)
    assert not any(e["original"] == "." for e in errors)


def test_multiple_decimals_all_exempt():
    text = "投资5.8亿元，申请3.5亿元。"
    assert _detect_punctuation_mixing(text) == []


def test_sentence_final_halfwidth_period_still_flagged():
    text = "会议结束."
    assert _detect_punctuation_mixing(text) == [
        {
            "original": ".",
            "corrected": "。",
            "position": 4,
            "operation": "replace",
            "context": "议结束.",
        }
    ]


def test_fullwidth_digits_with_halfwidth_dot_still_flagged():
    # 全角数字中的半角小数点不是合法数值写法（数字应整体半角），仍报标点混用
    text = "５.８亿元"
    dots = [e for e in _detect_punctuation_mixing(text) if e["original"] == "."]
    assert len(dots) == 1
    assert dots[0]["corrected"] == "。"
