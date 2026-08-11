from __future__ import annotations

import os
from typing import Protocol

from docmodels import Document
from pydantic import BaseModel, Field


def _env_flag(name: str) -> bool:
    """Parse a boolean environment variable ("1"/"true"/"yes"/"on" → True)."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


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

    max_llm_concurrent: int = Field(
        default=4, description="Maximum concurrent LLM calls for multi-page documents"
    )
    max_ocr_concurrent: int = Field(default=10, description="Maximum concurrent OCR API calls")
    ocr_max_image_long_side: int = Field(
        default=2048, description="Max image long side before resizing"
    )
    ocr_deskew_enabled: bool = Field(
        default=False,
        description="Enable OpenCV deskew preprocessing for scanned pages "
        "(requires the docparse[deskew] extra)",
    )

    # Self-trained ResNet font recognition model (single-char classifier).
    # When font_model_url is set, per-line fonts are recognized by the
    # model first and only unrecognized lines fall back to the LLM.
    font_model_url: str = Field(default="", description="Font recognition model API base URL")
    font_model_conf_threshold: float = Field(
        default=0.6, description="Min char-level font confidence to accept a prediction"
    )
    font_model_margin_threshold: float = Field(
        default=0.15, description="Min char-level top1-top2 margin to accept a prediction"
    )

    # LLM request optimization
    llm_image_max_long_side: int = Field(
        default=1280, description="Max long side of page images sent to the structure LLM"
    )
    classify_mode: str = Field(
        default="llm",
        description="Structure classification mode: 'llm' (always LLM), 'rule_first' "
        "(rule engine first, LLM when rules lack confidence), 'rule_only'",
    )

    @classmethod
    def from_env(cls) -> ParserConfig:
        """Create config from environment variables."""
        return cls(
            llm_base_url=os.getenv("LLM_IP", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_NAME", ""),
            ocr_api_url=os.getenv("DOCPARSE_OCR_API_URL", ""),
            ocr_lang=os.getenv("DOCPARSE_OCR_LANG", "ch"),
            ocr_engine=os.getenv("DOCPARSE_OCR_ENGINE", "ppstructure"),
            max_llm_concurrent=int(os.getenv("DOCPARSE_MAX_LLM_CONCURRENT", "4")),
            max_ocr_concurrent=int(os.getenv("DOCPARSE_MAX_OCR_CONCURRENT", "10")),
            ocr_max_image_long_side=int(os.getenv("DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE", "2048")),
            ocr_deskew_enabled=_env_flag("DOCPARSE_OCR_DESKEW"),
            font_model_url=os.getenv("FONT_MODEL_URL", ""),
            font_model_conf_threshold=float(os.getenv("FONT_MODEL_CONF_THRESHOLD", "0.6")),
            font_model_margin_threshold=float(os.getenv("FONT_MODEL_MARGIN_THRESHOLD", "0.15")),
            llm_image_max_long_side=int(os.getenv("DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE", "1280")),
            classify_mode=os.getenv("DOCPARSE_CLASSIFY_MODE", "llm"),
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
