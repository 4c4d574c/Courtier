"""Shared constants for document parsing — avoid duplication across parser modules."""

# -- Image extensions ----------------------------------------------------------
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif"}
)

# -- Unit conversions (typographic points ↔ millimetres) -----------------------
# 1 pt = 25.4 / 72 mm  (standard DTP convention)
PT_TO_MM: float = 25.4 / 72

# 1 mm = 72 / 25.4 pt
MM_TO_PT: float = 72.0 / 25.4
