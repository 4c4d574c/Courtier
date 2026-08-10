"""OCR backend abstraction layer.

Provides a pluggable OCR engine interface with a PPStructureV3 adapter.
"""

from .base import OCRBlock, OCREngine, OCRLineResult, OCRPageResult
from .factory import create_ocr_engine
from .ppstructure import PPStructureAdapter

__all__ = [
    "OCRBlock",
    "OCREngine",
    "OCRLineResult",
    "OCRPageResult",
    "PPStructureAdapter",
    "create_ocr_engine",
]
