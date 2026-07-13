from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from docmodels import Document

from ._constants import IMAGE_EXTENSIONS
from .base import ParserConfig

if TYPE_CHECKING:
    from .base import Parser

logger = logging.getLogger(__name__)

PDF_EXTENSIONS = frozenset({".pdf"})

DOCX_EXTENSIONS = frozenset({".docx"})


def _file_extension(file_path: str) -> str:
    """Extract lowercase file extension."""
    return Path(file_path).suffix.lower()


def _is_scanned_pdf(file_path: str) -> bool:
    """Detect if a PDF is scanned (image-only) vs text-based.

    A PDF is considered scanned if all pages contain only images
    and no extractable text.
    """
    try:
        import fitz

        doc = fitz.open(file_path)
    except Exception:
        logger.warning("Cannot open PDF to check if scanned: %s", file_path)
        return False

    try:
        for page in doc:
            text = page.get_text("text").strip()
            if text:
                return False
        return True
    finally:
        doc.close()


def get_parser(file_path: str) -> Parser:
    """Get the appropriate parser for a file.

    Args:
        file_path: Path to the document file.

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
        if _is_scanned_pdf(file_path):
            from .scanned import ScannedParser

            return ScannedParser()
        from .pdf_parser import PdfParser

        return PdfParser()

    supported = PDF_EXTENSIONS | DOCX_EXTENSIONS | IMAGE_EXTENSIONS
    raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(supported)}")


def parse(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a document file into the Document model.

    Auto-detects file type and selects the appropriate parser.

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
    parser = get_parser(file_path)
    return parser.parse(file_path, effective_config)
