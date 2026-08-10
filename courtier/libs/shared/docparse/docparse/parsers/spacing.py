"""Improved spacing, indent, and margin calculation for scanned documents.

Provides shared paragraph segmentation (segment_paragraphs), margin
computation with GB/T 9704 calibration, paragraph spacing analysis, and
first-line indent detection. All functions operate on OCR bounding box
data in pixel coordinates and convert to standard units (mm, pt) with
calibration to the GB/T 9704-2012 standard where applicable.
"""

from __future__ import annotations

from docmodels import Margin

from ._constants import MM_TO_PT, PT_TO_MM
from .calibration import (
    STANDARD_FIRST_INDENT,
    calibrate_first_indent,
    calibrate_line_spacing,
    calibrate_margin,
    compute_font_size,
)

# A4 page width in points (72 DPI)
A4_WIDTH_PT: float = 595.28


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


def segment_paragraphs(
    rec_boxes: list[list[float]],
    *,
    img_width: float = 0,
    a4_width_pt: float = A4_WIDTH_PT,
) -> list[list[int]]:
    """Group text lines into paragraphs — the shared segmentation used by
    the spacing/indent metrics below (previously each had its own copy
    with slightly different thresholds).

    Lines are sorted by Y coordinate; a paragraph break is declared
    between consecutive lines when either:
    - the bottom-to-top gap exceeds the adaptive threshold: 1.8x the
      dominant gap cluster center (histogram clustering via
      compute_body_line_spacing, median fallback), with a +5px floor; or
    - the x0 positions differ by more than 40pt converted to pixels
      (e.g. attachment-item lists).  40pt exceeds the standard 2-char
      indent (32pt) with OCR jitter margin, so a normal
      indent→continuation transition is not broken apart.  Disabled
      when img_width <= 0.

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_width: Image width in pixels; <= 0 disables x0-based breaks.
        a4_width_pt: A4 page width in points (for the 40pt→px conversion).

    Returns:
        List of paragraphs in Y order; each paragraph is a list of
        original line indices in Y order.  Empty input → []; a single
        line → one single-line paragraph.
    """
    if not rec_boxes:
        return []

    # Sort boxes by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    if len(indexed_boxes) == 1:
        return [[indexed_boxes[0][0]]]

    scale_x = a4_width_pt / img_width if img_width > 0 else 1.0
    x0_break_px = 40.0 / scale_x if img_width > 0 else float("inf")

    gaps_px: list[float] = []
    x0_diffs: list[float] = []
    for i in range(len(indexed_boxes) - 1):
        gaps_px.append(max(0.0, indexed_boxes[i + 1][1][1] - indexed_boxes[i][1][3]))
        x0_diffs.append(abs(indexed_boxes[i + 1][1][0] - indexed_boxes[i][1][0]))

    # Adaptive threshold: dominant cluster of bottom-to-top gaps (gaps
    # exclude line height, making paragraph breaks easier to distinguish
    # from inter-line gaps).
    cluster_center_px, _ = compute_body_line_spacing(gaps_px)
    if cluster_center_px <= 0.0:
        sorted_gaps_px = sorted(gaps_px)
        cluster_center_px = sorted_gaps_px[len(sorted_gaps_px) // 2]
    para_threshold_px = max(cluster_center_px * 1.8, cluster_center_px + 5.0)

    paragraphs: list[list[int]] = [[indexed_boxes[0][0]]]
    for i in range(len(gaps_px)):
        next_idx = indexed_boxes[i + 1][0]
        if gaps_px[i] > para_threshold_px or x0_diffs[i] > x0_break_px:
            paragraphs.append([next_idx])
        else:
            paragraphs[-1].append(next_idx)

    return paragraphs


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
) -> dict[int, dict[str, float | None]]:
    """Compute spacing between text lines with paragraph detection.

    Uses histogram-based clustering to find the dominant body line spacing;
    paragraph breaks come from the shared segment_paragraphs() segmentation
    (adaptive gap threshold + x0-jump detection). Body text within a
    paragraph gets uniform line spacing.

    space_before is always None: a scanned line's space_before cannot be
    measured (the visual gap above the line is the normal line gap plus
    whatever the previous paragraph booked as space_after — writing a
    hardcoded 0.0 would misreport compliant blocks whose spec requires
    space_before > 0, e.g. title).  None = 未测得, honestly skipped.
    space_after of the page's last line is None for the same reason
    (no next line on this page).  line_spacing of a single-line page is
    likewise None: line spacing is the distance between two lines and a
    page with only one line has nothing to measure — a hardcoded 0.0
    would be compared against the rule value (28.95pt) and false-positive.

    Args:
        rec_boxes: List of [x0, y0, x1, y1] bounding boxes.
        img_height: Image height in pixels.
        a4_height_pt: A4 page height in points.
        img_width: Image width in pixels (optional). If provided, enables
            x0-based paragraph break detection.

    Returns:
        Dict mapping original line index to spacing info:
        {line_index: {"space_before": pt | None, "space_after": pt | None,
                      "line_spacing": pt | None}}
    """
    if len(rec_boxes) < 2:
        # Single-line page: line spacing is unmeasurable (needs two
        # lines) — None (未测得), not a hardcoded 0.0.
        return {0: {"space_before": None, "space_after": None, "line_spacing": None}}

    scale_y = a4_height_pt / img_height if img_height > 0 else 1.0

    # Sort boxes by Y coordinate, keeping original indices
    indexed_boxes = list(enumerate(rec_boxes))
    indexed_boxes.sort(key=lambda x: x[1][1])

    # Compute bottom-to-top gaps (for space_after measurement) and
    # top-to-top distances (for line spacing).
    gaps: list[tuple[int, float, float]] = []  # (idx, gap_pt, curr_height_pt)
    top_distances_px: list[float] = []
    for i in range(len(indexed_boxes) - 1):
        curr_idx = indexed_boxes[i][0]
        curr_bottom = indexed_boxes[i][1][3]
        curr_top = indexed_boxes[i][1][1]
        next_top = indexed_boxes[i + 1][1][1]
        gap_px = max(0.0, next_top - curr_bottom)
        gap_pt = gap_px * scale_y
        curr_height_pt = max(0.0, curr_bottom - curr_top) * scale_y
        gaps.append((curr_idx, gap_pt, curr_height_pt))
        top_distances_px.append(max(0.0, next_top - curr_top))

    # Line spacing: cluster top-to-top distances (true center-to-center spacing)
    top_cluster_center_px, _ = compute_body_line_spacing(top_distances_px)

    if top_cluster_center_px <= 0.0:
        sorted_top = sorted(top_distances_px)
        top_cluster_center_px = sorted_top[len(sorted_top) // 2]

    top_cluster_center_pt = top_cluster_center_px * scale_y
    calibrated_line_spacing = calibrate_line_spacing(top_cluster_center_pt)

    # Paragraph starts from the shared segmentation; a gap is a paragraph
    # break iff the next line starts a new paragraph.
    para_starts: set[int] = {para[0] for para in segment_paragraphs(rec_boxes, img_width=img_width)}

    result: dict[int, dict[str, float | None]] = {}
    for i, (idx, gap_pt, curr_height_pt) in enumerate(gaps):
        is_break = indexed_boxes[i + 1][0] in para_starts

        # Only set space_after for paragraph breaks, and subtract the
        # normal intra-paragraph visual gap (line_spacing - text_height)
        # that is already produced by Word's EXACTLY line spacing rule.
        if is_break:
            normal_visual_gap = max(0.0, calibrated_line_spacing - curr_height_pt)
            space_after = max(0.0, gap_pt - normal_visual_gap)
        else:
            space_after = 0.0

        result[idx] = {
            "space_before": None,
            "space_after": round(space_after, 2),
            "line_spacing": round(calibrated_line_spacing, 2),
        }

    # Handle last line: no next line on this page, so space_after is
    # unmeasurable — None (未测得), not a guessed 0.0.
    if indexed_boxes:
        last_idx = indexed_boxes[-1][0]
        if last_idx not in result:
            result[last_idx] = {
                "space_before": None,
                "space_after": None,
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

    Paragraphs come from the shared segment_paragraphs() segmentation;
    the first line's X position is compared with the body X position,
    and the indent is calibrated to the GB/T 9704 standard (32.0pt).
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

    scale_x = a4_width_pt / img_width if img_width > 0 else 1.0

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
    for para_indices in segment_paragraphs(
        rec_boxes,
        img_width=img_width,
        a4_width_pt=a4_width_pt,
    ):
        para_lines = [(idx, rec_boxes[idx]) for idx in para_indices]
        _compute_para_indent(para_lines, scale_x, result, outline_levels, body_margin_x0)

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
        return {idx: {"left_indent": 0.0, "right_indent": 0.0} for idx, _ in enumerate(rec_boxes)}

    px_to_mm = page_width_mm / img_width if img_width > 0 else 0.0

    # Group lines into paragraphs via the shared segmentation and compute
    # per-paragraph indents.
    result: dict[int, dict[str, float]] = {}
    for para_indices in segment_paragraphs(rec_boxes, img_width=img_width):
        para_lines = [(idx, rec_boxes[idx]) for idx in para_indices]
        left_pt, right_pt = _compute_para_left_right_indent(
            para_lines,
            px_to_mm,
            margin,
            font_sizes,
        )
        for p_idx, _ in para_lines:
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
