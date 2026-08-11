"""Factory for creating OCR engine instances."""

from __future__ import annotations

from .base import OCREngine
from .ppstructure import PPStructureAdapter

_ENGINES: dict[str, type[OCREngine]] = {
    "ppstructure": PPStructureAdapter,
}


def create_ocr_engine(engine_name: str, api_url: str) -> OCREngine:
    """Create an OCR engine instance by name.

    Args:
        engine_name: Identifier of the OCR engine (e.g. "ppstructure").
        api_url: API endpoint URL for the engine.

    Returns:
        An OCREngine instance.

    Raises:
        ValueError: If engine_name is not recognized.
    """
    engine_cls = _ENGINES.get(engine_name)
    if engine_cls is None:
        supported = ", ".join(sorted(_ENGINES.keys()))
        raise ValueError(f"Unknown OCR engine '{engine_name}'. Supported: {supported}")
    return engine_cls(api_url=api_url)
