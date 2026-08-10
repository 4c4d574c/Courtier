from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from docmodels import Document

from ._constants import IMAGE_EXTENSIONS
from .base import ParserConfig

if TYPE_CHECKING:
    from .base import Parser
    from .pdf_parser import PdfPageExtraction

logger = logging.getLogger(__name__)

PDF_EXTENSIONS = frozenset({".pdf"})

DOCX_EXTENSIONS = frozenset({".docx"})


def _file_extension(file_path: str) -> str:
    """Extract lowercase file extension."""
    return Path(file_path).suffix.lower()


def _textless_pages(extraction: PdfPageExtraction) -> list[int]:
    """0-based indices of pages with no extractable text (scanned or blank)."""
    return [i for i, pd in enumerate(extraction.pages) if not pd["lines"]]


def _pdf_dispatch(textless_count: int, total: int, ocr_available: bool) -> str:
    """Pick the parser kind for a PDF from per-page text detection.

    - All pages have text → "pdf" (PdfParser).
    - No page has text → "scanned" (ScannedParser, legacy behavior).
    - Mixed text/scanned pages with OCR available (llm_api_key
      configured) → "mixed": text pages stay on the rule-engine path
      and only the textless pages go through OCR (_parse_mixed_pdf).
    - Mixed without OCR → "pdf", with the textless pages reported via
      Document.warnings instead of silently producing empty content.
    """
    if textless_count == 0:
        return "pdf"
    if textless_count == total:
        return "scanned"
    return "mixed" if ocr_available else "pdf"


class _MixedPdfParser:
    """Parser adapter returned by get_parser for mixed text/scanned PDFs.

    Holds the probe extraction computed during dispatch and delegates to
    registry._parse_mixed_pdf, so get_parser keeps its Parser-returning
    contract for every dispatch kind without re-reading the file.
    """

    def __init__(self, extraction: PdfPageExtraction, textless_pages: list[int]) -> None:
        self._extraction = extraction
        self._textless_pages = textless_pages

    def supports(self, file_path: str) -> bool:
        return _file_extension(file_path) in PDF_EXTENSIONS

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        document = _parse_mixed_pdf(
            file_path,
            config or ParserConfig.from_env(),
            self._extraction,
            self._textless_pages,
        )
        document.source = "mixed"
        return document


def get_parser(file_path: str, config: ParserConfig | None = None) -> Parser:
    """Get the appropriate parser for a file.

    Args:
        file_path: Path to the document file.
        config: Parser configuration; only used to decide mixed-PDF
            dispatch (whether OCR is available). Uses env defaults if None.

    Returns:
        A Parser instance suitable for the file type.

    Raises:
        ValueError: If the file type is not supported.
    """
    ext = _file_extension(file_path)

    if ext in DOCX_EXTENSIONS:
        from .docx_parser import DocxParser

        return DocxParser()

    if ext in IMAGE_EXTENSIONS:
        from .scanned import ScannedParser

        return ScannedParser()

    if ext in PDF_EXTENSIONS:
        from .pdf_parser import PdfParser, extract_pdf_pages

        try:
            extraction = extract_pdf_pages(file_path)
        except Exception:
            # Undetectable (e.g. corrupt file) — defer to PdfParser, which
            # surfaces the underlying error at parse time.
            logger.warning("Cannot probe PDF pages: %s", file_path, exc_info=True)
            return PdfParser()
        textless = _textless_pages(extraction)
        kind = _pdf_dispatch(
            len(textless),
            len(extraction.pages),
            ocr_available=bool((config or ParserConfig.from_env()).llm_api_key),
        )
        if kind == "scanned":
            from .scanned import ScannedParser

            return ScannedParser()
        if kind == "mixed":
            return _MixedPdfParser(extraction, textless)
        return PdfParser()

    supported = PDF_EXTENSIONS | DOCX_EXTENSIONS | IMAGE_EXTENSIONS
    raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(supported)}")


def parse(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a document file into the Document model.

    Auto-detects file type and selects the appropriate parser. For PDFs,
    per-page text detection runs once and the extraction is reused by
    PdfParser (no double extraction). ``Document.source`` is backfilled
    with the pipeline actually used ("docx" / "pdf" / "scanned" /
    "mixed").

    Args:
        file_path: Path to the document file.
        config: Parser configuration. Uses defaults/env if None.

    Returns:
        Parsed Document model.

    Raises:
        FileNotFoundError: If file_path does not exist.
        ValueError: If the file format is not supported.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    effective_config = config or ParserConfig.from_env()
    ext = _file_extension(file_path)
    if ext in PDF_EXTENSIONS:
        return _parse_pdf(file_path, effective_config)

    parser = get_parser(file_path)
    document = parser.parse(file_path, effective_config)
    # Backfill the explicit parse source so downstream consumers (e.g. the
    # validator) can rely on it instead of the "positions all zero"
    # heuristic. Non-DOCX, non-PDF files are images → scanned pipeline.
    document.source = "docx" if ext in DOCX_EXTENSIONS else "scanned"
    return document


def _parse_pdf(file_path: str, config: ParserConfig) -> Document:
    """Parse a PDF with per-page scanned detection and mixed-document dispatch.

    Textless pages (scanned or blank) are detected page by page. Mixed
    documents are split per page via _parse_mixed_pdf when OCR is
    available; otherwise they stay with PdfParser and the unrecognized
    pages are listed in Document.warnings. ``Document.source`` records
    the pipeline actually used ("pdf" / "scanned" / "mixed").
    """
    from .pdf_parser import PdfParser, extract_pdf_pages

    extraction = extract_pdf_pages(file_path)
    textless = _textless_pages(extraction)
    kind = _pdf_dispatch(
        len(textless),
        len(extraction.pages),
        ocr_available=bool(config.llm_api_key),
    )
    if kind == "scanned":
        from .scanned import ScannedParser

        document = ScannedParser().parse(file_path, config)
        document.source = "scanned"
        return document
    if kind == "mixed":
        document = _parse_mixed_pdf(file_path, config, extraction, textless)
        document.source = "mixed"
        return document
    document = PdfParser().parse(file_path, config, extraction=extraction)
    document.source = "pdf"
    return document


def _parse_mixed_pdf(
    file_path: str,
    config: ParserConfig,
    extraction: PdfPageExtraction,
    textless_pages: list[int],
) -> Document:
    """Parse a mixed text/scanned PDF page by page.

    Text pages are classified by the rule engine straight from the
    shared *extraction* (no OCR quota spent, no re-reading the file);
    only the textless pages go through the OCR pipeline via
    ScannedParser.parse_pages.  Pages are merged back in original
    order.  The textless pages are handled by OCR here, so PdfParser's
    "无文本层…该页内容为空" notice is intentionally not emitted for
    them.  ``doc_id`` remains the file-content sha256 and
    ``total_page_num`` the full page count.
    """
    from docmodels import Page

    from .pdf_parser import classify_extracted_page
    from .scanned import ScannedParser
    from .structure_recognizer import _doc_typical_line_gap, _estimate_spacing_from_positions

    path = Path(file_path)
    warnings = list(extraction.warnings)
    textless_set = set(textless_pages)

    # Text pages: rule-engine classification from the shared extraction,
    # then the same position-based spacing second pass as PdfParser.
    text_pages: list[Page] = []
    for page_idx, pd in enumerate(extraction.pages):
        if page_idx in textless_set:
            continue
        page_content, page_warnings = classify_extracted_page(pd["lines"], pd["margin"], config)
        warnings.extend(f"第 {page_idx + 1} 页：{w}" for w in page_warnings)
        text_pages.append(Page(page_content=page_content, page_no=page_idx))

    doc_typical_gap = _doc_typical_line_gap([p.page_content for p in text_pages])
    for p in text_pages:
        _estimate_spacing_from_positions(p.page_content, doc_typical_gap)

    # Scanned pages: OCR pipeline on the textless subset only.
    scanned_pages, scanned_warnings = ScannedParser().parse_pages(file_path, textless_pages, config)
    warnings.extend(scanned_warnings)

    pages = sorted(text_pages + scanned_pages, key=lambda p: p.page_no)
    doc_id = hashlib.sha256(path.read_bytes()).hexdigest()
    return Document(
        doc_id=doc_id,
        total_page_num=len(extraction.pages),
        save_path=str(path.absolute()),
        pages=pages,
        warnings=warnings,
    )
