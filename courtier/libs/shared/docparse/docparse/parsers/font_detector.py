"""Three-layer font detection with GB/T 9704 standard mapping as Layer 1.

Layer 1: Standard element-to-font mapping from the calibration module.
         Returns confidence=1.0 for known elements, confidence=0.0 for unknown.

Future layers (2 & 3) will add heuristic and OCR-based detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .calibration import get_element_format


@dataclass(frozen=True)
class FontDetectionResult:
    """Immutable result of font detection for a single text line."""

    font_family: str
    font_size: float
    bold: bool
    italic: bool = False
    confidence: float = 0.0


def detect_font(outline_level: str, font_size_hint: float = 0.0) -> FontDetectionResult:
    """Layer 1: GB/T 9704 standard mapping.

    Returns confidence=1.0 for known elements, confidence=0.0 for unknown.
    """
    fmt = get_element_format(outline_level)
    if fmt is not None:
        family, size, bold = fmt
        return FontDetectionResult(
            font_family=family,
            font_size=size,
            bold=bold,
            confidence=1.0,
        )
    return FontDetectionResult(
        font_family="",
        font_size=font_size_hint,
        bold=False,
        confidence=0.0,
    )


def detect_fonts_for_page(lines: list[dict[str, Any]]) -> list[FontDetectionResult]:
    """Batch detect fonts for all lines on a page.

    Preserves existing font_family if already set and standard mapping
    does not provide a confident result.
    """
    results: list[FontDetectionResult] = []
    for line in lines:
        outline = line.get("outline_level", "others")
        font_size = line.get("font_size", 0.0)
        existing_family = line.get("font_family", "")

        result = detect_font(outline, font_size)

        if existing_family and result.confidence < 1.0:
            result = FontDetectionResult(
                font_family=existing_family,
                font_size=font_size if font_size > 0 else result.font_size,
                bold=line.get("font_weight", False),
                italic=line.get("font_style", False),
                confidence=0.9,
            )
        results.append(result)
    return results


def merge_font_detections(
    lines: list[dict[str, Any]],
    detections: list[FontDetectionResult],
) -> None:
    """Merge font detection results back into line dicts.

    Only overwrites fields that are currently empty/falsy.
    """
    for line, detection in zip(lines, detections):
        if detection.font_family and not line.get("font_family"):
            line["font_family"] = detection.font_family
        if detection.font_size > 0 and not line.get("font_size"):
            line["font_size"] = detection.font_size
        if detection.bold and not line.get("font_weight"):
            line["font_weight"] = detection.bold
        if detection.italic and not line.get("font_style"):
            line["font_style"] = detection.italic
