"""PPStructureV3 OCR engine adapter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from .base import OCRLineResult, OCRPageResult

logger = logging.getLogger(__name__)

# Upload MIME type by image file extension — the OCR service validates
# the declared content type, so a JPEG/TIFF/BMP original must not be
# uploaded with a hardcoded image/png header.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


class PPStructureAdapter:
    """Adapter for the PPStructureV3 OCR API.

    Converts PPStructureV3 JSON responses into the standard
    OCRPageResult / OCRLineResult data model.

    Supports two response formats:
    - Legacy: fields nested under overall_ocr_res, with width/height
      and parsing_res_list at top level.
    - Current: rec_texts/rec_scores/rec_boxes/rec_polys at top level,
      with optional text_word/text_word_boxes for word-level data.
    """

    def __init__(self, api_url: str) -> None:
        self._api_url = api_url

    def recognize(self, image_path: str) -> OCRPageResult:
        """Call PPStructureV3 API and return structured OCR results.

        Args:
            image_path: Path to the image file to OCR.

        Returns:
            OCRPageResult with recognized text and layout.

        Raises:
            FileNotFoundError: If image_path does not exist.
            RuntimeError: If the API call fails or the response is not
                valid JSON (e.g. a gateway error page).
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        mime_type = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
        try:
            with open(image_path, "rb") as f:
                filename = path.name
                response = requests.post(
                    self._api_url,
                    files={"file": (filename, f, mime_type)},
                    timeout=120,
                )
            response.raise_for_status()
            json_response = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"PPStructureV3 API call failed: {exc}") from exc
        except ValueError as exc:
            # requests raises JSONDecodeError (a ValueError) when the body
            # is not JSON — e.g. an HTML gateway error page.
            raise RuntimeError(f"PPStructureV3 API returned a non-JSON response: {exc}") from exc

        results = json_response.get("results", [])
        if not results:
            return OCRPageResult(lines=[], width=0, height=0)

        return self.parse_response(results[0])

    def parse_response(self, response: dict[str, Any]) -> OCRPageResult:
        """Convert PPStructureV3 JSON to OCRPageResult.

        Args:
            response: A single page result dict from PPStructureV3 API.

        Returns:
            Structured OCRPageResult with lines and blocks.
        """
        if not response:
            return OCRPageResult(lines=[], width=0, height=0)

        lines = _parse_lines(response)

        width = response.get("width")
        height = response.get("height")
        if width is None or height is None:
            width, height = _compute_dimensions(lines)

        return OCRPageResult(
            lines=lines,
            width=width,
            height=height,
            blocks=[],
            raw=response,
        )


def _get_ocr_arrays(page_data: dict[str, Any]) -> tuple[list, list, list, list, list, list]:
    """Extract OCR arrays from the current PPStructureV3 response format."""
    rec_texts = page_data.get("rec_texts", [])
    rec_scores = page_data.get("rec_scores", [])
    rec_boxes = page_data.get("rec_boxes", [])
    rec_polys = page_data.get("rec_polys", [])
    text_word = page_data.get("text_word", [])
    text_word_boxes = page_data.get("text_word_boxes", [])
    return rec_texts, rec_scores, rec_boxes, rec_polys, text_word, text_word_boxes


def _parse_lines(page_data: dict[str, Any]) -> list[OCRLineResult]:
    """Extract OCRLineResult list from OCR response.

    Reads rec_texts, rec_scores, rec_boxes, rec_polys arrays
    and combines them into OCRLineResult objects.

    When text_word and text_word_boxes are available,
    populates the chars field with word-level data.
    """
    rec_texts, rec_scores, rec_boxes, rec_polys, text_word, text_word_boxes = _get_ocr_arrays(
        page_data
    )

    if not rec_texts:
        return []

    lines: list[OCRLineResult] = []
    for idx in range(len(rec_texts)):
        text = rec_texts[idx].strip()
        if not text:
            continue

        # Bounding box
        box = rec_boxes[idx] if idx < len(rec_boxes) else [0, 0, 0, 0]
        x0, y0, x1, y1 = float(box[0]), float(box[1]), float(box[2]), float(box[3])

        # Polys override bbox for more precise quadrilateral
        polys: list[tuple[float, float]] = []
        if idx < len(rec_polys):
            poly = rec_polys[idx]
            polys = [(float(p[0]), float(p[1])) for p in poly]
            if polys:
                xs = [p[0] for p in polys]
                ys = [p[1] for p in polys]
                x0 = min(xs)
                y0 = min(ys)
                x1 = max(xs)
                y1 = max(ys)

        confidence = float(rec_scores[idx]) if idx < len(rec_scores) else 0.0

        # Word-level data
        chars: list[dict[str, Any]] = _parse_word_data(
            idx,
            text_word,
            text_word_boxes,
        )

        lines.append(
            OCRLineResult(
                text=text,
                line_no=idx,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                polys=polys,
                confidence=confidence,
                chars=chars,
            )
        )

    # Merge OCR results that belong to the same text line.
    # Items whose Y-center distance is within one char-height are considered
    # on the same physical row.  Within a row, adjacent items (X-gap ≤ char-width)
    # are concatenated directly (character-level merge); items separated by a
    # larger gap stay as separate lines — same-row merging is handled downstream
    # by the document builder.
    if lines:
        lines = _merge_same_line_items(lines)

    return lines


def _merge_same_line_items(
    lines: list[OCRLineResult],
) -> list[OCRLineResult]:
    """Merge OCR items that belong to the same physical text row.

    Items whose Y-center distance is within one char-height are grouped
    onto the same row.  Within a row, adjacent items whose X-gap is ≤ one
    char-width are concatenated directly (character-level merge); items
    farther apart stay as separate lines.  The document builder later
    merges those separate same-row lines into a single paragraph with
    appropriate tab stops.

    Returns:
        Merged OCRLineResult objects, sorted top-to-bottom by Y position.
        Items that are close together (≤ char-width X-gap) on the same row
        are concatenated into one line; items farther apart stay as separate
        lines — their same-row relationship is resolved by the builder.
    """
    if len(lines) < 2:
        return lines

    # Char-height: median item height is a proxy for font size.
    heights = sorted(line.y1 - line.y0 for line in lines if (line.y1 - line.y0) > 0)
    char_height = heights[len(heights) // 2] if heights else 10.0

    # Char-width: median item width among short (≤2 char) items, falling
    # back to char_height when most items are full-length text lines.
    short_widths = sorted(
        line.x1 - line.x0 for line in lines if 0 < (line.x1 - line.x0) <= char_height * 2.5
    )
    if short_widths:
        char_width = short_widths[len(short_widths) // 2]
    else:
        char_width = char_height

    max_x_gap = char_width * 1.0

    # --- Pass 1: group by Y-center proximity ---
    sorted_by_y = sorted(lines, key=lambda line: line.y0)
    y_groups: list[list[OCRLineResult]] = []
    current_y_group: list[OCRLineResult] = [sorted_by_y[0]]

    for line in sorted_by_y[1:]:
        group_centers = [(line.y0 + line.y1) / 2 for line in current_y_group]
        avg_center_y = sum(group_centers) / len(group_centers)
        line_center_y = (line.y0 + line.y1) / 2

        if abs(line_center_y - avg_center_y) <= char_height:
            current_y_group.append(line)
        else:
            y_groups.append(current_y_group)
            current_y_group = [line]

    y_groups.append(current_y_group)

    # --- Pass 2: within each Y group, merge close items into blocks.
    # Well-separated blocks stay as independent lines — same-row merging
    # of different semantic fields is handled by the document builder.
    merged: list[OCRLineResult] = []
    for y_group in y_groups:
        if len(y_group) == 1:
            merged.append(y_group[0])
            continue

        x_sorted = sorted(y_group, key=lambda line: line.x0)
        blocks: list[list[OCRLineResult]] = []
        current_block: list[OCRLineResult] = [x_sorted[0]]

        for item in x_sorted[1:]:
            prev_x1 = current_block[-1].x1
            gap = item.x0 - prev_x1
            if gap <= max_x_gap:
                current_block.append(item)
            else:
                blocks.append(current_block)
                current_block = [item]

        blocks.append(current_block)

        for block in blocks:
            merged.append(_merge_line_group(block, sep=""))

    # Sort by Y position (top-to-bottom) to maintain physical reading order
    # after merging.  line_no alone is unreliable because merged items
    # absorb the line_no values of their constituents.
    merged.sort(key=lambda line: line.y0)
    return merged


def _merge_line_group(
    group: list[OCRLineResult],
    sep: str = "",
) -> OCRLineResult:
    """Merge a group of same-text-line OCR results into one.

    Text is joined in X order (left-to-right) using *sep*.
    Bounding box is the union of all group members.
    """
    if len(group) == 1:
        return group[0]

    # Sort by X for correct left-to-right text order
    sorted_group = sorted(group, key=lambda line: line.x0)

    merged_text = sep.join(line.text for line in sorted_group)
    x0 = min(line.x0 for line in sorted_group)
    y0 = min(line.y0 for line in sorted_group)
    x1 = max(line.x1 for line in sorted_group)
    y1 = max(line.y1 for line in sorted_group)

    merged_polys: list[tuple[float, float]] = []
    for line in sorted_group:
        if line.polys:
            merged_polys.extend(line.polys)

    merged_chars: list[dict[str, Any]] = []
    for line in sorted_group:
        if line.chars:
            merged_chars.extend(line.chars)

    avg_confidence = sum(line.confidence for line in sorted_group) / len(sorted_group)

    first = sorted_group[0]
    return OCRLineResult(
        text=merged_text,
        line_no=first.line_no,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        polys=merged_polys,
        confidence=round(avg_confidence, 4),
        chars=merged_chars,
    )


def _join_blocks_with_tabs(
    blocks: list[OCRLineResult],
    char_width: float = 16.0,
) -> OCRLineResult:
    """Join multiple X-blocks on the same Y row with tab separators.

    Each *block* represents a cluster of OCR items that were close together
    horizontally.  The number of tab characters between consecutive blocks
    is proportional to the X-gap measured in char-width units, so the
    horizontal spacing reflects the original layout (e.g. left-aligned
    classification label and right-aligned issuing number).
    """
    if len(blocks) == 1:
        return blocks[0]

    # Sort by X for correct left-to-right order
    sorted_blocks = sorted(blocks, key=lambda b: b.x0)

    # Build text with tab count proportional to inter-block gap.
    # Each tab represents one char-width of horizontal space.
    parts: list[str] = [sorted_blocks[0].text]
    for i in range(1, len(sorted_blocks)):
        prev_x1 = sorted_blocks[i - 1].x1
        curr_x0 = sorted_blocks[i].x0
        gap_px = max(0.0, curr_x0 - prev_x1)
        num_tabs = max(1, round(gap_px / char_width)) if char_width > 0 else 1
        parts.append("\t" * num_tabs + sorted_blocks[i].text)

    merged_text = "".join(parts)
    x0 = min(b.x0 for b in sorted_blocks)
    y0 = min(b.y0 for b in sorted_blocks)
    x1 = max(b.x1 for b in sorted_blocks)
    y1 = max(b.y1 for b in sorted_blocks)

    merged_polys: list[tuple[float, float]] = []
    for b in sorted_blocks:
        if b.polys:
            merged_polys.extend(b.polys)

    merged_chars: list[dict[str, Any]] = []
    for b in sorted_blocks:
        if b.chars:
            merged_chars.extend(b.chars)

    avg_confidence = sum(b.confidence for b in sorted_blocks) / len(sorted_blocks)

    first = sorted_blocks[0]
    return OCRLineResult(
        text=merged_text,
        line_no=first.line_no,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        polys=merged_polys,
        confidence=round(avg_confidence, 4),
        chars=merged_chars,
    )


def _parse_word_data(
    idx: int,
    text_word: list[list[str]],
    text_word_boxes: list[list[list[float]]],
) -> list[dict[str, Any]]:
    """Parse word-level character groups and boxes for a single line."""
    if idx >= len(text_word) or idx >= len(text_word_boxes):
        return []

    words = text_word[idx]
    word_boxes = text_word_boxes[idx]
    chars: list[dict[str, Any]] = []

    for w_idx in range(min(len(words), len(word_boxes))):
        wb = word_boxes[w_idx]
        if len(wb) < 4:
            continue
        word_text = words[w_idx]
        chars.append(
            {
                "text": "".join(word_text) if isinstance(word_text, list) else str(word_text),
                "x0": float(wb[0]),
                "y0": float(wb[1]),
                "x1": float(wb[2]),
                "y1": float(wb[3]),
            }
        )

    return chars


def _compute_dimensions(lines: list[OCRLineResult]) -> tuple[int, int]:
    """Derive page dimensions from bounding boxes when not in response."""
    if not lines:
        return 0, 0
    width = int(max(line.x1 for line in lines))
    height = int(max(line.y1 for line in lines))
    return width, height
