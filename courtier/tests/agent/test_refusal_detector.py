"""Refusal detector + settings (refusal plan T1)."""

from courtier.agent.core.refusal import RefusalDetector
from courtier.config import build_settings_meta, get_settings

DETECTOR = RefusalDetector(["我无法", "I cannot", "I'm sorry, but"])


class TestRefusalDetector:
    def test_chinese_match_returns_original_fragment(self):
        assert DETECTOR.match("很抱歉，我无法协助完成该操作。") == "我无法"

    def test_english_match_case_insensitive(self):
        assert DETECTOR.match("Sorry — I CANNOT do that.") == "I CANNOT"

    def test_no_match_returns_none(self):
        assert DETECTOR.match("好的，文件已删除。") is None

    def test_none_and_empty_are_none(self):
        assert DETECTOR.match(None) is None
        assert DETECTOR.match("") is None

    def test_blank_patterns_ignored(self):
        detector = RefusalDetector(["", "  ", "我无法"])
        assert detector.match("我无法") == "我无法"


class TestRefusalSettings:
    def test_defaults(self):
        settings = get_settings()
        assert settings.refusal_detection_enabled is True
        assert settings.refusal_retry_max == 1
        assert any("我无法" in p for p in settings.refusal_patterns)

    def test_meta_category_guards_not_secret(self):
        meta = build_settings_meta()
        for key in ("refusal_detection_enabled", "refusal_patterns", "refusal_retry_max"):
            assert meta[key].category == "guards", key
            assert meta[key].is_secret is False, key
