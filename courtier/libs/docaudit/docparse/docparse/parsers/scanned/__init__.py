"""Scanned document parser — pipeline for image/PDF-to-Document.

Stages: preprocess → OCR → spacing → font → normalize → structure (rule engine).
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
from ..structure_recognizer import _classified_lines_to_page_content
from .font_detector import merge_font_info, refine_font_size_by_chars_per_line
from .font_model import FontModelClient
from .ocr_engine import (
    build_block_outline_map_from_blocks,
    get_outline_for_line,
    ocr_result_to_lines,
    parallel_ocr,
)
from .preprocessor import (
    IMAGE_EXTENSIONS,
    cleanup_temp_images,
    deskew_image,
    prepare_images,
    resize_images_for_ocr,
)
from .spacing import (
    merge_spacing_into_page_content,
    normalize_body_line_spacing,
)

logger = logging.getLogger(__name__)


class ScannedParser:
    """Parser for scanned documents using PPStructureV3 API + rule engine.

    Flow:
    1. Call PPStructureV3 API for OCR + layout detection
    2. Compute margins and spacing from bounding boxes
    2.5. Crop-based font recognition for all lines
    3. Rule-engine structure recognition for all pages
    4. Build Document model
    """

    def supports(self, file_path: str) -> bool:
        """Check if file is a supported image or scanned PDF."""
        ext = Path(file_path).suffix.lower()
        return ext in IMAGE_EXTENSIONS or ext == ".pdf"

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        """Parse a scanned document into the Document model.

        Thin wrapper over parse_pages() covering every page; see
        parse_pages() for the pipeline stages.  Pages whose OCR calls
        failed are degraded (empty page) and reported in
        ``Document.warnings``.

        Args:
            file_path: Path to the image or scanned PDF file.
            config: Parser configuration.

        Returns:
            Parsed Document model.

        Raises:
            FileNotFoundError: If file_path does not exist.
            RuntimeError: If OCR fails for every page.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        file_bytes = path.read_bytes()
        doc_id = hashlib.sha256(file_bytes).hexdigest()

        pages, warnings = self.parse_pages(file_path, None, effective_config)

        return Document(
            doc_id=doc_id,
            total_page_num=len(pages),
            save_path=str(path.absolute()),
            pages=pages,
            warnings=warnings,
        )

    def parse_pages(
        self,
        file_path: str,
        page_indices: list[int] | None,
        config: ParserConfig | None = None,
    ) -> tuple[list[Page], list[str]]:
        """Parse selected pages of a scanned document via OCR + rule engine.

        Optimized flow:
        1. Parallel OCR for the selected pages via pluggable OCR engine
        2. Crop-based font recognition for all lines
        3. Rule-engine structure recognition for the selected pages
        4. Assemble Page models mapped back to the original page numbers

        ``Page.page_no`` and the page numbers inside warnings always
        refer to the original document pages, so the registry's mixed
        path can merge the result with rule-engine pages directly.

        Args:
            file_path: Path to the image or scanned PDF file.
            page_indices: 0-based original page numbers to process (PDF
                page subsets for mixed documents); ``None`` processes
                every page.
            config: Parser configuration.

        Returns:
            (pages, warnings): pages sorted by original page number and
            the collected pipeline warnings.

        Raises:
            FileNotFoundError: If file_path does not exist.
            RuntimeError: If OCR fails for every selected page.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        max_ocr = getattr(effective_config, "max_ocr_concurrent", 10)
        # Font LLM fallback concurrency (removed with the LLM font path).
        max_llm = max(1, getattr(effective_config, "max_llm_concurrent", 4))

        warnings: list[str] = []
        image_paths: list[str] = []
        try:
            image_paths = prepare_images(file_path, page_indices=page_indices)
            if effective_config.ocr_deskew_enabled:
                image_paths = [deskew_image(p) for p in image_paths]
            image_paths = resize_images_for_ocr(
                image_paths,
                effective_config.ocr_max_image_long_side,
            )

            # Original 0-based page number for each rendered image.
            orig_page_nos = (
                list(page_indices) if page_indices is not None else list(range(len(image_paths)))
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
                page_indices=orig_page_nos,
            )
            ocr_failed = set(ocr_failed_pages)

            # Phase 2: Parse OCR results and compute metrics
            page_metrics: list[dict[str, Any]] = []
            for local_idx, page_result in enumerate(ocr_results):
                page_no = orig_page_nos[local_idx]
                # Use actual (resized) image dimensions — not page_result.width
                # which is max(x1)/max(y1) when the API omits width/height.
                # The OCR bounding boxes are in this image's coordinate space.
                actual_img = Path(image_paths[local_idx])
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
                if not extracted_lines and page_no not in ocr_failed:
                    warnings.append(f"第 {page_no + 1} 页 OCR 未识别到文本内容，该页内容为空")

                # 版面块含 table 标签时提示：表格目前只按文本行解析（无单元格结构）。
                if any("table" in block.label for block in page_result.blocks):
                    warnings.append(f"第 {page_no + 1} 页检测到表格区域，表格内容按文本行解析")

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
                        "page_no": page_no,
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

            # Phase 2.5: Font recognition for every page with lines —
            # fonts are measured from the rendered image, not stamped
            # from the GB/T element mapping.  When font_model_url is
            # configured, the self-trained ResNet model recognizes lines
            # first (concurrency bounded by max_ocr_concurrent) and only
            # unrecognized lines fall back to the crop-based LLM;
            # otherwise every line goes through the LLM (bounded by
            # max_llm_concurrent).  A failed page keeps its lines without
            # font info and is recorded in warnings.
            llm_font_tasks: list[tuple[int, int, list[dict[str, Any]]]] = []
            if effective_config.font_model_url:
                font_model = FontModelClient(
                    effective_config.font_model_url,
                    conf_threshold=effective_config.font_model_conf_threshold,
                    margin_threshold=effective_config.font_model_margin_threshold,
                )

                def _recognize_fonts_by_model(
                    task: tuple[int, int, list[dict[str, Any]]],
                ) -> tuple[int, dict[int, dict[str, Any]] | None, list[dict[str, Any]], str | None]:
                    local_idx, page_no, lines = task
                    try:
                        font_info, unrecognized_nos = font_model.recognize_lines(
                            image_paths[local_idx],
                            lines,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Scanned page %d: font model recognition failed"
                            " (%s); falling back to LLM for all lines",
                            page_no,
                            exc,
                        )
                        return (
                            local_idx,
                            None,
                            lines,
                            f"第 {page_no + 1} 页字体模型识别失败，" f"已回退为 LLM 识别：{exc}",
                        )
                    unrecognized_set = set(unrecognized_nos)
                    missing = [line for line in lines if line.get("line_no", 0) in unrecognized_set]
                    logger.info(
                        "Scanned page %d: font model recognized %d/%d lines",
                        page_no,
                        len(font_info),
                        len(lines),
                    )
                    return local_idx, font_info, missing, None

                model_tasks: list[tuple[int, int, list[dict[str, Any]]]] = [
                    (local_idx, pm["page_no"], pm["lines"])
                    for local_idx, pm in enumerate(page_metrics)
                    if pm["lines"]
                ]
                if model_tasks:
                    with ThreadPoolExecutor(max_workers=min(max_ocr, len(model_tasks))) as executor:
                        model_outcomes = list(executor.map(_recognize_fonts_by_model, model_tasks))
                    for local_idx, font_info, missing, warning in model_outcomes:
                        if warning is not None:
                            warnings.append(warning)
                        if font_info:
                            merge_font_info(page_metrics[local_idx]["lines"], font_info)
                        if missing:
                            pm = page_metrics[local_idx]
                            llm_font_tasks.append((local_idx, pm["page_no"], missing))
            else:
                llm_font_tasks = [
                    (local_idx, pm["page_no"], pm["lines"])
                    for local_idx, pm in enumerate(page_metrics)
                    if pm["lines"]
                ]

            def _recognize_fonts(
                task: tuple[int, int, list[dict[str, Any]]],
            ) -> tuple[int, dict[int, dict[str, Any]] | None, str | None]:
                local_idx, page_no, lines = task
                try:
                    font_info = llm_client.recognize_fonts_from_crops(
                        image_paths[local_idx],
                        lines,
                    )
                except Exception as exc:
                    logger.warning(
                        "Scanned page %d: crop-based LLM font fallback failed: %s",
                        page_no,
                        exc,
                    )
                    return (
                        local_idx,
                        None,
                        f"第 {page_no + 1} 页字体 LLM 识别失败：{exc}",
                    )
                logger.info(
                    "Scanned page %d: crop-based LLM font fallback completed" " (%d lines)",
                    page_no,
                    len(font_info),
                )
                return local_idx, font_info, None

            if llm_font_tasks:
                with ThreadPoolExecutor(max_workers=min(max_llm, len(llm_font_tasks))) as executor:
                    font_outcomes = list(executor.map(_recognize_fonts, llm_font_tasks))
                for local_idx, font_info, warning in font_outcomes:
                    if warning is not None:
                        warnings.append(warning)
                    if font_info:
                        merge_font_info(page_metrics[local_idx]["lines"], font_info)

            # Phase 3: Structure recognition via the rule engine.  Pages
            # without OCR lines degrade to empty PageContent; lines the
            # rule engine cannot classify are kept as body text (see
            # _classified_lines_to_page_content).
            pages_result: list[tuple[int, PageContent]] = []
            rule_engine = StructureRuleEngine()

            for pm in page_metrics:
                lines = pm["lines"]
                if not lines:
                    pages_result.append((pm["page_no"], PageContent(margin=pm["margin"])))
                    continue

                page_warnings: list[str] = []
                classified = rule_engine.classify_lines(
                    lines,
                    has_position=True,
                    page_height=effective_config.a4_height_pt,
                )
                page_content = _classified_lines_to_page_content(
                    classified.lines,
                    lines,
                    pm["margin"],
                    warnings=page_warnings,
                )
                merge_spacing_into_page_content(
                    page_content,
                    pm["spacing_map"],
                    pm["indent_map"],
                    pm.get("left_right_indent_map"),
                )
                warnings.extend(f"第 {pm['page_no'] + 1} 页：{w}" for w in page_warnings)
                pages_result.append((pm["page_no"], page_content))

            # Phase 4: Assemble pages sorted by original page number.
            pages_result.sort(key=lambda x: x[0])
            pages = []
            for page_no, page_content in pages_result:
                pages.append(
                    Page(
                        page_content=page_content,
                        page_no=page_no,
                    )
                )

            return pages, warnings
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
