"""Tests for font family normalization in validator."""

import pytest

from validator.format_checker import normalize_font_family


class TestNormalizeFontFamily:
    """Test normalize_font_family converts English font names to Chinese standard names."""

    # English → Chinese mappings
    @pytest.mark.parametrize(
        "english, chinese",
        [
            ("FangSong", "仿宋"),
            ("SimHei", "黑体"),
            ("KaiTi", "楷体"),
            ("SimSun", "宋体"),
            ("NSimSun", "新宋体"),
            ("SimFang", "仿宋"),
            ("SimKai", "楷体"),
            ("STSong", "宋体"),
            ("STFangsong", "仿宋"),
            ("STKaiti", "楷体"),
            ("STHeiti", "黑体"),
            ("STXihei", "黑体"),
            ("FZXiaoBiaoSong-B05", "小标宋"),
            ("FZXiaoBiaoSong-B05S", "小标宋"),
            ("方正小标宋简体", "小标宋"),
            ("方正小标宋_GBK", "小标宋"),
            ("仿宋_GB2312", "仿宋"),
            ("楷体_GB2312", "楷体"),
        ],
    )
    def test_english_to_chinese(self, english: str, chinese: str) -> None:
        assert normalize_font_family(english) == chinese

    # Chinese names pass through (already standard)
    @pytest.mark.parametrize(
        "cn_name",
        ["仿宋", "黑体", "楷体", "宋体", "新宋体", "小标宋"],
    )
    def test_chinese_standard_names_unchanged(self, cn_name: str) -> None:
        assert normalize_font_family(cn_name) == cn_name

    # Chinese aliases normalize to standard
    def test_chinese_alias_xiaobiaosongti(self) -> None:
        assert normalize_font_family("小标宋体") == "小标宋"

    def test_chinese_alias_biaosong(self) -> None:
        assert normalize_font_family("标宋") == "小标宋"

    # Empty string stays empty
    def test_empty_string(self) -> None:
        assert normalize_font_family("") == ""

    # Unknown font name passed through unchanged
    def test_unknown_font_passed_through(self) -> None:
        assert normalize_font_family("SomeUnknownFont") == "SomeUnknownFont"

    # Font name with leading/trailing spaces
    def test_stripped_whitespace(self) -> None:
        assert normalize_font_family(" FangSong ") == "仿宋"


class TestFontFamilyComparison:
    """Test that font comparison in validator handles English names correctly."""

    def test_fangsong_matches_仿宋(self) -> None:
        """FangSong (from python-docx) should match 仿宋 (from spec)."""
        spec_name = normalize_font_family("仿宋")
        actual_name = normalize_font_family("FangSong")
        assert spec_name == actual_name

    def test_simhei_matches_黑体(self) -> None:
        """SimHei should match 黑体."""
        assert normalize_font_family("黑体") == normalize_font_family("SimHei")

    def test_kaiti_matches_楷体(self) -> None:
        """KaiTi should match 楷体."""
        assert normalize_font_family("楷体") == normalize_font_family("KaiTi")

    def test_xiaobiaosong_variants_match(self) -> None:
        """All xiaobiaosong variants should normalize to 小标宋."""
        variants = [
            "小标宋",
            "FZXiaoBiaoSong-B05",
            "FZXiaoBiaoSong-B05S",
            "方正小标宋简体",
            "方正小标宋_GBK",
            "小标宋体",
            "标宋",
        ]
        results = [normalize_font_family(v) for v in variants]
        assert all(r == "小标宋" for r in results)

    def test_different_fonts_dont_match(self) -> None:
        """Different fonts should still not match after normalization."""
        assert normalize_font_family("仿宋") != normalize_font_family("黑体")
        assert normalize_font_family("楷体") != normalize_font_family("宋体")
