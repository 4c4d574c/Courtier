"""PDF parser with rule engine + parallel page processing.

Optimizations over original:
- Rule engine classifies all pages
- Pages processed in parallel (ThreadPoolExecutor)
- Cross-page structure inference (middle pages = body text only)
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import fitz

from docmodels import (
    Document,
    Margin,
    Page,
    PageContent,
)

from .base import ParserConfig
from .rules import StructureRuleEngine
from .structure_recognizer import (
    _classified_lines_to_page_content,
)

logger = logging.getLogger(__name__)

from ._constants import PT_TO_MM as _PT_TO_MM  # noqa: E402


class PdfParser:
    """Parser for text-based PDF files using PyMuPDF + rule engine."""

    def supports(self, file_path: str) -> bool:
        """Check if file is a PDF."""
        return Path(file_path).suffix.lower() == ".pdf"

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        """Parse a text-based PDF into the Document model.

        Uses parallel page extraction and rule engine classification.

        Args:
            file_path: Path to the PDF file.
            config: Parser configuration.

        Returns:
            Parsed Document model.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        doc = fitz.open(file_path)
        try:
            file_bytes = path.read_bytes()
            doc_id = hashlib.sha256(file_bytes).hexdigest()
            total_pages = len(doc)

            # Phase 1: Extract all pages in parallel
            page_data_list = _extract_all_pages_parallel(doc)

            # Phase 2: Classify all pages with rule engine
            engine = StructureRuleEngine()
            rule_results = []

            for page_idx, pd in enumerate(page_data_list):
                if not pd["lines"]:
                    rule_results.append((page_idx, PageContent(margin=pd["margin"])))
                    continue

                result = engine.classify_lines(
                    pd["lines"],
                    has_position=True,
                    page_height=effective_config.a4_height_pt,
                )

                page_content = _classified_lines_to_page_content(
                    result.lines, pd["lines"], pd["margin"]
                )
                rule_results.append((page_idx, page_content))
                logger.info(
                    "Page %d: rule engine classified (%d lines)",
                    page_idx,
                    len(result.lines),
                )

            # Phase 3: Assemble final document
            all_results = rule_results
            all_results.sort(key=lambda x: x[0])

            pages = []
            for page_idx, page_content in all_results:
                pd = page_data_list[page_idx]
                raw = _page_to_raw_bytes(doc[page_idx])
                pages.append(
                    Page(
                        raw=raw,
                        page_content=page_content,
                        save_path=str(path.absolute()),
                        page_no=page_idx,
                    )
                )

            return Document(
                doc_id=doc_id,
                total_page_num=total_pages,
                save_path=str(path.absolute()),
                pages=pages,
            )
        finally:
            doc.close()


def _extract_all_pages_parallel(
    doc: fitz.Document,
) -> list[dict[str, Any]]:
    """Extract text, margin, and image path for all pages.

    Uses ThreadPoolExecutor for parallel extraction.
    PyMuPDF read-only operations are thread-safe.
    """
    total = len(doc)
    if total <= 2:
        return [_extract_single_page(doc, i) for i in range(total)]

    results: list[dict[str, Any] | None] = [None] * total

    with ThreadPoolExecutor(max_workers=min(total, 4)) as executor:
        futures = {
            executor.submit(_extract_single_page, doc, i): i for i in range(total)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return [r or {} for r in results]


def _extract_single_page(
    doc: fitz.Document,
    page_idx: int,
) -> dict[str, Any]:
    """Extract data from a single page."""
    page = doc[page_idx]
    lines = _extract_page_lines(page)
    margin = _extract_margin(page)
    return {"lines": lines, "margin": margin}


def _extract_page_lines(page: fitz.Page) -> list[dict[str, Any]]:
    """Extract text lines with position and font info from a PDF page."""
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    lines: list[dict[str, Any]] = []
    line_no = 0

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line_data in block.get("lines", []):
            line_bbox = line_data.get("bbox", (0, 0, 0, 0))
            spans = line_data.get("spans", [])

            if not spans:
                continue

            combined_text = ""
            font_family = ""
            font_size = 0.0
            font_weight = False
            font_style = False

            for span in spans:
                combined_text += span.get("text", "")
                if not font_family:
                    font_family = span.get("font", "")
                size = round(span.get("size", 0.0), 1)
                if not font_size and size:
                    font_size = size
                font_weight = font_weight or "bold" in span.get("font", "").lower()
                font_style = font_style or "italic" in span.get("font", "").lower()

            text = combined_text.strip()
            if not text:
                continue

            lines.append(
                {
                    "text": text,
                    "line_no": line_no,
                    "x0": round(line_bbox[0], 2),
                    "y0": round(line_bbox[1], 2),
                    "x1": round(line_bbox[2], 2),
                    "y1": round(line_bbox[3], 2),
                    "font_family": font_family,
                    "font_size": font_size,
                    "font_weight": font_weight,
                    "font_style": font_style,
                }
            )
            line_no += 1

    return lines


def _extract_margin(page: fitz.Page) -> Margin:
    """Extract page margins from text blocks, converting points to mm.

    PyMuPDF returns coordinates in points. Margins are converted to mm
    using the standard 25.4/72 conversion factor.

    Returns Margin with all-zero values when the page has no text blocks.
    """
    rect = page.rect
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    min_x = rect.width
    min_y = rect.height
    max_x = 0.0
    max_y = 0.0

    has_text = False
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        bbox = block.get("bbox", (0, 0, 0, 0))
        min_x = min(min_x, bbox[0])
        min_y = min(min_y, bbox[1])
        max_x = max(max_x, bbox[2])
        max_y = max(max_y, bbox[3])
        has_text = True

    if not has_text:
        return Margin()

    return Margin(
        top_margin=round(min_y * _PT_TO_MM, 2),
        bottom_margin=round((rect.height - max_y) * _PT_TO_MM, 2),
        left_margin=round(min_x * _PT_TO_MM, 2),
        right_margin=round((rect.width - max_x) * _PT_TO_MM, 2),
    )


def _page_to_raw_bytes(page: fitz.Page) -> bytes:
    """Render page to PNG bytes for the raw field."""
    try:
        pix = page.get_pixmap(dpi=150)
        return pix.tobytes("png")
    except Exception:
        logger.warning("Failed to render page %d to image", page.number)
        return b""
