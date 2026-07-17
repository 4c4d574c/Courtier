"""Scanned document parser — seven-stage pipeline for image/PDF-to-Document.

Stages: preprocess → OCR → spacing → font → normalize → LLM → structure.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from docmodels import (
    Document,
    Page,
    PageContent,
)
from PIL import Image as PILImage

from ..base import ParserConfig
from ..font_detector import detect_fonts_for_page, merge_font_detections
from ..llm_client import LLMClient
from ..ocr import create_ocr_engine
from ..spacing import (
    compute_first_indent,
    compute_left_right_indent,
    compute_margins,
    compute_paragraph_spacing,
)
from ..structure_recognizer import (
    recognize_page_structure,
)
from .font_detector import merge_font_info, refine_font_size_by_chars_per_line
from .ocr_engine import (
    build_block_outline_map_from_blocks,
    get_outline_for_line,
    ocr_result_to_lines,
    parallel_ocr,
)
from .preprocessor import (
    IMAGE_EXTENSIONS,
    cleanup_temp_images,
    prepare_images,
    resize_images_for_ocr,
)
from .spacing import (
    adjust_cross_page_spacing,
    merge_spacing_into_page_content,
    normalize_body_line_spacing,
)

logger = logging.getLogger(__name__)


class ScannedParser:
    """Parser for scanned documents using PPStructureV3 API + LLM.

    Flow:
    1. Call PPStructureV3 API for OCR + layout detection
    2. Compute margins and spacing from bounding boxes
    2.5. Font detection via standard mapping + LLM fallback
    3. LLM structure recognition for all pages
    4. Build Document model
    """

    def supports(self, file_path: str) -> bool:
        """Check if file is a supported image or scanned PDF."""
        ext = Path(file_path).suffix.lower()
        return ext in IMAGE_EXTENSIONS or ext == ".pdf"

    def parse(
        self, file_path: str, config: ParserConfig | None = None
    ) -> Document:
        """Parse a scanned document into the Document model.

        Optimized flow:
        1. Parallel OCR for all pages via pluggable OCR engine
        2. Standard font detection + LLM fallback for low-confidence lines
        3. LLM structure recognition for all pages
        4. Build Document model

        Args:
            file_path: Path to the image or scanned PDF file.
            config: Parser configuration.

        Returns:
            Parsed Document model.

        Raises:
            FileNotFoundError: If file_path does not exist.
            RuntimeError: If OCR API call fails.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()
        max_ocr = getattr(effective_config, "max_ocr_concurrent", 10)

        file_bytes = path.read_bytes()
        doc_id = hashlib.sha256(file_bytes).hexdigest()

        image_paths = prepare_images(file_path)
        image_paths = resize_images_for_ocr(
            image_paths, effective_config.ocr_max_image_long_side,
        )

        try:
            # Phase 1: Parallel OCR via pluggable engine
            ocr_engine = create_ocr_engine(
                effective_config.ocr_engine, effective_config.ocr_api_url,
            )
            ocr_results = parallel_ocr(
                ocr_engine, image_paths, max_ocr,
            )

            # Phase 2: Parse OCR results and compute metrics
            page_metrics: list[dict[str, Any]] = []
            for page_idx, page_result in enumerate(ocr_results):
                extracted_lines = ocr_result_to_lines(page_result)

                rec_boxes = [
                    [line["x0"], line["y0"], line["x1"], line["y1"]]
                    for line in extracted_lines
                ]
                # Use actual (resized) image dimensions — not page_result.width
                # which is max(x1)/max(y1) when the API omits width/height.
                # The OCR bounding boxes are in this image's coordinate space.
                actual_img = Path(image_paths[page_idx])
                if actual_img.exists():
                    with PILImage.open(actual_img) as pil_img:
                        img_width, img_height = pil_img.size
                else:
                    img_width = page_result.width
                    img_height = page_result.height

                margin = compute_margins(
                    rec_boxes, img_width, img_height,
                    effective_config.a4_width_pt,
                    effective_config.a4_height_pt,
                )
                spacing_map = compute_paragraph_spacing(
                    rec_boxes,
                    img_height,
                    effective_config.a4_height_pt,
                    img_width=img_width,
                )
                indent_map = compute_first_indent(
                    rec_boxes, img_width, effective_config.a4_width_pt,
                )

                font_sizes = {
                    i: line["font_size"]
                    for i, line in enumerate(extracted_lines)
                }
                left_right_indent_map = compute_left_right_indent(
                    rec_boxes,
                    img_width,
                    page_width_mm=210.0,
                    margin=margin,
                    font_sizes=font_sizes,
                )

                # Layer 1 font detection: standard element-to-font mapping
                detections = detect_fonts_for_page(extracted_lines)
                merge_font_detections(extracted_lines, detections)

                page_metrics.append({
                    "lines": extracted_lines,
                    "margin": margin,
                    "spacing_map": spacing_map,
                    "indent_map": indent_map,
                    "left_right_indent_map": left_right_indent_map,
                    "img_width": img_width,
                    "img_height": img_height,
                    "a4_width_pt": effective_config.a4_width_pt,
                    "a4_height_pt": effective_config.a4_height_pt,
                })

            # Phase 2.1: Refine font_size using chars-per-line cross-validation
            refine_font_size_by_chars_per_line(page_metrics)

            # Phase 2.2: Document-level body line spacing normalization
            normalize_body_line_spacing(page_metrics)

            # Phase 2.3: Cross-page continuation adjustment
            adjust_cross_page_spacing(page_metrics)

            # Phase 2.5: Crop-based LLM font recognition for unresolved lines
            if not effective_config.llm_api_key:
                raise ValueError(
                    "LLM_API_KEY not configured; scanned document parsing requires an LLM"
                )
            llm_client = LLMClient(effective_config)
            for page_idx, pm in enumerate(page_metrics):
                lines = pm["lines"]
                if not lines:
                    continue

                # Collect lines that still lack font info
                unresolved = [
                    line for line in lines
                    if not line.get("font_family")
                ]
                if not unresolved:
                    logger.info(
                        "Scanned page %d: all fonts resolved by standard mapping",
                        page_idx,
                    )
                    continue

                img_path = image_paths[page_idx]
                try:
                    font_info = llm_client.recognize_fonts_from_crops(
                        img_path, unresolved,
                    )
                    merge_font_info(lines, font_info)
                    logger.info(
                        "Scanned page %d: crop-based LLM font fallback completed (%d lines)",
                        page_idx,
                        len(font_info),
                    )
                except Exception as exc:
                    logger.warning(
                        "Scanned page %d: crop-based LLM font fallback failed: %s",
                        page_idx,
                        exc,
                    )

            # Phase 3: Collect pages for LLM classification
            pages_result: list[tuple[int, PageContent]] = []
            llm_needed: list[tuple[int, dict[str, Any]]] = []

            for page_idx, pm in enumerate(page_metrics):
                lines = pm["lines"]
                margin = pm["margin"]

                if not lines:
                    pages_result.append((page_idx, PageContent(margin=margin)))
                    continue

                llm_needed.append((page_idx, pm))

            # Phase 4: LLM fallback for low-confidence pages
            if llm_needed:
                for page_idx, pm in llm_needed:
                    lines = pm["lines"]
                    margin = pm["margin"]
                    img_path = image_paths[page_idx]

                    page_content = recognize_page_structure(
                        lines, margin, llm_client, img_path, effective_config,
                    )
                    merge_spacing_into_page_content(
                        page_content, pm["spacing_map"], pm["indent_map"],
                        pm.get("left_right_indent_map"),
                    )
                    pages_result.append((page_idx, page_content))

            # Phase 5: Assemble final document
            pages_result.sort(key=lambda x: x[0])
            pages = []
            for page_idx, page_content in pages_result:
                raw = (
                    Path(image_paths[page_idx]).read_bytes()
                    if page_idx == 0 else b""
                )
                pages.append(
                    Page(
                        raw=raw,
                        page_content=page_content,
                        save_path=str(path.absolute()),
                        page_no=page_idx,
                    )
                )

            return Document(
                doc_id=doc_id,
                total_page_num=len(pages),
                save_path=str(path.absolute()),
                pages=pages,
            )
        finally:
            cleanup_temp_images(image_paths)


__all__ = [
    "ScannedParser",
    "IMAGE_EXTENSIONS",
    "prepare_images",
    "resize_images_for_ocr",
    "cleanup_temp_images",
    "parallel_ocr",
    "ocr_result_to_lines",
    "build_block_outline_map_from_blocks",
    "get_outline_for_line",
    "merge_font_info",
    "refine_font_size_by_chars_per_line",
    "merge_spacing_into_page_content",
    "normalize_body_line_spacing",
    "adjust_cross_page_spacing",
]
