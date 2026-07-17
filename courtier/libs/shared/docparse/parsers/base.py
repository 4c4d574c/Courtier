from __future__ import annotations

import os
from typing import Protocol

from docmodels import Document
from pydantic import BaseModel, Field


class ParserConfig(BaseModel):
    """Configuration for document parsers."""

    # Multimodal LLM (for structure recognition with images)
    llm_base_url: str = Field(default="", description="Multimodal LLM API base URL")
    llm_api_key: str = Field(default="", description="API key for LLM service")
    llm_model: str = Field(default="", description="Model name for LLM service")

    # PPStructureV3 OCR API
    ocr_api_url: str = Field(default="", description="PPStructureV3 API endpoint URL")
    ocr_lang: str = Field(default="ch", description="OCR language (ch for Chinese)")
    ocr_engine: str = Field(default="ppstructure", description="OCR engine identifier")

    # Page size constants (A4 in points, 72 DPI)
    a4_width_pt: float = Field(default=595.28, description="A4 page width in points (72 DPI)")
    a4_height_pt: float = Field(default=841.89, description="A4 page height in points (72 DPI)")

    rule_confidence_threshold: float = Field(
        default=0.80, description="Minimum confidence to skip LLM fallback (0.0-1.0)"
    )
    max_llm_concurrent: int = Field(
        default=4, description="Maximum concurrent LLM calls for multi-page documents"
    )
    max_ocr_concurrent: int = Field(
        default=10, description="Maximum concurrent OCR API calls"
    )
    ocr_max_image_long_side: int = Field(
        default=2048, description="Max image long side before resizing"
    )

    @classmethod
    def from_settings(cls, settings) -> ParserConfig:
        """Create config from Settings instance."""
        return cls(
            llm_base_url=settings.llm_base_url,
            llm_api_key=settings.llm_api_key,
            llm_model=settings.llm_model,
            ocr_api_url=settings.docparse_ocr_api_url,
            ocr_lang=settings.docparse_ocr_lang,
            ocr_engine=settings.docparse_ocr_engine,
            ocr_max_image_long_side=settings.docparse_ocr_max_image_long_side,
        )

    @classmethod
    def from_env(cls) -> ParserConfig:
        """Create config from environment variables.

        Fallback when no Settings instance is available.
        """
        return cls(
            llm_base_url=os.getenv("LLM_IP", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_NAME", ""),
            ocr_api_url=os.getenv("DOCPARSE_OCR_API_URL", ""),
            ocr_lang=os.getenv("DOCPARSE_OCR_LANG", "ch"),
            ocr_engine=os.getenv("DOCPARSE_OCR_ENGINE", "ppstructure"),
            ocr_max_image_long_side=int(os.getenv("DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE", "2048")),
        )


class Parser(Protocol):
    """Base parser interface for document parsing."""

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        """Parse a document file into the Document model.

        Args:
            file_path: Path to the document file.
            config: Parser configuration. Uses defaults if None.

        Returns:
            Parsed Document model.

        Raises:
            FileNotFoundError: If file_path does not exist.
            ValueError: If the file format is not supported.
        """
        ...

    def supports(self, file_path: str) -> bool:
        """Check if this parser supports the given file type.

        Args:
            file_path: Path to the document file.

        Returns:
            True if this parser can handle the file.
        """
        ...
