"""Image preprocessing for scanned document OCR pipeline.

Handles PDF-to-image conversion, image resizing, and temporary file cleanup.

Temporary directories created by this module are tracked in an explicit
process-local registry (_TEMP_DIRS). cleanup_temp_images() only removes
directories present in that registry, so user files are never deleted —
even when their path happens to contain "docparse_".
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from PIL import Image as PILImage

from .._constants import IMAGE_EXTENSIONS

logger = logging.getLogger(__name__)

# Absolute paths of temp directories created by this module in this process.
# Registration happens at creation time, so a function that raises midway
# still leaves its already-created dirs registered for the caller's
# finally/cleanup to remove.
_TEMP_DIRS: set[str] = set()


def _make_temp_dir(prefix: str) -> str:
    """Create a temp directory and register it for later cleanup."""
    temp_dir = tempfile.mkdtemp(prefix=prefix)
    _TEMP_DIRS.add(temp_dir)
    return temp_dir


def prepare_images(file_path: str) -> list[str]:
    """Convert file to a list of image file paths.

    For images, returns the file directly.
    For PDFs, renders each page to a temporary image.
    """
    ext = Path(file_path).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        return [file_path]

    if ext == ".pdf":
        return pdf_to_images(file_path)

    raise ValueError(f"Unsupported file type for OCR: {ext}")


def pdf_to_images(pdf_path: str, target_long_side: int = 2048) -> list[str]:
    """Render each page of a PDF to a temporary PNG image.

    Each page is rendered at a zoom that maps its long side to
    *target_long_side* pixels (~175 DPI for A4 — matching the OCR
    service's own long-side cap, so resize_images_for_ocr becomes a
    pass-through in the common case).  Pages with a degenerate (zero)
    size fall back to a fixed 150 DPI render.

    All pages share one per-document temp directory (registered in
    _TEMP_DIRS at creation, removed by cleanup_temp_images).
    """
    import fitz

    doc = fitz.open(pdf_path)
    temp_dir = _make_temp_dir(prefix="docparse_")
    image_paths: list[str] = []

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            long_side_pt = max(page.rect.width, page.rect.height)
            if long_side_pt > 0:
                zoom = target_long_side / long_side_pt
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            else:
                pix = page.get_pixmap(dpi=150)

            img_path = f"{temp_dir}/page_{page_idx}.png"
            pix.save(img_path)
            image_paths.append(img_path)
    finally:
        doc.close()

    return image_paths


def resize_images_for_ocr(
    image_paths: list[str],
    max_long_side: int,
) -> list[str]:
    """Resize images so long side <= max_long_side, matching OCR service behavior.

    The OCR service resizes images before processing so coordinates in results
    are relative to the resized image. This function applies the same transform
    locally so that subsequent cropping (e.g. font recognition) uses coordinates
    that match the actual image dimensions.

    PDF pages rendered by pdf_to_images already target this long side, so
    for them this is normally a pass-through; it remains as the fallback
    for direct image inputs and abnormal page sizes.

    Original files are never modified — resized copies are written to temp dirs.

    Args:
        image_paths: List of image file paths (may include temp files from PDF).
        max_long_side: Maximum allowed long side in pixels.

    Returns:
        List of image paths, possibly with some replaced by resized temp copies.
    """
    resized_paths: list[str] = []
    for img_path in image_paths:
        img = PILImage.open(img_path)
        w, h = img.size
        long_side = max(w, h)
        if long_side <= max_long_side:
            img.close()
            resized_paths.append(img_path)
            continue

        ratio = max_long_side / long_side
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), PILImage.LANCZOS)

        temp_dir = _make_temp_dir(prefix="docparse_resized_")
        new_path = os.path.join(temp_dir, Path(img_path).name)
        img.save(new_path)
        img.close()

        logger.info(
            "Image resized for OCR: %dx%d -> %dx%d (ratio %.2f)",
            w,
            h,
            new_w,
            new_h,
            ratio,
        )
        resized_paths.append(new_path)

    return resized_paths


def cleanup_temp_images(image_paths: list[str]) -> None:
    """Remove temporary image directories created by this module.

    Only directories registered in _TEMP_DIRS (created by pdf_to_images /
    resize_images_for_ocr in this process) are removed; anything else —
    e.g. the user's original image files — is left untouched. Successfully
    removed directories are dropped from the registry; directories that
    fail to remove stay registered so a later cleanup can retry.
    """
    dirs_cleaned: set[str] = set()
    for img_path in image_paths:
        parent = str(Path(img_path).parent)
        if parent in dirs_cleaned or parent not in _TEMP_DIRS:
            continue
        try:
            shutil.rmtree(parent)
        except OSError:
            logger.warning("Failed to cleanup temp directory: %s", parent)
        else:
            dirs_cleaned.add(parent)
            _TEMP_DIRS.discard(parent)
