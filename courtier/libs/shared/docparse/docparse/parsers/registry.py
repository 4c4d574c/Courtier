from __future__ import annotations

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
    - Mixed text/scanned pages → "scanned" when OCR is available
      (llm_api_key configured) so the scanned pages get recognized;
      otherwise "pdf", with the textless pages reported via
      Document.warnings instead of silently producing empty content.
    """
    if textless_count == 0:
        return "pdf"
    if textless_count == total:
        return "scanned"
    return "scanned" if ocr_available else "pdf"


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
        kind = _pdf_dispatch(
            len(_textless_pages(extraction)),
            len(extraction.pages),
            ocr_available=bool((config or ParserConfig.from_env()).llm_api_key),
        )
        if kind == "scanned":
            from .scanned import ScannedParser

            return ScannedParser()
        return PdfParser()

    supported = PDF_EXTENSIONS | DOCX_EXTENSIONS | IMAGE_EXTENSIONS
    raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(supported)}")


def parse(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a document file into the Document model.

    Auto-detects file type and selects the appropriate parser. For PDFs,
    per-page text detection runs once and the extraction is reused by
    PdfParser (no double extraction). ``Document.source`` is backfilled
    with the pipeline actually used ("docx" / "pdf" / "scanned").

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
    documents go through the scanned pipeline when OCR is available;
    otherwise they stay with PdfParser and the unrecognized pages are
    listed in Document.warnings. ``Document.source`` records the pipeline
    actually used ("scanned" for mixed documents dispatched to
    ScannedParser).
    """
    from .pdf_parser import PdfParser, extract_pdf_pages

    extraction = extract_pdf_pages(file_path)
    kind = _pdf_dispatch(
        len(_textless_pages(extraction)),
        len(extraction.pages),
        ocr_available=bool(config.llm_api_key),
    )
    if kind == "scanned":
        from .scanned import ScannedParser

        document = ScannedParser().parse(file_path, config)
        document.source = "scanned"
        return document
    document = PdfParser().parse(file_path, config, extraction=extraction)
    document.source = "pdf"
    return document
