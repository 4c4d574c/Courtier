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


def prepare_images(file_path: str, page_indices: list[int] | None = None) -> list[str]:
    """Convert file to a list of image file paths.

    For images, returns the file directly (page_indices is inapplicable).
    For PDFs, renders the selected pages (None = every page) to
    temporary images, in the order given by page_indices.
    """
    ext = Path(file_path).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        return [file_path]

    if ext == ".pdf":
        return pdf_to_images(file_path, page_indices=page_indices)

    raise ValueError(f"Unsupported file type for OCR: {ext}")


def pdf_to_images(
    pdf_path: str,
    target_long_side: int = 2048,
    page_indices: list[int] | None = None,
) -> list[str]:
    """Render pages of a PDF to temporary PNG images.

    Each page is rendered at a zoom that maps its long side to
    *target_long_side* pixels (~175 DPI for A4 — matching the OCR
    service's own long-side cap, so resize_images_for_ocr becomes a
    pass-through in the common case).  Pages with a degenerate (zero)
    size fall back to a fixed 150 DPI render.

    *page_indices* selects 0-based pages to render (mixed-PDF page
    subsets); None renders every page.  Output order follows the
    requested indices.

    All pages share one per-document temp directory (registered in
    _TEMP_DIRS at creation, removed by cleanup_temp_images).
    """
    import fitz

    doc = fitz.open(pdf_path)
    temp_dir = _make_temp_dir(prefix="docparse_")
    image_paths: list[str] = []

    try:
        indices = range(len(doc)) if page_indices is None else page_indices
        for page_idx in indices:
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


def deskew_image(image_path: str) -> str:
    """Deskew a scanned page image into a registered temp copy.

    Estimates the skew angle from ink pixels (grayscale → adaptive
    binarization → minAreaRect over foreground points) and rotates the
    image straight with an affine transform.  The corrected copy is
    written to a registered temp dir; the original file is never
    modified.

    Returns the original path unchanged when opencv is not installed
    (optional ``deskew`` extra), the image cannot be read, the page has
    too little ink to estimate an angle, or it is already straight.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning(
            "opencv-python-headless not installed (docparse[deskew] extra); "
            "skipping deskew of %s",
            image_path,
        )
        return image_path

    img = cv2.imread(image_path)
    if img is None:
        logger.warning("deskew: cannot read image %s; leaving it unchanged", image_path)
        return image_path

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    coords = np.column_stack(np.where(binary > 0))  # (row, col) ink points
    if len(coords) < 100:  # too little ink for a reliable angle
        logger.info("deskew: %s has too little content; leaving it unchanged", image_path)
        return image_path

    angle = cv2.minAreaRect(coords)[-1]
    # Cross-version normalization: OpenCV 4.5–4.x reports the rect angle in
    # (0, 90], older and 5.x in [-90, 0).  Fold to [-90, 0) first, then map
    # to the signed correction that straightens the text lines.
    if angle > 45:
        angle -= 90
    correction = -(90 + angle) if angle < -45 else -angle
    if abs(correction) < 0.1:  # already straight
        return image_path

    height, width = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), correction, 1.0)
    rotated = cv2.warpAffine(
        img,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )

    temp_dir = _make_temp_dir(prefix="docparse_deskew_")
    deskewed_path = os.path.join(temp_dir, Path(image_path).name)
    cv2.imwrite(deskewed_path, rotated)
    logger.info("deskew: %s corrected by %.2f degrees", image_path, correction)
    return deskewed_path


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
