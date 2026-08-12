"""OCR fallback for convert_document — PPOCR /ocr/text endpoint integration.

convert_document is a content-only converter; when the source is a plain
image or a scanned/image-only PDF (anydoc raises UnsupportedError), the
plugin falls back to the OCR text endpoint configured via
ANYDOC_OCR_API_URL, which returns full-page Markdown in reading order.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Upload MIME type by image file extension — the OCR service validates the
# declared content type, so a JPEG/TIFF/BMP original must not be uploaded
# with a hardcoded image/png header.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
}

# Formats anydoc cannot convert at all — these go straight to the OCR path.
IMAGE_EXTENSIONS = frozenset(_MIME_BY_SUFFIX)

# Concurrent page OCR jobs — the OCR service is the bottleneck, not the
# plugin process.
_OCR_CONCURRENCY = 4

# A single page OCR call is ~0.5s on the reference service; 60s is generous
# headroom for slow pages or a loaded service.
_OCR_TIMEOUT = 60.0


def is_ocr_configured() -> bool:
    """Return True when an OCR endpoint is configured."""
    return bool(os.environ.get("ANYDOC_OCR_API_URL"))


def ocr_image(path: str) -> str:
    """OCR a single image file and return the full-page Markdown.

    Raises:
        RuntimeError: API call failed, non-200 response, or non-JSON body.
    """
    p = Path(path)
    mime = _MIME_BY_SUFFIX.get(p.suffix.lower(), "image/png")
    return _post_markdown(p, p.name, mime)


async def ocr_pdf(path: str) -> tuple[str, int]:
    """OCR a scanned PDF page by page; return (markdown, page_count).

    Pages are rendered to PNG at 150 DPI with PyMuPDF, OCR'd concurrently
    (4-way), and joined in page order with 【第 N 页】 marker lines so reading
    order survives downstream processing. A failure on any single page fails
    the whole job and reports the failing page number.

    Raises:
        RuntimeError: PDF open/render failure, or an OCR failure on a page.
    """
    import fitz

    pdf_path = Path(path)
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise RuntimeError(f"无法打开 PDF（{exc}）") from exc

    try:
        page_count = len(doc)
        if page_count == 0:
            raise RuntimeError("PDF 没有任何页面")

        with tempfile.TemporaryDirectory(prefix="anydoc_ocr_") as tmp:
            images: list[str] = []
            for idx in range(page_count):
                try:
                    pix = doc[idx].get_pixmap(dpi=150)
                except Exception as exc:
                    raise RuntimeError(f"第 {idx + 1} 页渲染失败：{exc}") from exc
                img = Path(tmp) / f"page_{idx}.png"
                pix.save(img)
                images.append(str(img))

            sem = asyncio.Semaphore(_OCR_CONCURRENCY)

            async def _ocr_page(idx: int) -> str:
                async with sem:
                    try:
                        return await asyncio.to_thread(
                            _post_markdown,
                            Path(images[idx]),
                            f"page_{idx + 1}.png",
                            "image/png",
                        )
                    except RuntimeError as exc:
                        raise RuntimeError(f"第 {idx + 1} 页 OCR 失败：{exc}") from exc

            results = await asyncio.gather(
                *(_ocr_page(idx) for idx in range(page_count)),
                return_exceptions=True,
            )
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    raise result

            pages = "\n\n".join(f"【第 {idx + 1} 页】\n{results[idx]}" for idx in range(page_count))
        return pages, page_count
    finally:
        doc.close()


def _post_markdown(path: Path, filename: str, mime: str) -> str:
    """POST one image to the OCR endpoint and return its Markdown text.

    Raises:
        RuntimeError: endpoint missing/unreachable, non-200 response,
            non-JSON body, or no ``markdown`` field in the payload.
    """
    url = os.environ.get("ANYDOC_OCR_API_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("ANYDOC_OCR_API_URL 未配置")
    try:
        with open(path, "rb") as f:
            resp = requests.post(url, files={"file": (filename, f, mime)}, timeout=_OCR_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"OCR 服务返回 HTTP {resp.status_code}")
        payload: Any = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"OCR 服务调用失败: {exc}") from exc
    except ValueError as exc:
        # requests raises JSONDecodeError (a ValueError) when the body is
        # not JSON — e.g. an HTML gateway error page.
        raise RuntimeError(f"OCR 服务返回了非 JSON 响应: {exc}") from exc

    markdown = payload.get("markdown") if isinstance(payload, dict) else None
    if not isinstance(markdown, str) or not markdown:
        raise RuntimeError("OCR 服务响应缺少 markdown 字段")
    return markdown
