"""Improved spacing, indent, and margin calculation for scanned documents.

Provides paragraph boundary detection, margin computation with GB/T 9704
calibration, paragraph spacing analysis, and first-line indent detection.
All functions operate on OCR bounding box data in pixel coordinates and
convert to standard units (mm, pt) with calibration to the
GB/T 9704-2012 standard where applicable.
"""

from __future__ import annotations

from dataclasses import dataclass

from docmodels import Margin

from .calibration import (
    STANDARD_FIRST_INDENT,
    calibrate_first_indent,
    calibrate_line_spacing,
    calibrate_margin,
    compute_font_size,
)

# -- Threshold constants for paragraph boundary detection -------------------
# These reflect GB/T 9704-2012 formatting rules and should be reviewed
# when the standard is updated.
_PARA_BREAK_FACTOR = 1.2       # line-height multiplier for paragraph breaks
_MIN_PARA_BREAK_PX = 2.0       # minimum gap (px) that constitutes a break
_PARA_SPACING_FACTOR = 1.5     # median-gap multiplier for spacing detection

from ._constants import PT_TO_MM, MM_TO_PT  # noqa: E402

# A4 page width in points (72 DPI)
A4_WIDTH_PT: float = 595.28


@dataclass(frozen=True)
class ParagraphBoundary:
    """Represents a contiguous group of text lines forming a paragraph.

    Attributes:
        start_index: Index of the first line in the paragraph (sorted by Y).
        end_index: Index of the last line in the paragraph (sorted by Y).
        line_indices: Original indices of all lines in the paragraph.
    """

    start_index: int
    end_index: int
    line_indices: list[int]


def compute_font_size_from_ocr(
    box_height_px: float,
    img_height_px: float,
    a4_height_pt: float = 841.89,
    polys: list[tuple[float, float]] | None = None,
) -> float:
    """Compute calibrated font size from OCR bounding box measurements.

    Wrapper around calibration.compute_font_size() for use in the
    spacing module's public API.

    Args:
        box_height_px: Height of the text bounding box in pixels.
        img_height_px: Total image height in pixels.
        a4_height_pt: A4 page height in points (default 841.89).
        polys: Optional quadrilateral corner points for better height estimate.

    Returns:
        Calibrated font size in points, or 0.0 for invalid inputs.
    """
    return compute_font_size(box_height_px, img_height_px, a4_height_pt, polys)


def compute_body_line_spacing(
    gaps_px: list[float],
    bin_width: float = 2.0,
    *,
    outlier_median_multiplier: float | None = None,
    min_cluster_ratio: float = 0.0,
) -> tuple[float, float]:
    """Extract dominant body text line spacing via histogram clustering.

    OCR coordinates have 1-2 px jitter. Normal body text lines cluster
    together; paragraph breaks produce much larger gaps in separate bins.

    When *outlier_median_multiplier* is set (e.g. 2.5), gaps exceeding
    median × multiplier are filtered out before clustering — useful for
    excluding paragraph-break outliers.  *min_cluster_ratio* (0.0–1.0)
    requires the best bin to contain at least this fraction of filtered
    values, falling back to median otherwise.

    Args:
        gaps_px: Y-gap values (in pixels) between consecutive text lines.
        bin_width: Width of each histogram bin.
        outlier_median_multiplier: If set, filter values > median × factor.
        min_cluster_ratio: Minimum fraction of values the best bin must
            contain (0 = no check).  Default 0.0 preserves existing behaviour.

    Returns:
        (cluster_center, cluster_width) where center is the mean of the
        values in the dominant bin. If gaps_px is empty, returns (0.0, 0.0).
        If gaps_px has one element, returns (value, bin_width).
    """
    if not gaps_px:
        return (0.0, 0.0)

    if len(gaps_px) == 1:
        return (gaps_px[0], bin_width)

    # Optional outlier filtering
    values = list(gaps_px)
    if outlier_median_multiplier is not None:
        sorted_vals = sorted(values)
        median = sorted_vals[len(sorted_vals) // 2]
        upper_bound = median * outlier_median_multiplier
        filtered = [v for v in values if v <= upper_bound]
        if len(filtered) >= max(3, len(values) * 0.4):
            values = filtered

    # Histogram clustering
    bins: dict[int, list[float]] = {}
    for gap in values:
        idx = int(gap // bin_width)
        bins.setdefault(idx, []).append(gap)

    if not bins:
        return (0.0, 0.0)

    best_bin = max(bins.values(), key=lambda v: len(v))

    # Confidence check
    if min_cluster_ratio > 0 and values:
        min_required = max(3, len(values) * min_cluster_ratio)
        if len(best_bin) < min_required:
            sorted_vals = sorted(values)
            center = sorted_vals[len(sorted_vals) // 2]
            return (center, bin_width)

    center = sum(best_bin) / len(best_bin)
    return (center, bin_width)


def compute_alignment_from_position(
    x0: float,
    x1: float,
    img_width: float,
    outline_level: str = "",
) -> str:
    """Compute text alignment from bounding box horizontal position.

    Strategy:
    - Wide text (>=55% of page) with body/others outline -> "justify" (两端对齐).
    - Narrow text (<50%) classified by center position:
      center of page -> "center", right side -> "right", else -> "left".
    - Heading/title types never get justify.

    Args:
        x0: Left edge of the text bounding box in pixels.
        x1: Right edge of the text bounding box in pixels.
        img_width: Total image width in pixels.
        outline_level: Outline level hint (e.g. "body_text", "heading1").

    Returns:
        "justify", "center", "right", or "left".
    """
    if img_width <= 0:
        return "left"

    text_width = x1 - x0
    width_ratio = text_width / img_width

    is_body_like = outline_level in ("body_text", "others")

    # Wide body-like text -> justify (两端对齐，符合公文正文标准)
    if is_body_like and width_ratio >= 0.55:
        return "justify"

    # Body-like text starting from the left margin is left-aligned, not
    # centered — even when short (e.g. last line of a paragraph). The
    # standard left margin is ~13% of page width; 22% allows for indent.
    left_ratio = x0 / img_width
    if is_body_like and left_ratio < 0.22:
        return "left"

    center_x = (x0 + x1) / 2
    center_ratio = center_x / img_width

    # Center band width depends on text width — shorter OCR boxes have
    # more positional jitter, so use a wider tolerance band
    if width_ratio < 0.30:
        center_lo, center_hi = 0.25, 0.75
    elif width_ratio < 0.50:
        center_lo, center_hi = 0.30, 0.70
    else:
        center_lo, center_hi = 0.40, 0.60

    # Allow center for text up to 75% width — this catches wide titles
    # (typically 50-75% of page) while blocking full-width body paragraphs.
    if width_ratio < 0.75 and center_lo <= center_ratio <= center_hi:
        return "center"
    if center_ratio > 0.75:
        return "right"
    return "left"


def detect_paragraph_boundaries(
    rec_boxes: list[list[float]],
    img_height: int,
    a4_height_pt: float,
) -> list[ParagraphBoundary]:
    """Detect paragraph boundaries by analyzing Y-coordinate gaps.

    Sorts boxes by Y coordinate, computes line heights, finds the median,
    and identifies paragraph breaks where gaps exceed 1.2x the median height.

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_height: Image height in pixels.
        a4_height_pt: A4 page height in points.

    Returns:
        List of ParagraphBoundary objects, each representing a paragraph.
    """
    if not rec_boxes:
        return []

    # Sort boxes by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    if len(indexed_boxes) == 1:
        idx = indexed_boxes[0][0]
        return [
            ParagraphBoundary(
                start_index=0,
                end_index=0,
                line_indices=[idx],
            )
        ]

    # Compute line heights (y1 - y0) for each box
    line_heights = [box[3] - box[1] for _, box in indexed_boxes]
    sorted_heights = sorted(line_heights)
    median_height = sorted_heights[len(sorted_heights) // 2]

    # Threshold for paragraph break: 1.2x median line height
    para_threshold = max(median_height * _PARA_BREAK_FACTOR, _MIN_PARA_BREAK_PX)

    # Identify paragraph boundaries by gap between consecutive lines
    boundaries: list[ParagraphBoundary] = []
    current_start = 0
    current_indices: list[int] = [indexed_boxes[0][0]]

    for i in range(len(indexed_boxes) - 1):
        curr_bottom = indexed_boxes[i][1][3]
        next_top = indexed_boxes[i + 1][1][1]
        gap = max(0, next_top - curr_bottom)

        if gap > para_threshold:
            # End current paragraph, start new one
            boundaries.append(
                ParagraphBoundary(
                    start_index=current_start,
                    end_index=i,
                    line_indices=list(current_indices),
                )
            )
            current_start = i + 1
            current_indices = [indexed_boxes[i + 1][0]]
        else:
            current_indices.append(indexed_boxes[i + 1][0])

    # Add the last paragraph
    boundaries.append(
        ParagraphBoundary(
            start_index=current_start,
            end_index=len(indexed_boxes) - 1,
            line_indices=list(current_indices),
        )
    )

    return boundaries


def compute_margins(
    rec_boxes: list[list[float]],
    img_width: int,
    img_height: int,
    a4_width_pt: float,
    a4_height_pt: float,
) -> Margin:
    """Compute page margins from outermost text bounding boxes.

    Finds the minimum distance from each page edge to the nearest
    text region, converts from pixels to millimeters, and calibrates
    to GB/T 9704-2012 standard margins.

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_width: Image width in pixels.
        img_height: Image height in pixels.
        a4_width_pt: A4 page width in points.
        a4_height_pt: A4 page height in points.

    Returns:
        Calibrated Margin model with values in millimeters.
    """
    if not rec_boxes:
        return Margin()

    min_x = min(box[0] for box in rec_boxes)
    min_y = min(box[1] for box in rec_boxes)
    max_x = max(box[2] for box in rec_boxes)
    max_y = max(box[3] for box in rec_boxes)

    scale_x = a4_width_pt / img_width if img_width > 0 else 1.0
    scale_y = a4_height_pt / img_height if img_height > 0 else 1.0

    raw_top = round(min_y * scale_y * PT_TO_MM, 2)
    raw_bottom = round((img_height - max_y) * scale_y * PT_TO_MM, 2)
    raw_left = round(min_x * scale_x * PT_TO_MM, 2)
    raw_right = round((img_width - max_x) * scale_x * PT_TO_MM, 2)

    return Margin(
        top_margin=calibrate_margin("top", raw_top),
        bottom_margin=calibrate_margin("bottom", raw_bottom),
        left_margin=calibrate_margin("left", raw_left),
        right_margin=calibrate_margin("right", raw_right),
    )


def compute_paragraph_spacing(
    rec_boxes: list[list[float]],
    img_height: int,
    a4_height_pt: float,
    img_width: int = 0,
) -> dict[int, dict[str, float]]:
    """Compute spacing between text lines with paragraph detection.

    Uses histogram-based clustering to find the dominant body line spacing,
    then detects paragraph breaks with an adaptive threshold (1.8x cluster
    center). Body text within a paragraph gets uniform line spacing.

    Also detects paragraph breaks when consecutive lines start at very
    different x0 positions (e.g. attachment-item lists).

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_height: Image height in pixels.
        a4_height_pt: A4 page height in points.
        outline_levels: Optional mapping from line index to outline_level.
        img_width: Image width in pixels (optional). If provided, enables
            x0-based paragraph break detection.

    Returns:
        Dict mapping original line index to spacing info:
        {line_index: {"space_before": pt, "space_after": pt, "line_spacing": pt}}
    """
    if len(rec_boxes) < 2:
        return {0: {"space_before": 0.0, "space_after": 0.0, "line_spacing": 0.0}}

    scale_y = a4_height_pt / img_height if img_height > 0 else 1.0
    scale_x = A4_WIDTH_PT / img_width if img_width > 0 else 1.0

    # When consecutive lines start at very different x0 positions, treat
    # as a paragraph break even if the vertical gap is small.  Threshold
    # (40pt) exceeds the standard 2-char indent (32pt) with OCR jitter
    # margin, so normal indent→continuation is not broken apart.
    _x0_break_px: float = 40.0 / scale_x if img_width > 0 else float("inf")

    # Sort boxes by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    # Compute bottom-to-top gaps (for paragraph break detection) and
    # top-to-top distances (for line spacing).  Also record x0 positions
    # for x0-based break detection.
    gaps_px: list[float] = []
    gaps: list[tuple[int, float, float]] = []  # (idx, gap_pt, curr_height_pt)
    top_distances_px: list[float] = []
    x0_pairs: list[tuple[float, float]] = []  # (curr_x0, next_x0) per gap
    for i in range(len(indexed_boxes) - 1):
        curr_idx = indexed_boxes[i][0]
        curr_bottom = indexed_boxes[i][1][3]
        curr_top = indexed_boxes[i][1][1]
        curr_x0 = indexed_boxes[i][1][0]
        next_top = indexed_boxes[i + 1][1][1]
        next_x0 = indexed_boxes[i + 1][1][0]
        gap_px = max(0.0, next_top - curr_bottom)
        gap_pt = gap_px * scale_y
        curr_height_pt = max(0.0, curr_bottom - curr_top) * scale_y
        gaps_px.append(gap_px)
        gaps.append((curr_idx, gap_pt, curr_height_pt))
        top_distances_px.append(max(0.0, next_top - curr_top))
        x0_pairs.append((curr_x0, next_x0))

    # Line spacing: cluster top-to-top distances (true center-to-center spacing)
    top_cluster_center_px, _ = compute_body_line_spacing(top_distances_px)

    if top_cluster_center_px <= 0.0:
        sorted_top = sorted(top_distances_px)
        top_cluster_center_px = sorted_top[len(sorted_top) // 2]

    top_cluster_center_pt = top_cluster_center_px * scale_y
    calibrated_line_spacing = calibrate_line_spacing(top_cluster_center_pt)

    # Paragraph break: cluster bottom-to-top gaps (gaps exclude line height,
    # making paragraph breaks easier to distinguish from inter-line gaps)
    cluster_center_px, _ = compute_body_line_spacing(gaps_px)

    if cluster_center_px <= 0.0:
        sorted_gaps_px = sorted(gaps_px)
        cluster_center_px = sorted_gaps_px[len(sorted_gaps_px) // 2]

    # Adaptive threshold for paragraph break (based on bottom-to-top gaps)
    para_threshold_px = max(cluster_center_px * 1.8, cluster_center_px + 5.0)
    para_threshold_pt = para_threshold_px * scale_y

    def _is_para_break(gap_idx: int) -> bool:
        """True if the gap at *gap_idx* separates two paragraphs."""
        if gaps[gap_idx][1] > para_threshold_pt:
            return True
        if x0_pairs and abs(x0_pairs[gap_idx][1] - x0_pairs[gap_idx][0]) > _x0_break_px:
            return True
        return False

    result: dict[int, dict[str, float]] = {}
    for i, (idx, gap_pt, curr_height_pt) in enumerate(gaps):
        is_break = _is_para_break(i)

        # Only set space_after for paragraph breaks, and subtract the
        # normal intra-paragraph visual gap (line_spacing - text_height)
        # that is already produced by Word's EXACTLY line spacing rule.
        if is_break:
            normal_visual_gap = max(0.0, calibrated_line_spacing - curr_height_pt)
            space_after = max(0.0, gap_pt - normal_visual_gap)
        else:
            space_after = 0.0

        result[idx] = {
            "space_before": 0.0,
            "space_after": round(space_after, 2),
            "line_spacing": round(calibrated_line_spacing, 2),
        }

    # Handle last line
    if indexed_boxes:
        last_idx = indexed_boxes[-1][0]
        if last_idx not in result:
            result[last_idx] = {
                "space_before": 0.0,
                "space_after": 0.0,
                "line_spacing": round(calibrated_line_spacing, 2),
            }

    return result


def compute_first_indent(
    rec_boxes: list[list[float]],
    img_width: int,
    a4_width_pt: float,
    outline_levels: dict[int, str] | None = None,
) -> dict[int, float]:
    """Compute first-line indent for paragraphs with calibration.

    Detects paragraphs using detect_paragraph_boundaries, compares
    the first line X position with the body X position, and calibrates
    the indent to the GB/T 9704 standard (32.0pt).
    Single-line body_text paragraphs fallback to the standard indent.

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_width: Image width in pixels.
        a4_width_pt: A4 page width in points.
        outline_levels: Optional mapping from line index to outline_level,
            used for single-line fallback.

    Returns:
        Dict mapping original line index to calibrated first_indent in points.
    """
    if not rec_boxes:
        return {}

    # Sort by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    if len(rec_boxes) == 1:
        idx = indexed_boxes[0][0]
        if outline_levels and outline_levels.get(idx) == "body_text":
            return {idx: calibrate_first_indent(STANDARD_FIRST_INDENT)}
        return {idx: 0.0}

    scale_x = a4_width_pt / img_width if img_width > 0 else 1.0

    # Find paragraph boundaries using gap analysis
    gaps: list[float] = []
    # x0 difference between consecutive lines (for x0-based break detection)
    x0_diffs: list[float] = []
    for i in range(len(indexed_boxes) - 1):
        curr_bottom = indexed_boxes[i][1][3]
        next_top = indexed_boxes[i + 1][1][1]
        gaps.append(max(0, next_top - curr_bottom))
        x0_diffs.append(abs(indexed_boxes[i + 1][1][0] - indexed_boxes[i][1][0]))

    if not gaps:
        idx = indexed_boxes[0][0]
        if outline_levels and outline_levels.get(idx) == "body_text":
            return {idx: calibrate_first_indent(STANDARD_FIRST_INDENT)}
        return {idx: 0.0}

    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2]
    para_threshold = max(median_gap * _PARA_SPACING_FACTOR, _MIN_PARA_BREAK_PX)

    # x0 threshold for paragraph break: 40pt in pixels (exceeds standard
    # 2-char indent of 32pt, so indent→continuation is not broken apart).
    _x0_break_px_fi = (40.0 / scale_x) if scale_x > 0 else float("inf")

    # Find paragraph start lines (gap-based and x0-based)
    para_starts: set[int] = {indexed_boxes[0][0]}
    for i in range(len(gaps)):
        if gaps[i] > para_threshold or x0_diffs[i] > _x0_break_px_fi:
            para_starts.add(indexed_boxes[i + 1][0])

    # Group lines into paragraphs and compute indents
    # Compute dominant body x0 for single-line indent detection
    body_x0s = [
        box[0]
        for idx, box in enumerate(rec_boxes)
        if outline_levels and outline_levels.get(idx) == "body_text"
    ]
    if not body_x0s:
        body_x0s = [box[0] for box in rec_boxes]
    body_margin_x0 = sorted(body_x0s)[len(body_x0s) // 2] if body_x0s else 0.0
    result: dict[int, float] = {}
    current_para: list[tuple[int, list[float]]] = []

    for i, (idx, box) in enumerate(indexed_boxes):
        is_para_start = idx in para_starts or not current_para
        if is_para_start and current_para:
            _compute_para_indent(
                current_para, scale_x, result, outline_levels, body_margin_x0
            )
            current_para = []
        current_para.append((idx, box))

    # Process last paragraph
    if current_para:
        _compute_para_indent(
            current_para, scale_x, result, outline_levels, body_margin_x0
        )

    return result


def _compute_para_indent(
    para_lines: list[tuple[int, list[float]]],
    scale_x: float,
    result: dict[int, float],
    outline_levels: dict[int, str] | None = None,
    body_margin_x0: float = 0.0,
) -> None:
    """Compute first-line indent for a single paragraph.

    If the paragraph has 2+ lines, compares the X start of the
    first line with the minimum X start of subsequent lines.
    Single-line body_text paragraphs fallback to the standard indent.
    Single-line non-body_text paragraphs detect indent by comparing
    their x0 to the dominant body margin x0.

    The indent is calibrated to GB/T 9704 standard (32.0pt).

    Args:
        para_lines: List of (original_index, [x0, y0, x1, y1]) tuples.
        scale_x: Conversion factor from pixels to points (X axis).
        result: Dict to update with indent values.
        outline_levels: Optional mapping from line index to outline_level.
        body_margin_x0: Dominant body text x0 position (pixels). Used to
            detect indent for single-line non-body_text paragraphs.
    """
    if len(para_lines) < 2:
        idx = para_lines[0][0]
        if outline_levels and outline_levels.get(idx) == "body_text":
            result[idx] = calibrate_first_indent(STANDARD_FIRST_INDENT)
        elif body_margin_x0 > 0:
            x0 = para_lines[0][1][0]
            indent_px = x0 - body_margin_x0
            if indent_px > 10.0:  # at least ~5pt to count as indent
                indent_pt = indent_px * scale_x
                result[idx] = calibrate_first_indent(round(indent_pt, 2))
            else:
                result[idx] = 0.0
        else:
            result[idx] = 0.0
        return

    first_x = para_lines[0][1][0]
    body_min_x = min(line[1][0] for line in para_lines[1:])

    indent_px = first_x - body_min_x
    indent_pt = indent_px * scale_x

    # Only set indent if first line starts further right, then calibrate
    raw_indent = max(0, round(indent_pt, 2))
    result[para_lines[0][0]] = calibrate_first_indent(raw_indent)

    for idx, _ in para_lines[1:]:
        if idx not in result:
            result[idx] = 0.0


def compute_left_right_indent(
    rec_boxes: list[list[float]],
    img_width: int,
    page_width_mm: float = 210.0,
    margin: Margin | None = None,
    font_sizes: dict[int, float] | None = None,
) -> dict[int, dict[str, float]]:
    """Compute left and right indent from OCR bounding boxes.

    For each paragraph, measures the horizontal distance from the left
    margin to the paragraph start and from the paragraph end to the right
    margin. Values are quantized to character-width steps (Chinese typography:
    char_width ≈ font_size_pt).

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes in pixel coords.
        img_width: Image width in pixels.
        page_width_mm: Page width in mm (default A4 = 210).
        margin: Page margins in mm. Returns all zeros if None or all-zero.
        font_sizes: Optional mapping from original line index to font size in
            pt. Used to determine character width for quantization.

    Returns:
        Dict mapping original line index to {"left_indent": pt, "right_indent": pt}.
        All lines in the same paragraph get identical values.
    """
    if not rec_boxes:
        return {}

    if margin is None or (margin.left_margin == 0.0 and margin.right_margin == 0.0):
        return {
            idx: {"left_indent": 0.0, "right_indent": 0.0}
            for idx, _ in enumerate(rec_boxes)
        }

    px_to_mm = page_width_mm / img_width if img_width > 0 else 0.0

    # Sort by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    # Single-line page
    if len(indexed_boxes) == 1:
        idx = indexed_boxes[0][0]
        left_pt, right_pt = _compute_para_left_right_indent(
            [indexed_boxes[0]],
            px_to_mm,
            margin,
            font_sizes,
        )
        return {idx: {"left_indent": left_pt, "right_indent": right_pt}}

    # x0 scale for break detection: 40pt in pixels (same as compute_first_indent)
    scale_x = A4_WIDTH_PT / img_width if img_width > 0 else 1.0
    _x0_break_px = (40.0 / scale_x) if scale_x > 0 else float("inf")

    # Detect paragraph boundaries via Y-gaps and x0 diffs
    gaps: list[float] = []
    x0_diffs: list[float] = []
    for i in range(len(indexed_boxes) - 1):
        curr_bottom = indexed_boxes[i][1][3]
        next_top = indexed_boxes[i + 1][1][1]
        gaps.append(max(0.0, next_top - curr_bottom))
        x0_diffs.append(abs(indexed_boxes[i + 1][1][0] - indexed_boxes[i][1][0]))

    if not gaps:
        idx = indexed_boxes[0][0]
        left_pt, right_pt = _compute_para_left_right_indent(
            [indexed_boxes[0]],
            px_to_mm,
            margin,
            font_sizes,
        )
        return {idx: {"left_indent": left_pt, "right_indent": right_pt}}

    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2]
    para_threshold = max(median_gap * _PARA_SPACING_FACTOR, _MIN_PARA_BREAK_PX)

    para_starts: set[int] = {indexed_boxes[0][0]}
    for i in range(len(gaps)):
        if gaps[i] > para_threshold or x0_diffs[i] > _x0_break_px:
            para_starts.add(indexed_boxes[i + 1][0])

    # Group lines into paragraphs and compute indents
    result: dict[int, dict[str, float]] = {}
    current_para: list[tuple[int, list[float]]] = []

    for i, (idx, box) in enumerate(indexed_boxes):
        is_para_start = idx in para_starts or not current_para
        if is_para_start and current_para:
            left_pt, right_pt = _compute_para_left_right_indent(
                current_para,
                px_to_mm,
                margin,
                font_sizes,
            )
            for p_idx, _ in current_para:
                result[p_idx] = {"left_indent": left_pt, "right_indent": right_pt}
            current_para = []
        current_para.append((idx, box))

    if current_para:
        left_pt, right_pt = _compute_para_left_right_indent(
            current_para,
            px_to_mm,
            margin,
            font_sizes,
        )
        for p_idx, _ in current_para:
            result[p_idx] = {"left_indent": left_pt, "right_indent": right_pt}

    # Fill any missing indices
    for i in range(len(rec_boxes)):
        if i not in result:
            result[i] = {"left_indent": 0.0, "right_indent": 0.0}

    return result


def _compute_para_left_right_indent(
    para_lines: list[tuple[int, list[float]]],
    px_to_mm: float,
    margin: Margin,
    font_sizes: dict[int, float] | None = None,
) -> tuple[float, float]:
    """Compute left and right indent for a single paragraph.

    Measures horizontal offset from margins and quantizes to
    character-width steps.

    Right indent is only reported when all lines in the paragraph
    consistently end at the same position noticeably inside the right
    margin.  Short single-line paragraphs (e.g. "密级▲长期") are
    not treated as right-indented — the gap after the text is just
    natural whitespace, not a deliberate indent.

    Args:
        para_lines: List of (original_index, [x0, y0, x1, y1]) tuples.
        px_to_mm: Conversion factor from pixels to mm.
        margin: Page margins in mm.
        font_sizes: Optional mapping from line index to font size in pt.

    Returns:
        (left_indent_pt, right_indent_pt)
    """
    char_width_pt = _get_para_char_width(para_lines, font_sizes)

    # ---- left_indent ----
    min_x0 = min(box[0] for _, box in para_lines)
    min_x_mm = min_x0 * px_to_mm
    left_indent_mm = max(0.0, min_x_mm - margin.left_margin)
    left_indent_raw_pt = left_indent_mm * MM_TO_PT
    left_pt = _quantize_to_char_width(left_indent_raw_pt, char_width_pt)

    # ---- right_indent ----
    # Right indent means the paragraph is intentionally pulled in from the
    # right margin.  This is only detectable when ALL lines consistently
    # end at roughly the same x1 position that is well inside the right
    # boundary.  A single short line does NOT imply right indent — the
    # gap after a short text is just whitespace, not an indent.
    right_boundary_mm = 210.0 - margin.right_margin
    right_pt: float = 0.0

    if len(para_lines) >= 2:
        line_x1_mm: list[float] = [box[2] * px_to_mm for _, box in para_lines]
        min_x1_mm = min(line_x1_mm)
        max_x1_mm = max(line_x1_mm)
        x1_spread_mm = max_x1_mm - min_x1_mm

        # All lines must end within one char-width of each other.
        char_width_mm = char_width_pt * PT_TO_MM
        if x1_spread_mm <= char_width_mm:
            # The consistent right edge must be at least 2 char-widths
            # inside the right boundary to qualify as a deliberate indent.
            gap_mm = right_boundary_mm - max_x1_mm
            if gap_mm >= 2.0 * char_width_mm:
                right_indent_raw_pt = gap_mm * MM_TO_PT
                right_pt = _quantize_to_char_width(right_indent_raw_pt, char_width_pt)

    return (left_pt, right_pt)


def _get_para_char_width(
    para_lines: list[tuple[int, list[float]]],
    font_sizes: dict[int, float] | None,
) -> float:
    """Determine character width for indent quantization.

    Uses median font size of paragraph lines as character width
    (Chinese typography: char_width ≈ font_size_pt).

    Falls back to 16pt when no font sizes are available.
    """
    if not font_sizes:
        return 16.0

    sizes = [font_sizes[idx] for idx, _ in para_lines if idx in font_sizes]
    if not sizes:
        return 16.0

    sizes.sort()
    return sizes[len(sizes) // 2]


def _quantize_to_char_width(raw_pt: float, char_width_pt: float) -> float:
    """Quantize indent to nearest character-width step.

    For very small indents that round to 0 steps or when char_width
    is too small, returns 0.0.
    """
    if char_width_pt <= 0:
        return 0.0
    steps = round(raw_pt / char_width_pt)
    return round(steps * char_width_pt, 2)
