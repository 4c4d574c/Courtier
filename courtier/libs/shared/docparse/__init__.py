"""docparse - Parse Chinese government official documents into structured data models.

Supports:
- PDF files (text-based and scanned)
- DOCX files
- Image files (PNG, JPG, TIFF, BMP)
"""

from docmodels import (
    Body,
    Document,
    Font,
    Footer,
    Header,
    Margin,
    MetaData,
    Page,
    PageContent,
    Paragraph,
    Position,
)
from docparse.parsers import ParserConfig
from docparse.parsers.registry import parse

__all__ = [
    # Public API
    "parse",
    "ParserConfig",
    # Models
    "Body",
    "Document",
    "Font",
    "Footer",
    "Header",
    "Margin",
    "MetaData",
    "Page",
    "PageContent",
    "Paragraph",
    "Position",
]


def parse_pdf(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a PDF file into the Document model.

    Args:
        file_path: Path to the PDF file.
        config: Parser configuration. Uses defaults/env if None.

    Returns:
        Parsed Document model.
    """
    from docparse.parsers.pdf_parser import PdfParser

    effective_config = config or ParserConfig.from_env()
    return PdfParser().parse(file_path, effective_config)


def parse_docx(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a DOCX file into the Document model.

    Args:
        file_path: Path to the DOCX file.
        config: Parser configuration. Uses defaults/env if None.

    Returns:
        Parsed Document model.
    """
    from docparse.parsers.docx_parser import DocxParser

    effective_config = config or ParserConfig.from_env()
    return DocxParser().parse(file_path, effective_config)


def parse_scanned(file_path: str, config: ParserConfig | None = None) -> Document:
    """Parse a scanned document (image or scanned PDF) into the Document model.

    Uses PaddleOCR for text extraction with position info, then LLM
    for structure recognition.

    Args:
        file_path: Path to the image or scanned PDF file.
        config: Parser configuration. Uses defaults/env if None.

    Returns:
        Parsed Document model.
    """
    from docparse.parsers.scanned import ScannedParser

    effective_config = config or ParserConfig.from_env()
    return ScannedParser().parse(file_path, effective_config)
