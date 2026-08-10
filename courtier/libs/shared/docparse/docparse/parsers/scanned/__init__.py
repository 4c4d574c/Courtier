"""Scanned document parser — seven-stage pipeline for image/PDF-to-Document.

Stages: preprocess → OCR → spacing → font → normalize → LLM → structure.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from docmodels import (
    Document,
    Page,
    PageContent,
)
from PIL import Image as PILImage

from ..base import ParserConfig
from ..llm_client import LLMClient
from ..ocr import create_ocr_engine
from ..rules import StructureRuleEngine
from ..spacing import (
    compute_first_indent,
    compute_left_right_indent,
    compute_margins,
    compute_paragraph_spacing,
)
from ..structure_recognizer import (
    _classified_lines_to_page_content,
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
    merge_spacing_into_page_content,
    normalize_body_line_spacing,
)

logger = logging.getLogger(__name__)


class ScannedParser:
    """Parser for scanned documents using PPStructureV3 API + LLM.

    Flow:
    1. Call PPStructureV3 API for OCR + layout detection
    2. Compute margins and spacing from bounding boxes
    2.5. Crop-based LLM font recognition for all lines
    3. LLM structure recognition for all pages
    4. Build Document model
    """

    def supports(self, file_path: str) -> bool:
        """Check if file is a supported image or scanned PDF."""
        ext = Path(file_path).suffix.lower()
        return ext in IMAGE_EXTENSIONS or ext == ".pdf"

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        """Parse a scanned document into the Document model.

        Optimized flow:
        1. Parallel OCR for all pages via pluggable OCR engine
        2. Crop-based LLM font recognition for all lines
        3. LLM structure recognition for all pages
        4. Build Document model

        Args:
            file_path: Path to the image or scanned PDF file.
            config: Parser configuration.

        Returns:
            Parsed Document model. Pages whose OCR or LLM calls failed are
            degraded (empty page / rule-engine fallback) and reported in
            ``Document.warnings``.

        Raises:
            FileNotFoundError: If file_path does not exist.
            ValueError: If no LLM API key is configured (checked before
                any OCR call is made).
            RuntimeError: If OCR fails for every page.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        # Fail fast — the pipeline cannot produce structure without the
        # LLM, so check before burning any OCR quota.
        if not effective_config.llm_api_key:
            raise ValueError("LLM_API_KEY not configured; scanned document parsing requires an LLM")

        max_ocr = getattr(effective_config, "max_ocr_concurrent", 10)
        max_llm = max(1, getattr(effective_config, "max_llm_concurrent", 4))

        file_bytes = path.read_bytes()
        doc_id = hashlib.sha256(file_bytes).hexdigest()

        warnings: list[str] = []
        image_paths: list[str] = []
        try:
            image_paths = prepare_images(file_path)
            image_paths = resize_images_for_ocr(
                image_paths,
                effective_config.ocr_max_image_long_side,
            )

            # Phase 1: Parallel OCR via pluggable engine.  Per-page
            # failures are recorded in warnings; parallel_ocr raises
            # RuntimeError when every page fails.
            ocr_engine = create_ocr_engine(
                effective_config.ocr_engine,
                effective_config.ocr_api_url,
            )
            ocr_failed_pages: list[int] = []
            ocr_results = parallel_ocr(
                ocr_engine,
                image_paths,
                max_ocr,
                warnings=warnings,
                failed_pages=ocr_failed_pages,
            )
            ocr_failed = set(ocr_failed_pages)

            # Phase 2: Parse OCR results and compute metrics
            page_metrics: list[dict[str, Any]] = []
            for page_idx, page_result in enumerate(ocr_results):
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

                extracted_lines = ocr_result_to_lines(
                    page_result,
                    img_width=img_width,
                    img_height=img_height,
                )
                if not extracted_lines and page_idx not in ocr_failed:
                    warnings.append(f"第 {page_idx + 1} 页 OCR 未识别到文本内容，该页内容为空")

                rec_boxes = [
                    [line["x0"], line["y0"], line["x1"], line["y1"]] for line in extracted_lines
                ]

                margin = compute_margins(
                    rec_boxes,
                    img_width,
                    img_height,
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
                    rec_boxes,
                    img_width,
                    effective_config.a4_width_pt,
                    outline_levels={
                        i: line.get("outline_level", "") for i, line in enumerate(extracted_lines)
                    },
                )

                font_sizes = {i: line["font_size"] for i, line in enumerate(extracted_lines)}
                left_right_indent_map = compute_left_right_indent(
                    rec_boxes,
                    img_width,
                    page_width_mm=210.0,
                    margin=margin,
                    font_sizes=font_sizes,
                )

                page_metrics.append(
                    {
                        "lines": extracted_lines,
                        "margin": margin,
                        "spacing_map": spacing_map,
                        "indent_map": indent_map,
                        "left_right_indent_map": left_right_indent_map,
                        "img_width": img_width,
                        "img_height": img_height,
                        "a4_width_pt": effective_config.a4_width_pt,
                        "a4_height_pt": effective_config.a4_height_pt,
                    }
                )

            # Phase 2.1: Refine font_size using chars-per-line cross-validation
            refine_font_size_by_chars_per_line(page_metrics)

            # Phase 2.2: Document-level body line spacing normalization
            normalize_body_line_spacing(page_metrics)

            llm_client = LLMClient(effective_config)

            # Phase 2.5: Crop-based LLM font recognition for every page
            # with lines — fonts are measured from the rendered image, not
            # stamped from the GB/T element mapping — run concurrently
            # across pages (bounded by max_llm_concurrent).  A failed page
            # keeps its lines without font info and is recorded in warnings.
            font_tasks: list[tuple[int, list[dict[str, Any]]]] = []
            for page_idx, pm in enumerate(page_metrics):
                lines = pm["lines"]
                if not lines:
                    continue
                font_tasks.append((page_idx, lines))

            def _recognize_fonts(
                task: tuple[int, list[dict[str, Any]]],
            ) -> tuple[int, dict[int, dict[str, Any]] | None, str | None]:
                page_idx, lines = task
                try:
                    font_info = llm_client.recognize_fonts_from_crops(
                        image_paths[page_idx],
                        lines,
                    )
                except Exception as exc:
                    logger.warning(
                        "Scanned page %d: crop-based LLM font fallback failed: %s",
                        page_idx,
                        exc,
                    )
                    return (
                        page_idx,
                        None,
                        f"第 {page_idx + 1} 页字体 LLM 识别失败：{exc}",
                    )
                logger.info(
                    "Scanned page %d: crop-based LLM font fallback completed" " (%d lines)",
                    page_idx,
                    len(font_info),
                )
                return page_idx, font_info, None

            if font_tasks:
                with ThreadPoolExecutor(max_workers=min(max_llm, len(font_tasks))) as executor:
                    font_outcomes = list(executor.map(_recognize_fonts, font_tasks))
                for page_idx, font_info, warning in font_outcomes:
                    if warning is not None:
                        warnings.append(warning)
                    if font_info:
                        merge_font_info(page_metrics[page_idx]["lines"], font_info)

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

            # Phase 4: Concurrent LLM structure recognition (bounded by
            # max_llm_concurrent), results assembled in page order.  A
            # page whose LLM call fails falls back to rule-engine
            # classification (unclassified lines are kept as body text)
            # instead of breaking the whole document.
            def _recognize_structure(
                task: tuple[int, dict[str, Any]],
            ) -> tuple[int, PageContent, list[str]]:
                page_idx, pm = task
                lines = pm["lines"]
                margin = pm["margin"]
                page_warnings: list[str] = []
                try:
                    page_content = recognize_page_structure(
                        lines,
                        margin,
                        llm_client,
                        image_paths[page_idx],
                        effective_config,
                        img_width=pm["img_width"],
                    )
                except Exception as exc:
                    logger.warning(
                        "Scanned page %d: LLM structure recognition failed"
                        " (%s); falling back to rule engine",
                        page_idx,
                        exc,
                    )
                    page_warnings.append(
                        f"第 {page_idx + 1} 页 LLM 结构识别失败，" f"已回退为规则引擎分类：{exc}"
                    )
                    rule_warnings: list[str] = []
                    classified = StructureRuleEngine().classify_lines(
                        lines,
                        has_position=True,
                        page_height=effective_config.a4_height_pt,
                    )
                    page_content = _classified_lines_to_page_content(
                        classified.lines,
                        lines,
                        margin,
                        warnings=rule_warnings,
                    )
                    page_warnings.extend(f"第 {page_idx + 1} 页：{w}" for w in rule_warnings)
                merge_spacing_into_page_content(
                    page_content,
                    pm["spacing_map"],
                    pm["indent_map"],
                    pm.get("left_right_indent_map"),
                )
                return page_idx, page_content, page_warnings

            if llm_needed:
                with ThreadPoolExecutor(max_workers=min(max_llm, len(llm_needed))) as executor:
                    structure_outcomes = list(executor.map(_recognize_structure, llm_needed))
                for page_idx, page_content, page_warnings in structure_outcomes:
                    warnings.extend(page_warnings)
                    pages_result.append((page_idx, page_content))

            # Phase 5: Assemble final document.
            pages_result.sort(key=lambda x: x[0])
            pages = []
            for page_idx, page_content in pages_result:
                pages.append(
                    Page(
                        page_content=page_content,
                        page_no=page_idx,
                    )
                )

            return Document(
                doc_id=doc_id,
                total_page_num=len(pages),
                save_path=str(path.absolute()),
                pages=pages,
                warnings=warnings,
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
]
