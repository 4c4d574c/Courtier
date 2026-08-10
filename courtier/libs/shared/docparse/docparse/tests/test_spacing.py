"""Tests for improved spacing, indent, and margin calculation module."""

import pytest
from docparse.parsers.spacing import (
    ParagraphBoundary,
    compute_alignment_from_position,
    compute_body_line_spacing,
    compute_first_indent,
    compute_font_size_from_ocr,
    compute_left_right_indent,
    compute_margins,
    compute_paragraph_spacing,
    detect_paragraph_boundaries,
)

# ---------------------------------------------------------------------------
# TestComputeFontSizeFromOcr
# ---------------------------------------------------------------------------


class TestComputeFontSizeFromOcr:
    """Verify font size computation from OCR bounding box data."""

    def test_basic(self) -> None:
        """Basic computation returns a positive size."""
        size = compute_font_size_from_ocr(59, 943, 841.89)
        assert size > 0

    def test_zero_image_height(self) -> None:
        """Zero image height returns 0.0."""
        assert compute_font_size_from_ocr(59, 0) == 0.0

    def test_zero_box_height(self) -> None:
        """Zero box height returns 0.0."""
        assert compute_font_size_from_ocr(0, 943) == 0.0

    def test_with_polys(self) -> None:
        """Polys override box height for computation."""
        polys = [(10.0, 20.0), (200.0, 22.0), (200.0, 80.0), (10.0, 78.0)]
        size = compute_font_size_from_ocr(0, 943, 841.89, polys=polys)
        assert size > 0

    def test_default_a4_height(self) -> None:
        """Default A4 height is used when not specified."""
        size = compute_font_size_from_ocr(59, 943)
        assert size > 0


# ---------------------------------------------------------------------------
# TestComputeAlignment
# ---------------------------------------------------------------------------


class TestComputeAlignment:
    """Verify text alignment detection from bounding box position."""

    def test_center(self) -> None:
        """Narrow text centered in page returns 'center'."""
        # x0=200, x1=300, center=250, center_ratio=250/500=0.5
        assert compute_alignment_from_position(200, 300, 500) == "center"

    def test_left(self) -> None:
        """Text on the left side returns 'left'."""
        # x0=20, x1=100, center=60, center_ratio=60/500=0.12
        assert compute_alignment_from_position(20, 100, 500) == "left"

    def test_right(self) -> None:
        """Text on the right side returns 'right'."""
        # x0=400, x1=480, center=440, center_ratio=440/500=0.88
        assert compute_alignment_from_position(400, 480, 500) == "right"

    def test_full_width(self) -> None:
        """Full-width text with no outline returns 'left'."""
        # width_ratio = 400/500 = 0.80, outline="" (not body_like) -> left
        assert compute_alignment_from_position(20, 420, 500) == "left"

    def test_zero_img_width(self) -> None:
        """Zero image width returns 'left' as default."""
        assert compute_alignment_from_position(20, 100, 0) == "left"

    def test_near_center_boundary(self) -> None:
        """Text at center ratio 0.35 is classified as center."""
        # center_ratio = (170 + 200) / 2 / 500 = 0.37
        assert compute_alignment_from_position(170, 200, 500) == "center"

    def test_body_text_returns_justify(self) -> None:
        """Wide body_text returns 'justify' (两端对齐)."""
        # width_ratio = 300/500 = 0.60 > 0.55, outline_level=body_text
        assert compute_alignment_from_position(50, 350, 500, outline_level="body_text") == "justify"

    def test_others_wide_returns_justify(self) -> None:
        """Wide text with outline_level='others' also returns 'justify'."""
        # OCR typically maps body text blocks to 'others'
        assert compute_alignment_from_position(50, 350, 500, outline_level="others") == "justify"

    def test_heading_wide_not_justify(self) -> None:
        """Wide heading text returns 'center' if centered, not 'justify'."""
        # width_ratio = 300/500 = 0.60, center_ratio = 200/500 = 0.40
        # Within center band 0.40-0.60 → center
        assert compute_alignment_from_position(50, 350, 500, outline_level="heading1") == "center"

    def test_body_text_narrow_left(self) -> None:
        """Narrow body_text on left returns 'left'."""
        assert compute_alignment_from_position(20, 100, 500, outline_level="body_text") == "left"

    def test_wide_text_not_misjudged_as_center(self) -> None:
        """Very wide text (>=75%) should NOT be classified as center."""
        # width_ratio = 400/500 = 0.80 >= 0.75, so no center even if centered
        assert compute_alignment_from_position(50, 450, 500) != "center"


# ---------------------------------------------------------------------------
# TestDetectParagraphBoundaries
# ---------------------------------------------------------------------------


class TestDetectParagraphBoundaries:
    """Verify paragraph boundary detection from line gaps."""

    def test_single_paragraph(self) -> None:
        """Close lines are grouped into one paragraph."""
        # Three close lines: gaps are small
        boxes = [[20, 100, 500, 120], [20, 125, 500, 145], [20, 150, 500, 170]]
        boundaries = detect_paragraph_boundaries(boxes, 943, 841.89)
        assert len(boundaries) == 1
        assert boundaries[0].start_index == 0
        assert boundaries[0].end_index == 2
        assert boundaries[0].line_indices == [0, 1, 2]

    def test_two_paragraphs(self) -> None:
        """Large gap creates two paragraph boundaries."""
        # Gap between line 2 and 3 is much larger (250-145=105 vs 125-120=5)
        boxes = [[20, 100, 500, 120], [20, 125, 500, 145], [20, 250, 500, 270]]
        boundaries = detect_paragraph_boundaries(boxes, 943, 841.89)
        assert len(boundaries) == 2

        # First paragraph: lines 0, 1
        assert boundaries[0].start_index == 0
        assert boundaries[0].end_index == 1
        assert boundaries[0].line_indices == [0, 1]

        # Second paragraph: line 2
        assert boundaries[1].start_index == 2
        assert boundaries[1].end_index == 2
        assert boundaries[1].line_indices == [2]

    def test_empty(self) -> None:
        """Empty input returns no boundaries."""
        boundaries = detect_paragraph_boundaries([], 943, 841.89)
        assert boundaries == []

    def test_single_line(self) -> None:
        """Single line produces one paragraph with start == end."""
        boxes = [[20, 100, 500, 120]]
        boundaries = detect_paragraph_boundaries(boxes, 943, 841.89)
        assert len(boundaries) == 1
        assert boundaries[0].start_index == 0
        assert boundaries[0].end_index == 0
        assert boundaries[0].line_indices == [0]


# ---------------------------------------------------------------------------
# TestComputeMargins
# ---------------------------------------------------------------------------


class TestComputeMargins:
    """Verify margin computation with GB/T 9704 calibration."""

    def test_from_boxes(self) -> None:
        """Margins are computed from outermost text boxes."""
        boxes = [[50, 80, 450, 100], [50, 110, 450, 130]]
        margin = compute_margins(boxes, 500, 943, 595.28, 841.89)
        assert margin.top_margin > 0
        assert margin.bottom_margin > 0
        assert margin.left_margin > 0
        assert margin.right_margin > 0

    def test_empty(self) -> None:
        """Empty boxes return zero margins."""
        from docmodels import Margin

        margin = compute_margins([], 500, 943, 595.28, 841.89)
        assert margin == Margin()

    def test_calibrated_to_standard(self) -> None:
        """Margins near GB/T 9704 standard values snap to standard."""
        # Standard: top=37mm, bottom=35mm, left=28mm, right=26mm
        # Compute exact pixel positions that produce standard margins:
        # top: min_y * (841.89/943) * (25.4/72) = 37 => min_y ~ 117.5
        # left: min_x * (595.28/500) * (25.4/72) = 28 => min_x ~ 66.7
        boxes = [[67, 118, 434, 200]]
        margin = compute_margins(boxes, 500, 943, 595.28, 841.89)
        # Should snap to exactly standard values
        assert margin.top_margin == 37.0
        assert margin.left_margin == 28.0


# ---------------------------------------------------------------------------
# TestComputeParagraphSpacing
# ---------------------------------------------------------------------------


class TestComputeParagraphSpacing:
    """Verify paragraph spacing computation with calibration."""

    def test_computes_map(self) -> None:
        """Returns a map of line indices to spacing info."""
        boxes = [[20, 100, 500, 120], [20, 125, 500, 145], [20, 250, 500, 270]]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        assert isinstance(spacing, dict)
        assert len(spacing) > 0
        for line_idx, info in spacing.items():
            assert "space_before" in info
            assert "space_after" in info
            assert "line_spacing" in info

    def test_single_line(self) -> None:
        """Single line: spacing unmeasurable -> None (not a guessed 0.0)."""
        boxes = [[20, 100, 500, 120]]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        assert len(spacing) == 1
        info = spacing[0]
        assert info["space_before"] is None
        assert info["space_after"] is None
        # 行距需要两行才能测量：单行页为 None（未测得），而非硬编码 0.0
        # （0.0 会被 validator 拿去与规则 28.95pt 比对而误报）。
        assert info["line_spacing"] is None

    def test_paragraph_break_space_after(self) -> None:
        """Lines before a paragraph break get non-zero space_after."""
        boxes = [[20, 100, 500, 120], [20, 125, 500, 145], [20, 250, 500, 270]]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        # Line 1 (index 1) is the last line before the gap
        line_1_spacing = spacing[1]
        assert line_1_spacing["space_after"] > 0.0

    def test_line_spacing_calibrated(self) -> None:
        """Line spacing near standard 28.95pt is calibrated."""
        # Create boxes where line spacing would be close to 28.95pt
        # scale_y = 841.89 / 943 = 0.8927
        # median_gap in pt ~ (gap_px * scale_y)
        # For gap=33px: 33 * 0.8927 ~ 29.5pt -> should calibrate to 28.95
        boxes = [
            [20, 100, 500, 120],
            [20, 153, 500, 173],
            [20, 206, 500, 226],
        ]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        # Check that at least one line has calibrated line_spacing
        line_spacings = [v["line_spacing"] for v in spacing.values()]
        assert any(ls > 0 for ls in line_spacings)

    def test_body_text_no_false_paragraph_break(self) -> None:
        """5 body text lines with 1-2px jitter gaps (~30px each)."""
        boxes = [
            [20, 100, 500, 120],
            [20, 130, 500, 150],
            [20, 161, 500, 181],
            [20, 191, 500, 211],
            [20, 221, 500, 241],
        ]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        for info in spacing.values():
            # space_before is unmeasurable for scanned lines -> None
            assert info["space_before"] is None
            assert info["space_after"] in (0.0, None)  # no false break

    def test_real_paragraph_break_detected(self) -> None:
        """3 body text lines with ~30px gaps, then a 100px gap."""
        boxes = [
            [20, 100, 500, 120],
            [20, 130, 500, 150],
            [20, 160, 500, 180],
            [20, 280, 500, 300],
        ]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        # Line before gap (index 2) should have space_after > 0
        assert spacing[2]["space_after"] > 0.0
        # space_before is never measured (single-side accounting, no
        # double-counting) — it stays None rather than a hardcoded 0.0.
        assert spacing[3]["space_before"] is None

    def test_line_spacing_uniform_in_body(self) -> None:
        """Multiple body text lines with jitter, all same line_spacing."""
        boxes = [
            [20, 100, 500, 120],
            [20, 130, 500, 150],
            [20, 161, 500, 181],
            [20, 191, 500, 211],
            [20, 221, 500, 241],
        ]
        spacing = compute_paragraph_spacing(boxes, 943, 841.89)
        line_spacings = [v["line_spacing"] for v in spacing.values()]
        assert len(set(line_spacings)) == 1
        assert line_spacings[0] > 0.0


# ---------------------------------------------------------------------------
# TestComputeFirstIndent
# ---------------------------------------------------------------------------


class TestComputeFirstIndent:
    """Verify first-line indent detection and calibration."""

    def test_detects_indent(self) -> None:
        """First line X offset from body X is detected as indent."""
        # First line starts at x=60, body starts at x=20
        boxes = [[60, 100, 500, 120], [20, 125, 500, 145], [20, 150, 500, 170]]
        indent = compute_first_indent(boxes, 500, 595.28)
        # Line 0 should have a positive indent
        assert indent[0] > 0

    def test_no_indent(self) -> None:
        """All lines at same X produce zero indent."""
        boxes = [[20, 100, 500, 120], [20, 125, 500, 145], [20, 150, 500, 170]]
        indent = compute_first_indent(boxes, 500, 595.28)
        # All lines at same X, so indent should be 0
        assert indent[0] == 0.0

    def test_single_line(self) -> None:
        """Single line returns zero indent."""
        boxes = [[60, 100, 500, 120]]
        indent = compute_first_indent(boxes, 500, 595.28)
        assert len(indent) == 1
        assert indent[0] == 0.0

    def test_indent_calibrated(self) -> None:
        """Indent near standard 32pt is calibrated."""
        # Indent px ~ 40px, scale_x = 595.28/500 = 1.19056
        # indent_pt ~ 40 * 1.19056 ~ 47.6pt -> too far, not calibrated
        # Let's use a smaller indent: 27px -> 27 * 1.19056 ~ 32.1pt -> calibrated
        boxes = [[47, 100, 500, 120], [20, 125, 500, 145], [20, 150, 500, 170]]
        indent = compute_first_indent(boxes, 500, 595.28)
        # Should be calibrated to exactly 32.0pt
        assert indent[0] == 32.0

    def test_single_line_body_text_indent(self) -> None:
        """Single-line body_text falls back to standard 32pt indent."""
        boxes = [[60, 100, 500, 120]]
        outline_levels = {0: "body_text"}
        indent = compute_first_indent(boxes, 500, 595.28, outline_levels=outline_levels)
        assert indent[0] == 32.0

    def test_single_line_non_body_text_no_indent(self) -> None:
        """Single-line non-body_text has zero indent."""
        boxes = [[60, 100, 500, 120]]
        indent = compute_first_indent(boxes, 500, 595.28)
        assert indent[0] == 0.0


# ---------------------------------------------------------------------------
# TestComputeLeftRightIndent
# ---------------------------------------------------------------------------


class TestComputeLeftRightIndent:
    """Verify left/right indent detection from bounding boxes and margins."""

    def test_no_indent_when_at_margins(self) -> None:
        """Text at exactly margin positions produces zero indents."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        # Boxes at exact margin positions (67px left, 500-67=433px right for
        # standard 500px image → 210mm mapping with 28/26mm margins)
        boxes = [[67, 100, 434, 120], [67, 125, 434, 145]]
        result = compute_left_right_indent(boxes, 500, margin=margin)
        assert result[0]["left_indent"] == 0.0
        assert result[0]["right_indent"] == 0.0

    def test_left_indent_detected(self) -> None:
        """Text inset from left margin produces positive left_indent."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        boxes = [[150, 100, 434, 120], [150, 125, 434, 145]]
        result = compute_left_right_indent(boxes, 500, margin=margin)
        assert result[0]["left_indent"] > 0
        assert result[0]["right_indent"] == 0.0

    def test_right_indent_detected(self) -> None:
        """Text short of the right margin produces positive right_indent."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        boxes = [[67, 100, 300, 120], [67, 125, 300, 145]]
        result = compute_left_right_indent(boxes, 500, margin=margin)
        assert result[0]["left_indent"] == 0.0
        assert result[0]["right_indent"] > 0

    def test_none_margin_returns_zero(self) -> None:
        """None margin returns all zeros."""
        boxes = [[120, 100, 434, 120]]
        result = compute_left_right_indent(boxes, 500, margin=None)
        assert result[0]["left_indent"] == 0.0
        assert result[0]["right_indent"] == 0.0

    def test_empty_boxes(self) -> None:
        """Empty input returns empty dict."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        result = compute_left_right_indent([], 500, margin=margin)
        assert result == {}

    def test_all_lines_in_paragraph_same_values(self) -> None:
        """All lines in a paragraph get the same left/right indent values."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        boxes = [[120, 100, 434, 120], [120, 125, 434, 145], [120, 150, 434, 170]]
        result = compute_left_right_indent(boxes, 500, margin=margin)
        vals = [(result[i]["left_indent"], result[i]["right_indent"]) for i in range(3)]
        assert len(set(vals)) == 1  # all lines have same values

    def test_two_paragraphs_different_values(self) -> None:
        """Different paragraphs can have different left/right indent."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        boxes = [
            [150, 100, 434, 120],  # para 0, line 0 - left indented
            [150, 125, 434, 145],  # para 0, line 1 - left indented
            [67, 300, 434, 320],  # para 1, line 0 - at margin (large gap)
            [67, 325, 434, 345],  # para 1, line 1
        ]
        result = compute_left_right_indent(boxes, 500, margin=margin)
        assert result[0]["left_indent"] != result[2]["left_indent"]

    def test_font_size_quantization(self) -> None:
        """Indent values are quantized to character-width steps."""
        from docmodels import Margin

        margin = Margin(left_margin=28.0, right_margin=26.0)
        # font_size 16pt → char_width ≈ 16pt
        # x0=190px, img=500px, page=210mm, left_margin=28mm
        # min_x_mm = 190 * 210/500 = 79.8mm
        # left_indent_mm = 79.8 - 28 = 51.8mm
        # left_indent_pt = 51.8 * (72/25.4) ≈ 146.8pt
        # 146.8 / 16 = 9.175 → round to 9 steps → 9*16 = 144pt
        boxes = [[190, 100, 434, 120]]
        font_sizes = {0: 16.0}
        result = compute_left_right_indent(
            boxes,
            500,
            margin=margin,
            font_sizes=font_sizes,
        )
        assert result[0]["left_indent"] == 144.0


# ---------------------------------------------------------------------------
# TestParagraphBoundary dataclass
# ---------------------------------------------------------------------------


class TestParagraphBoundaryDataclass:
    """Verify ParagraphBoundary dataclass structure."""

    def test_fields(self) -> None:
        """Dataclass has correct fields."""
        b = ParagraphBoundary(start_index=0, end_index=2, line_indices=[0, 1, 2])
        assert b.start_index == 0
        assert b.end_index == 2
        assert b.line_indices == [0, 1, 2]

    def test_frozen(self) -> None:
        """Dataclass is immutable (frozen)."""
        b = ParagraphBoundary(start_index=0, end_index=0, line_indices=[0])
        with pytest.raises(AttributeError):
            b.start_index = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestComputeBodyLineSpacing
# ---------------------------------------------------------------------------


class TestComputeBodyLineSpacing:
    """Verify histogram-based body line spacing clustering."""

    def test_single_cluster(self) -> None:
        """10 gaps around 30px with minor jitter -> center should be ~30."""
        gaps = [29.0, 31.0, 30.0, 28.5, 30.5, 29.5, 31.5, 30.0, 29.0, 31.0]
        center, width = compute_body_line_spacing(gaps, bin_width=2.0)
        assert 29.0 <= center <= 31.0
        assert width == 2.0

    def test_two_clusters_prefers_larger(self) -> None:
        """8 gaps ~30px + 2 gaps ~80px -> should pick ~30px cluster."""
        gaps = [
            29.0,
            31.0,
            30.0,
            28.5,
            30.5,
            29.5,
            31.5,
            30.0,
            79.0,
            81.0,
        ]
        center, width = compute_body_line_spacing(gaps, bin_width=2.0)
        assert 29.0 <= center <= 31.0
        assert width == 2.0

    def test_empty_fallback(self) -> None:
        """Empty list -> (0.0, 0.0)."""
        assert compute_body_line_spacing([]) == (0.0, 0.0)

    def test_single_gap(self) -> None:
        """Single value -> returns that value."""
        assert compute_body_line_spacing([42.0]) == (42.0, 2.0)
