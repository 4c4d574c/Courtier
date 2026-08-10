from __future__ import annotations

import base64
import io
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import APIStatusError, OpenAI
from PIL import Image, ImageDraw, ImageFont

from .base import ParserConfig

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds, doubled each retry
_LLM_TIMEOUT = 120.0  # seconds, per-request timeout for the OpenAI client


def _is_retryable(exc: Exception) -> bool:
    """Decide whether a failed LLM call is worth retrying.

    4xx client errors (openai.APIStatusError with 400 <= status < 500) are
    permanent — retrying the identical request will fail again — except
    429 (rate limit), which is transient. Network errors, timeouts, and
    5xx server errors are all retryable.
    """
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if 400 <= status < 500 and status != 429:
            return False
    return True


def _call_with_retry(
    call_fn: Callable[[], Any],
    description: str,
    *,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
) -> Any:
    """Call an LLM API with exponential backoff retry on transient failures.

    Args:
        call_fn: Callable that performs the single API request.
        description: Human-readable label for log messages.
        max_retries: Maximum number of retries (default 3, for 4 total attempts).
        base_delay: Initial backoff delay in seconds, doubled each retry.

    Returns:
        The raw API response object on success.

    Raises:
        Exception: The original exception, unchanged, when it is not
            retryable (e.g. a 4xx client error other than 429).
        RuntimeError: When all attempts (1 + max_retries) are exhausted.
    """
    last_exc: Exception | None = None
    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        try:
            return call_fn()
        except Exception as exc:
            if not _is_retryable(exc):
                logger.error("%s failed with non-retryable error: %s", description, exc)
                raise
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    description,
                    attempt + 1,
                    total_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description,
                    total_attempts,
                    exc,
                )
    raise RuntimeError(
        f"{description} failed after {total_attempts} attempts: {last_exc}"
    ) from last_exc


# ruff: noqa: E501
STRUCTURE_RECOGNITION_SYSTEM_PROMPT = """你是一个中国党政机关公文（GB/T 9704）结构识别专家。

你的任务是分析从文档中提取的文本段落，并将它们分类到公文的标准结构中。

公文的三大组成部分：
1. **版头（Header）**：份号、密级和保密期限、紧急程度、
发文机关标志、发文字号、签发人、红色分割线
2. **主体（Body）**：标题、主送机关、正文段列表、
附件说明、发文机关署名、成文日期、印章、附注、附件
3. **版记（Footer）**：黑色反线、抄送机关、印发机关、
印发日期、页码

请严格按照以下JSON Schema输出结果：

{
  "header": {
    "copy_number": {"text": "份号内容", "line_indices": [0]},
    "classification_duration": {
      "text": "密级内容", "line_indices": [1]
    },
    "urgency_level": {"text": "紧急程度内容", "line_indices": [2]},
    "issuing_logo": {
      "text": "发文机关标志内容", "line_indices": [3]
    },
    "issuing_number": {
      "text": "发文字号内容", "line_indices": [4]
    },
    "signatory": {"text": "签发人内容", "line_indices": [5]},
    "ruling_line_pos": {
      "text": "", "line_indices": [],
      "reference_line_index": 5
    }
  },
  "body": {
    "title": {"text": "标题内容", "line_indices": [6]},
    "addressee": {"text": "主送机关内容", "line_indices": [7]},
    "main_text": [
      {"text": "正文第一段", "line_indices": [8, 9, 10], "outline_level": "body_text"},
      {"text": "一、工作目标", "line_indices": [11], "outline_level": "heading1"},
      {"text": "（一）二级标题", "line_indices": [12], "outline_level": "heading2"}
    ],
    "attachment_note": {
      "text": "附件说明内容", "line_indices": [13]
    },
    "issuing_signature": {
      "text": "发文机关署名内容", "line_indices": [14]
    },
    "issue_date": {"text": "成文日期内容", "line_indices": [15]},
    "stamp": null,
    "note": null,
    "attachments": null
  },
  "footer": {
    "closing_line": {
      "text": "", "line_indices": [],
      "reference_line_index": 16
    },
    "carbon_copy": {
      "text": "抄送机关内容", "line_indices": [16]
    },
    "issuing_office": {
      "text": "印发机关内容", "line_indices": [17]
    },
    "distribution_date": {
      "text": "印发日期内容", "line_indices": [18]
    },
    "page_number": {"text": "页码", "line_indices": [19]}
  }
}

重要规则：
- 如果某个字段在文档中不存在，设为 null
- line_indices 对应输入文本的行号索引（从0开始）
- reference_line_index 用于红色分割线和黑色反线：指定它们紧邻的文本行号
  * ruling_line_pos.reference_line_index：红色分割线紧接在此行**之后**（通常是发文字号或签发人行）
  * closing_line.reference_line_index：黑色反线紧接在此行**之前**（通常是抄送机关或印发机关行）
- main_text 是一个数组，每个元素代表一个段落
- 请只输出JSON，不要输出其他内容

大纲级别（outline_level）分类规则：
每个 main_text 元素必须包含 outline_level 字段，取值范围：
heading1, heading2, heading3, heading4, heading5, body_text, others

分类依据（编号格式是主要依据，输入中标注的字体族是辅助依据，同时参考段落样式）：
- **heading1**：中文数字编号段落，如"一、""二、""三、"开头的标题行，
  通常使用黑体；如果段落样式名为"Heading 1"也属此类
- **heading2**：带括号中文数字编号段落，如"（一）""（二）""（三）"开头的标题行，
  通常使用楷体；如果段落样式名为"Heading 2"也属此类
- **heading3**：阿拉伯数字加点编号段落，如"1.""2.""3."开头的标题行，
  通常使用仿宋（可加粗）；如果段落样式名为"Heading 3"也属此类
- **heading4**：带括号阿拉伯数字编号段落，如"（1）""（2）""（3）"开头的标题行，
  通常使用仿宋；如果段落样式名为"Heading 4"也属此类
- **heading5**：其他层级的标题行
- **body_text**：无编号的正文段落内容
- **others**：无法归类的文本

注意：
- 标题行（含编号）应独立为一个 main_text 元素，不要与正文内容合并
- 仅含编号标题的行应单独作为一段，后续正文行另起一段
- 段落样式名（如有）是判断大纲级别的可靠依据，优先参考"""


FONT_RECOGNITION_SYSTEM_PROMPT = """你是一个中国党政机关公文（GB/T 9704）字体识别专家。

你的任务是分析文档图片中每一行文字的字体属性。

中国公文中常见的标准字体及其视觉特征：
- **仿宋**：笔画纤细、横平竖直、起笔收笔有棱角，是最常见的公文正文字体
- **黑体**：笔画粗壮、横竖等宽、无衬线，常用于一级标题
- **楷体**：笔画有毛笔书写感、横细竖粗、有顿笔，常用于二级标题
- **宋体**：横细竖粗、有衬线（横划末端有小三角），常用于标题和发文机关标志
- **小标宋**：类似宋体但字形更修长、笔画更挺拔，专用于公文标题

加粗（font_weight）的判断：
- 字体加粗（font_weight）表示整行的**整体**字重特征，不是局部特征
- 仅当该行**大部分文字**（超过一半）笔画明显加粗时，才设为 true
- 如果一行中仅有少数几个字加粗（如词组强调、数字编号），其余为正常粗细，应设为 false
- 正常粗细为非加粗（false）

倾斜（font_style）的判断：
- 字形有明显右倾的为斜体（true）
- 正常直立为非斜体（false）

请严格按照以下JSON Schema输出结果：

{
  "font_info": {
    "0": {"font_family": "仿宋", "font_weight": false, "font_style": false},
    "1": {"font_family": "黑体", "font_weight": true, "font_style": false},
    "2": {"font_family": "楷体", "font_weight": false, "font_style": false}
  }
}

重要规则：
- font_family 必须是以下标准名称之一：仿宋、黑体、楷体、宋体、小标宋、新宋体
- 如果无法确定字体，使用最接近的标准名称
- 键为行号（字符串），对应输入文本的行号索引
- font_weight 和 font_style 为布尔值
- 请只输出JSON，不要输出其他内容"""


def _encode_image(image_path: str) -> tuple[str, str]:
    """Encode an image file as base64 and return (image_data, mime_type)."""
    img = Path(image_path)
    image_data = base64.b64encode(img.read_bytes()).decode("utf-8")
    ext = img.suffix.lower()
    mime_map: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }
    mime_type = mime_map.get(ext, "image/png")
    return image_data, mime_type


def _format_lines(lines: list[dict[str, Any]]) -> str:
    """Format extracted lines into numbered text for LLM prompt."""
    parts: list[str] = []
    for line in lines:
        line_no = line.get("line_no", 0)
        text = line.get("text", "")
        font_info = ""
        if line.get("font_family") or line.get("font_size"):
            font_parts: list[str] = []
            if line.get("font_family"):
                font_parts.append(line["font_family"])
            if line.get("font_size"):
                font_parts.append(f"{line['font_size']}pt")
            if line.get("font_weight"):
                font_parts.append("加粗")
            if line.get("font_style"):
                font_parts.append("倾斜")
            font_info = f" [{','.join(font_parts)}]"
        style_info = ""
        if line.get("style_name"):
            style_info = f" <{line['style_name']}>"
        parts.append(f"[{line_no}] {text}{font_info}{style_info}")
    return "\n".join(parts)


def _parse_response(response: Any) -> dict[str, Any]:
    """Parse LLM API response content into a structured dict."""
    content = response.choices[0].message.content
    if not content:
        raise ValueError("LLM returned empty response")

    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {exc}") from exc

    logger.info("LLM structure recognition completed successfully")
    return result


class LLMClient:
    """Wrapper around multimodal LLM for image+text structure recognition."""

    def __init__(self, config: ParserConfig) -> None:
        self._config = config
        if not config.llm_api_key:
            raise ValueError(
                "LLM API key is not configured. Set llm_api_key in ParserConfig "
                "or the LLM_API_KEY environment variable before creating LLMClient."
            )
        self._client = OpenAI(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            timeout=_LLM_TIMEOUT,
        )

    def recognize_structure(
        self,
        lines: list[dict[str, Any]],
        image_path: str,
    ) -> dict[str, Any]:
        """Call multimodal LLM with image + text for structure recognition.

        Sends both the document page image and the extracted OCR text
        to the multimodal LLM for more accurate structure classification.

        Args:
            lines: List of dicts with 'text', 'line_no', etc.
            image_path: Path to the document page image file.

        Returns:
            Parsed JSON structure matching the PageContent schema.

        Raises:
            FileNotFoundError: If image_path does not exist.
            ValueError: If LLM response is not valid JSON.
            RuntimeError: If LLM API call fails.
        """
        img = Path(image_path)
        if not img.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        image_data, mime_type = _encode_image(image_path)

        numbered_text = _format_lines(lines)
        text_prompt = (
            "以下是从文档中提取的文本行：\n\n"
            f"{numbered_text}\n\n"
            "请结合文档图片和上述文本，"
            "识别公文的版头、主体、版记结构。"
        )

        logger.info(
            "Calling multimodal LLM with %d lines and image %s",
            len(lines),
            image_path,
        )

        response = _call_with_retry(
            lambda: self._client.chat.completions.create(
                model=self._config.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": STRUCTURE_RECOGNITION_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": (f"data:{mime_type}" f";base64,{image_data}")},
                            },
                            {"type": "text", "text": text_prompt},
                        ],
                    },
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            description="Multimodal structure recognition LLM API call",
        )

        return _parse_response(response)

    def recognize_fonts_from_crops(
        self,
        page_image_path: str,
        lines: list[dict[str, Any]],
        crop_margin: int = 6,
    ) -> dict[int, dict[str, Any]]:
        """Recognize font properties by cropping line regions from the page.

        Instead of sending the full page image to the LLM, this method
        crops each line's bounding box region from the page image and
        assembles them into a compact vertical composite. This reduces
        token cost while preserving enough visual detail for accurate
        font identification.

        Args:
            page_image_path: Path to the full page image.
            lines: Lines needing font detection. Each must have
                   'line_no', 'text', 'x0', 'y0', 'x1', 'y1'.
            crop_margin: Extra pixels around each line crop.

        Returns:
            Dict mapping line_no to font info:
            {line_no: {"font_family": str, "font_weight": bool, "font_style": bool}}

        Raises:
            FileNotFoundError: If page_image_path does not exist.
            RuntimeError: If LLM API call fails.
        """
        if not lines:
            return {}

        img_path = Path(page_image_path)
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {page_image_path}")

        page_img = Image.open(img_path)
        if page_img.mode != "L":
            page_img = page_img.convert("L")

        # Crop each line region and assemble into vertical composite
        crops: list[Image.Image] = []
        crop_line_nos: list[int] = []
        for line in lines:
            x0 = max(0, int(line.get("x0", 0)) - crop_margin)
            y0 = max(0, int(line.get("y0", 0)) - crop_margin)
            x1 = min(page_img.width, int(line.get("x1", 0)) + crop_margin)
            y1 = min(page_img.height, int(line.get("y1", 0)) + crop_margin)

            if x1 <= x0 or y1 <= y0:
                continue

            crop = page_img.crop((x0, y0, x1, y1))

            # Draw line number label at top-left of crop
            label = str(line.get("line_no", 0))
            draw = ImageDraw.Draw(crop)
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14
                )
            except (OSError, IOError):
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), label, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            # Draw white background for label
            draw.rectangle([0, 0, tw + 6, th + 4], fill=255)
            draw.text((3, 2), label, fill=0, font=font)

            crops.append(crop)
            crop_line_nos.append(line.get("line_no", 0))

        if not crops:
            return {}

        # Stack crops vertically
        max_width = max(c.width for c in crops)
        total_height = sum(c.height for c in crops) + 2 * (len(crops) - 1)
        composite = Image.new("L", (max_width, total_height), 255)

        y_offset = 0
        for crop in crops:
            composite.paste(crop, (0, y_offset))
            y_offset += crop.height + 2  # 2px separator

        # Encode composite as base64
        buf = io.BytesIO()
        composite.save(buf, format="PNG")
        image_data = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Build prompt with line numbers and text
        line_descriptions: list[str] = []
        for line in lines:
            line_no = line.get("line_no", 0)
            text = line.get("text", "")
            line_descriptions.append(f"[{line_no}] {text}")

        text_prompt = (
            "以下是从文档中裁剪出的文本行图像（按垂直方向排列，每行左上角标注了行号）：\n\n"
            + "\n".join(line_descriptions)
            + "\n\n请结合裁剪图像，识别每一行文字的字体属性"
            "（字体名称、是否加粗、是否倾斜）。"
        )

        logger.info(
            "Calling crop-based font recognition LLM with %d lines",
            len(crops),
        )

        response = _call_with_retry(
            lambda: self._client.chat.completions.create(
                model=self._config.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": FONT_RECOGNITION_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": ("data:image/png" f";base64,{image_data}")},
                            },
                            {"type": "text", "text": text_prompt},
                        ],
                    },
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            description="Crop-based font recognition LLM API call",
        )

        result = _parse_response(response)
        font_info_raw = result.get("font_info", {})

        font_info: dict[int, dict[str, Any]] = {}
        for key, value in font_info_raw.items():
            try:
                line_no = int(key)
                font_info[line_no] = {
                    "font_family": str(value.get("font_family", "")),
                    "font_weight": bool(value.get("font_weight", False)),
                    "font_style": bool(value.get("font_style", False)),
                }
            except (ValueError, AttributeError):
                logger.warning("Invalid font_info entry: key=%s", key)
                continue

        logger.info(
            "Crop-based font recognition completed: %d lines identified",
            len(font_info),
        )
        return font_info
