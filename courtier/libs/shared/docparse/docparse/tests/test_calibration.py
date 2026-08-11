"""Tests for GB/T 9704-2012 standard calibration module."""

from docparse.parsers.calibration import (
    ELEMENT_FORMAT_MAP,
    STANDARD_FONT_SIZES,
    STANDARD_SIZE_VALUES,
    calibrate_first_indent,
    calibrate_font_size,
    calibrate_line_spacing,
    calibrate_margin,
    compute_font_size,
    get_element_format,
)


class TestStandardFontSizes:
    """Verify STANDARD_FONT_SIZE data integrity."""

    def test_common_sizes_exist(self) -> None:
        expected = {"初号", "小初", "一号", "二号", "三号", "四号", "小四", "五号"}
        assert expected.issubset(set(STANDARD_FONT_SIZES.keys()))

    def test_all_values_positive(self) -> None:
        for name, pt in STANDARD_FONT_SIZES.items():
            assert pt > 0, f"{name} has non-positive value {pt}"

    def test_specific_values(self) -> None:
        assert STANDARD_FONT_SIZES["初号"] == 42.0
        assert STANDARD_FONT_SIZES["三号"] == 16.0
        assert STANDARD_FONT_SIZES["小四"] == 12.0
        assert STANDARD_FONT_SIZES["五号"] == 10.5

    def test_standard_size_values_sorted_unique(self) -> None:
        unique_values = sorted(set(STANDARD_FONT_SIZES.values()))
        assert STANDARD_SIZE_VALUES == unique_values


class TestElementFormatMap:
    """Verify ELEMENT_FORMAT_MAP lookup correctness."""

    def test_body_text(self) -> None:
        family, size, bold = ELEMENT_FORMAT_MAP["body_text"]
        assert family == "仿宋"
        assert size == 16.0
        assert bold is False

    def test_title(self) -> None:
        family, size, bold = ELEMENT_FORMAT_MAP["title"]
        assert family == "方正小标宋简体"
        assert size == 22.0
        assert bold is False

    def test_heading1(self) -> None:
        family, size, bold = ELEMENT_FORMAT_MAP["heading1"]
        assert family == "黑体"
        assert size == 16.0

    def test_heading2(self) -> None:
        family, size, bold = ELEMENT_FORMAT_MAP["heading2"]
        assert family == "楷体"

    def test_heading3_bold(self) -> None:
        _, _, bold = ELEMENT_FORMAT_MAP["heading3"]
        assert bold is True

    def test_footer_elements_14pt(self) -> None:
        for name in ("carbon_copy", "issuing_office", "distribution_date", "page_number"):
            family, size, bold = ELEMENT_FORMAT_MAP[name]
            assert size == 14.0, f"{name} should be 14pt, got {size}"

    def test_get_element_format_known(self) -> None:
        result = get_element_format("body_text")
        assert result is not None
        assert result == ("仿宋", 16.0, False)

    def test_get_element_format_unknown(self) -> None:
        result = get_element_format("nonexistent_element")
        assert result is None


class TestCalibrateFontSize:
    """Verify font size calibration with +/-1pt tolerance."""

    def test_exact_match(self) -> None:
        assert calibrate_font_size(16.0) == 16.0

    def test_close_snap_within_tolerance(self) -> None:
        # 15.2 is closest to 15.0 (dist=0.2), not 16.0 (dist=0.8)
        assert calibrate_font_size(15.2) == 15.0
        assert calibrate_font_size(16.8) == 16.0
        assert calibrate_font_size(10.0) == 10.5
        # 10.8 is closest to 10.5 (dist=0.3), not 12.0 (dist=1.2)
        assert calibrate_font_size(10.8) == 10.5

    def test_far_kept_original(self) -> None:
        # 13.0 is 1pt from both 12.0 and 14.0 -- within tolerance, snaps to 12.0
        assert calibrate_font_size(13.0) == 12.0
        # 20.0 is 2pt from both 18.0 and 22.0 -- outside tolerance, kept
        assert calibrate_font_size(20.0) == 20.0

    def test_zero(self) -> None:
        assert calibrate_font_size(0.0) == 0.0

    def test_near_14pt(self) -> None:
        assert calibrate_font_size(13.5) == 14.0
        # 14.6 is closer to 15.0 (dist=0.4) than 14.0 (dist=0.6)
        assert calibrate_font_size(14.6) == 15.0

    def test_boundary_exactly_1pt(self) -> None:
        # 15.0 is an exact standard value (小三), should stay as-is
        assert calibrate_font_size(15.0) == 15.0
        # 17.0 is 1pt from 16.0 and 1pt from 18.0 -- snaps to 16.0 (first match)
        assert calibrate_font_size(17.0) == 16.0


class TestCalibrateMargin:
    """Verify margin calibration with +/-2mm tolerance."""

    def test_close_snap(self) -> None:
        assert calibrate_margin("top", 36.0) == 37.0
        assert calibrate_margin("bottom", 34.0) == 35.0
        assert calibrate_margin("left", 27.0) == 28.0
        assert calibrate_margin("right", 25.0) == 26.0

    def test_far_kept_original(self) -> None:
        assert calibrate_margin("top", 30.0) == 30.0
        assert calibrate_margin("left", 20.0) == 20.0

    def test_unknown_side(self) -> None:
        assert calibrate_margin("diagonal", 40.0) == 40.0

    def test_exact_match(self) -> None:
        assert calibrate_margin("top", 37.0) == 37.0

    def test_boundary_exactly_2mm(self) -> None:
        assert calibrate_margin("top", 35.0) == 37.0
        assert calibrate_margin("top", 39.0) == 37.0


class TestCalibrateLineSpacing:
    """Verify line spacing is measured from scan, not calibrated to standard."""

    def test_passthrough(self) -> None:
        """All values pass through unchanged — no calibration."""
        assert calibrate_line_spacing(24.0) == 24.0
        assert calibrate_line_spacing(27.5) == 27.5
        assert calibrate_line_spacing(28.95) == 28.95
        assert calibrate_line_spacing(30.0) == 30.0
        assert calibrate_line_spacing(35.0) == 35.0

    def test_zero(self) -> None:
        assert calibrate_line_spacing(0.0) == 0.0

    def test_negative(self) -> None:
        assert calibrate_line_spacing(-1.0) == -1.0


class TestCalibrateFirstIndent:
    """Verify first line indent calibration with +/-4pt tolerance."""

    def test_close_snap(self) -> None:
        assert calibrate_first_indent(30.0) == 32.0
        assert calibrate_first_indent(34.0) == 32.0

    def test_far_kept_original(self) -> None:
        assert calibrate_first_indent(20.0) == 20.0
        assert calibrate_first_indent(40.0) == 40.0

    def test_zero(self) -> None:
        assert calibrate_first_indent(0.0) == 0.0

    def test_exact_match(self) -> None:
        assert calibrate_first_indent(32.0) == 32.0

    def test_boundary_exactly_4pt(self) -> None:
        assert calibrate_first_indent(28.0) == 32.0
        assert calibrate_first_indent(36.0) == 32.0

    def test_sub_tolerance_noise_zeroed(self) -> None:
        """<4pt 的微小缩进是 OCR 框抖动噪声，归零。"""
        assert calibrate_first_indent(0.41) == 0.0
        assert calibrate_first_indent(2.05) == 0.0
        assert calibrate_first_indent(3.29) == 0.0
        assert calibrate_first_indent(3.99) == 0.0

    def test_at_tolerance_kept_original(self) -> None:
        """恰好 4pt 不属于噪声，原样保留。"""
        assert calibrate_first_indent(4.0) == 4.0


class TestComputeFontSize:
    """Verify enhanced font size computation with poly and bbox support."""

    def test_basic_from_bbox(self) -> None:
        size = compute_font_size(59, 943, 841.89)
        assert size > 0

    def test_zero_image_height(self) -> None:
        assert compute_font_size(59, 0) == 0.0

    def test_zero_box_height(self) -> None:
        assert compute_font_size(0, 943) == 0.0

    def test_from_polys_averages_height(self) -> None:
        polys = [(10.0, 20.0), (200.0, 22.0), (200.0, 80.0), (10.0, 78.0)]
        size = compute_font_size(0, 943, 841.89, polys=polys)
        assert size > 0

    def test_calibrated_to_standard(self) -> None:
        # Compute a size that should snap to 16pt
        size = compute_font_size(89, 3508, 841.89)
        assert abs(size - 16.0) < 1.0
