"""OCR engine module for parallel OCR execution and result conversion.

Provides parallel OCR processing across pages and conversion of raw OCR results
into the internal extracted_lines format used by downstream pipeline stages.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .._retry import _call_with_retry
from ..ocr import OCRPageResult
from ..spacing import (
    compute_alignment_from_position,
    compute_font_size_from_ocr,
)

logger = logging.getLogger(__name__)

# Block label to outline_level mapping
BLOCK_LABEL_TO_OUTLINE = {
    "paragraph_title": "heading2",
    "document_title": "heading1",
    "section_header": "heading2",
    "title": "heading1",
}


def parallel_ocr(
    ocr_engine: Any,
    image_paths: list[str],
    max_workers: int,
    warnings: list[str] | None = None,
    failed_pages: list[int] | None = None,
) -> list[OCRPageResult]:
    """Call OCR engine for all pages in parallel.

    A page whose OCR call fails degrades to an empty OCRPageResult; the
    failure (page number + error) is logged and appended to *warnings*
    when a list is provided. If every page fails, the whole call raises
    RuntimeError instead of returning an all-empty result that would
    masquerade as a successfully parsed blank document.

    Args:
        ocr_engine: An OCREngine instance with a recognize() method.
        image_paths: List of image file paths.
        max_workers: Maximum concurrent requests.
        warnings: Optional collector for per-page failure messages.
        failed_pages: Optional collector for the 0-based indices of pages
            whose OCR call failed.

    Returns:
        List of OCRPageResult, one per page, in order.

    Raises:
        RuntimeError: If OCR failed for every page.
    """
    if not image_paths:
        return []

    failures = 0

    def record_failure(idx: int, exc: Exception) -> None:
        nonlocal failures
        failures += 1
        if failed_pages is not None:
            failed_pages.append(idx)
        logger.warning("OCR failed for page %d: %s", idx, exc)
        if warnings is not None:
            warnings.append(f"第 {idx + 1} 页 OCR 识别失败：{exc}")

    def recognize_with_retry(idx: int, img: str) -> OCRPageResult:
        # One retry (2 attempts total) per page: transient OCR service
        # failures get a second chance while bounding the added latency.
        return _call_with_retry(
            lambda: ocr_engine.recognize(img),
            f"OCR page {idx}",
            max_retries=1,
        )

    if len(image_paths) == 1:
        try:
            return [recognize_with_retry(0, image_paths[0])]
        except Exception as exc:
            record_failure(0, exc)
            raise RuntimeError(f"所有 1 页 OCR 识别均失败：{exc}") from exc

    n = len(image_paths)
    workers = min(n, max_workers)
    results: list[OCRPageResult | None] = [None] * n

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(recognize_with_retry, idx, img): idx
            for idx, img in enumerate(image_paths)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                record_failure(idx, exc)
                results[idx] = OCRPageResult(lines=[], width=0, height=0)

    if failures == n:
        raise RuntimeError(f"所有 {n} 页 OCR 识别均失败，无法解析该扫描文档")

    return [r if r is not None else OCRPageResult(lines=[], width=0, height=0) for r in results]


def ocr_result_to_lines(
    page_result: OCRPageResult,
    *,
    img_width: float = 0,
    img_height: float = 0,
) -> list[dict[str, Any]]:
    """Convert OCRPageResult to the internal extracted_lines format.

    Uses block outline mapping from OCR blocks for outline_level hints,
    compute_alignment_from_position for alignment, and
    compute_font_size_from_ocr for calibrated font size.

    Args:
        page_result: OCRPageResult from the OCR engine.
        img_width: Actual page image width in pixels.  When > 0 it takes
            precedence over page_result.width (the API may omit width or
            report max(x1) instead of the true image width).
        img_height: Actual page image height in pixels; same precedence
            rule as img_width.

    Returns:
        List of line dicts compatible with structure_recognizer.
    """
    if not page_result.lines:
        return []

    block_outline_map = build_block_outline_map_from_blocks(
        page_result.blocks,
    )

    page_width = img_width if img_width > 0 else page_result.width
    page_height = img_height if img_height > 0 else page_result.height

    lines: list[dict[str, Any]] = []
    for line in page_result.lines:
        text = line.text.strip()
        if not text:
            continue

        outline_level = get_outline_for_line(
            (line.x0 + line.x1) / 2,
            (line.y0 + line.y1) / 2,
            block_outline_map,
        )
        alignment = compute_alignment_from_position(
            line.x0,
            line.x1,
            page_width,
            outline_level,
        )
        font_size = compute_font_size_from_ocr(
            line.y1 - line.y0,
            page_height,
            polys=line.polys or None,
        )

        lines.append(
            {
                "text": text,
                "line_no": line.line_no,
                "x0": round(line.x0, 2),
                "y0": round(line.y0, 2),
                "x1": round(line.x1, 2),
                "y1": round(line.y1, 2),
                "font_family": "",
                "font_size": round(font_size, 1),
                "font_weight": False,
                "font_style": False,
                "confidence": round(line.confidence, 4),
                "outline_level": outline_level,
                "alignment": alignment,
                "chars": line.chars,
            }
        )

    # When OCR blocks are unavailable (new format), classify body_text
    # heuristically: lines with the dominant left-edge position and
    # sufficient text length are body text.
    if not block_outline_map and lines:
        classify_body_text_heuristic(lines)

    return lines


def classify_body_text_heuristic(lines: list[dict[str, Any]]) -> None:
    """Classify body_text lines when OCR blocks are unavailable.

    Body text is identified by: non-centered alignment, sufficient text length
    (>= 5 chars, to catch short paragraph-ending lines like "未来发展变化。"),
    and not an obvious title (font_size <= 18pt from OCR).
    """
    for line in lines:
        if line.get("outline_level") != "others":
            continue
        text = line["text"]
        if len(text) < 5:
            continue
        # Centered lines are titles/headings, not body text.
        if line.get("alignment") == "center":
            continue
        # Large OCR font size indicates a title, not body text.
        if line.get("font_size", 0) > 18:
            continue
        line["outline_level"] = "body_text"


def build_block_outline_map_from_blocks(
    blocks: list,
) -> list[tuple[float, float, float, float, str]]:
    """Build outline mapping from OCRBlock list.

    Args:
        blocks: List of OCRBlock objects.

    Returns:
        List of (x0, y0, x1, y1, outline_level) tuples.
    """
    result: list[tuple[float, float, float, float, str]] = []
    for block in blocks:
        outline = BLOCK_LABEL_TO_OUTLINE.get(block.label, "others")
        result.append((block.x0, block.y0, block.x1, block.y1, outline))
    return result


def get_outline_for_line(
    cx: float,
    cy: float,
    block_outline_map: list[tuple[float, float, float, float, str]],
) -> str:
    """Find the outline_level for a line based on which block it falls in."""
    for x0, y0, x1, y1, outline in block_outline_map:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return outline
    return "others"
