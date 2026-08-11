"""OCR engine protocol and data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OCRLineResult:
    """A single recognized text line from OCR."""

    text: str
    line_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    polys: list[tuple[float, float]] = field(default_factory=list)
    confidence: float = 0.0
    chars: list[Any] = field(default_factory=list)


@dataclass
class OCRBlock:
    """A layout block detected by OCR (e.g. title, paragraph, table)."""

    label: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class OCRPageResult:
    """OCR results for a single page."""

    lines: list[OCRLineResult]
    width: int
    height: int
    blocks: list[OCRBlock] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class OCREngine(Protocol):
    """Protocol for OCR backend implementations."""

    def __init__(self, api_url: str) -> None:
        ...

    def recognize(self, image_path: str) -> OCRPageResult:
        """Run OCR on an image file and return structured results."""
        ...

    def parse_response(self, response: dict[str, Any]) -> OCRPageResult:
        """Convert a raw API response dict into an OCRPageResult."""
        ...
