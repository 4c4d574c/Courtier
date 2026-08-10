"""PDF parser with rule engine + parallel page processing.

- Rule engine classifies all pages.
- Pages are extracted in parallel (ThreadPoolExecutor) with per-page fault
  tolerance: a failing page degrades to empty content plus a warning instead
  of aborting the whole document; only a total failure raises.
- Each page's text dict is fetched exactly once and shared by line and
  margin extraction.
- extract_pdf_pages() exposes the same extraction to the registry so
  scanned/mixed-PDF detection and the actual parse share a single pass.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    _doc_typical_line_gap,
    _estimate_spacing_from_positions,
)

logger = logging.getLogger(__name__)

from ._constants import PT_TO_MM as _PT_TO_MM  # noqa: E402


@dataclass
class PdfPageExtraction:
    """Precomputed per-page extraction shared by detection and parsing."""

    # One {"lines": [...], "margin": Margin} entry per page; pages whose
    # extraction failed degrade to empty lines + zero margin.
    pages: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    failed_pages: list[int] = field(default_factory=list)  # 0-based page indices


def extract_pdf_pages(file_path: str) -> PdfPageExtraction:
    """Open a PDF once and extract per-page lines and margins.

    Each page's ``get_text("dict")`` result is computed exactly once and
    reused for both line and margin extraction.

    Args:
        file_path: Path to the PDF file.

    Returns:
        PdfPageExtraction with per-page data, extraction warnings, and the
        list of pages whose extraction failed.

    Raises:
        RuntimeError: If every page failed to extract.
    """
    doc = fitz.open(file_path)
    try:
        return _extract_all_pages_parallel(doc)
    finally:
        doc.close()


class PdfParser:
    """Parser for text-based PDF files using PyMuPDF + rule engine."""

    def supports(self, file_path: str) -> bool:
        """Check if file is a PDF."""
        return Path(file_path).suffix.lower() == ".pdf"

    def parse(
        self,
        file_path: str,
        config: ParserConfig | None = None,
        *,
        extraction: PdfPageExtraction | None = None,
    ) -> Document:
        """Parse a text-based PDF into the Document model.

        Uses parallel page extraction and rule engine classification.

        Args:
            file_path: Path to the PDF file.
            config: Parser configuration.
            extraction: Optional precomputed per-page extraction (e.g. from
                the registry's scanned/mixed detection) to avoid re-reading
                the document. When omitted, the PDF is extracted here.

        Returns:
            Parsed Document model. Pages that yielded no text (scanned or
            blank pages) and pages whose extraction failed are listed in
            ``Document.warnings``.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        if extraction is None:
            extraction = extract_pdf_pages(file_path)
        page_data_list = extraction.pages

        file_bytes = path.read_bytes()
        doc_id = hashlib.sha256(file_bytes).hexdigest()
        total_pages = len(page_data_list)

        warnings = list(extraction.warnings)
        failed_pages = set(extraction.failed_pages)
        for page_idx, pd in enumerate(page_data_list):
            if not pd["lines"] and page_idx not in failed_pages:
                warnings.append(
                    f"第 {page_idx + 1} 页无文本层（可能为扫描页或图片页），该页内容为空"
                )

        # Classify all pages with rule engine
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

            page_warnings: list[str] = []
            page_content = _classified_lines_to_page_content(
                result.lines, pd["lines"], pd["margin"], warnings=page_warnings
            )
            warnings.extend(f"第 {page_idx + 1} 页：{w}" for w in page_warnings)
            rule_results.append((page_idx, page_content))
            logger.info(
                "Page %d: rule engine classified (%d lines)",
                page_idx,
                len(result.lines),
            )

        # Estimate spacing/line-spacing from positions (pt) in a second
        # pass. Anything that cannot be estimated stays None (never a
        # guessed 0.0). Pages without any multi-line paragraph (cover /
        # signature pages are often all single-line) fall back to the
        # document-level median intra-paragraph line gap collected in the
        # first pass, instead of 0.0 (no line-gap subtraction → inflated
        # space_after false positives).
        doc_typical_gap = _doc_typical_line_gap([pc for _, pc in rule_results])
        for _, page_content in rule_results:
            _estimate_spacing_from_positions(page_content, doc_typical_gap)

        # Assemble final document
        rule_results.sort(key=lambda x: x[0])

        pages = []
        for page_idx, page_content in rule_results:
            pages.append(
                Page(
                    page_content=page_content,
                    page_no=page_idx,
                )
            )

        return Document(
            doc_id=doc_id,
            total_page_num=total_pages,
            save_path=str(path.absolute()),
            pages=pages,
            warnings=warnings,
        )


def _extract_all_pages_parallel(
    doc: fitz.Document,
) -> PdfPageExtraction:
    """Extract text lines and margins for all pages.

    Uses ThreadPoolExecutor for parallel extraction.
    PyMuPDF read-only operations are thread-safe.

    A page that fails to extract degrades to empty content and is recorded
    in the result's warnings/failed_pages; the whole extraction raises
    RuntimeError only when every page failed.
    """
    total = len(doc)
    results: list[dict[str, Any] | None] = [None] * total
    warnings: list[str] = []
    failed_pages: list[int] = []

    def record_failure(page_idx: int, exc: Exception) -> None:
        failed_pages.append(page_idx)
        warnings.append(f"第 {page_idx + 1} 页内容提取失败：{exc}")
        logger.warning("PDF page %d extraction failed: %s", page_idx, exc)

    if total <= 2:
        for i in range(total):
            try:
                results[i] = _extract_single_page(doc, i)
            except Exception as exc:
                record_failure(i, exc)
    else:
        with ThreadPoolExecutor(max_workers=min(total, 4)) as executor:
            futures = {executor.submit(_extract_single_page, doc, i): i for i in range(total)}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    record_failure(idx, exc)

    if total > 0 and all(r is None for r in results):
        raise RuntimeError(f"PDF 全部 {total} 页内容提取失败")

    pages = [r or {"lines": [], "margin": Margin()} for r in results]
    return PdfPageExtraction(
        pages=pages,
        warnings=warnings,
        failed_pages=sorted(failed_pages),
    )


def _extract_single_page(
    doc: fitz.Document,
    page_idx: int,
) -> dict[str, Any]:
    """Extract lines and margin from a single page.

    Calls ``get_text("dict")`` exactly once; both the line list and the
    margin are derived from the same text dict.
    """
    page = doc[page_idx]
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    lines = _extract_lines_from_dict(text_dict)
    margin = _extract_margin_from_dict(text_dict, page.rect)
    return {"lines": lines, "margin": margin}


def _extract_lines_from_dict(text_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Build text lines with position and font info from a page text dict."""
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


def _extract_margin_from_dict(text_dict: dict[str, Any], rect: fitz.Rect) -> Margin:
    """Compute page margins from a page text dict, converting points to mm.

    PyMuPDF returns coordinates in points. Margins are converted to mm
    using the standard 25.4/72 conversion factor.

    Returns Margin with all-zero values when the page has no text blocks.
    """
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
