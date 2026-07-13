"""Image preprocessing for scanned document OCR pipeline.

Handles PDF-to-image conversion, image resizing, and temporary file cleanup.
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


def pdf_to_images(pdf_path: str) -> list[str]:
    """Render each page of a PDF to a temporary PNG image."""
    import fitz

    doc = fitz.open(pdf_path)
    image_paths: list[str] = []

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=300)

            temp_dir = tempfile.mkdtemp(prefix="docparse_")
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

        temp_dir = tempfile.mkdtemp(prefix="docparse_resized_")
        new_path = os.path.join(temp_dir, Path(img_path).name)
        img.save(new_path)
        img.close()

        logger.info(
            "Image resized for OCR: %dx%d -> %dx%d (ratio %.2f)",
            w, h, new_w, new_h, ratio,
        )
        resized_paths.append(new_path)

    return resized_paths


def cleanup_temp_images(image_paths: list[str]) -> None:
    """Remove temporary image files created during PDF rendering."""
    dirs_cleaned: set[str] = set()
    for img_path in image_paths:
        img = Path(img_path)
        if img.exists() and "docparse_" in str(img.parent):
            parent = str(img.parent)
            if parent not in dirs_cleaned:
                try:
                    shutil.rmtree(parent)
                    dirs_cleaned.add(parent)
                except OSError:
                    logger.warning(
                        "Failed to cleanup temp directory: %s", parent
                    )
