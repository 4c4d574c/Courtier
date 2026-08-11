from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

from ._retry import _call_with_retry
from .base import ParserConfig

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 120.0  # seconds, per-request timeout for the OpenAI client


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
    "copy_number": {"line_indices": [0]},
    "classification_duration": {"line_indices": [1]},
    "urgency_level": {"line_indices": [2]},
    "issuing_logo": {"line_indices": [3]},
    "issuing_number": {"line_indices": [4]},
    "signatory": {"line_indices": [5]},
    "ruling_line_pos": {
      "line_indices": [],
      "reference_line_index": 5
    }
  },
  "body": {
    "title": {"line_indices": [6]},
    "addressee": {"line_indices": [7]},
    "main_text": [
      {"line_indices": [8, 9, 10], "outline_level": "body_text"},
      {"line_indices": [11], "outline_level": "heading1"},
      {"line_indices": [12], "outline_level": "heading2"}
    ],
    "attachment_note": {"line_indices": [13]},
    "issuing_signature": {"line_indices": [14]},
    "issue_date": {"line_indices": [15]},
    "stamp": null,
    "note": null,
    "attachments": null
  },
  "footer": {
    "closing_line": {
      "line_indices": [],
      "reference_line_index": 16
    },
    "carbon_copy": {"line_indices": [16]},
    "issuing_office": {"line_indices": [17]},
    "distribution_date": {"line_indices": [18]},
    "page_number": {"line_indices": [19]}
  }
}

重要规则：
- 如果某个字段在文档中不存在，设为 null
- line_indices 对应输入文本的行号索引（从0开始）
- 不要回拷文本内容：输出只需给出 line_indices（和 outline_level），
  段落文本由调用方按行号自行取用
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
- 输入图像为各行裁剪区域按行号升序、自上而下拼接的合成图，
  图像顺序与行号顺序一一对应，请按此顺序逐行输出识别结果
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


def _encode_page_image(image_path: str, max_long_side: int) -> tuple[str, str]:
    """Downscale and JPEG-encode a page image for the structure LLM.

    Structure recognition only needs layout-level detail, so the page is
    capped at ``max_long_side`` pixels and sent as JPEG (quality 80) to
    cut the base64 payload — full-resolution PNGs dominate request size
    and LLM prefill time.
    """
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    long_side = max(img.size)
    if max_long_side > 0 and long_side > max_long_side:
        scale = max_long_side / long_side
        img = img.resize(
            (round(img.width * scale), round(img.height * scale)),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"


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


# Candidate fonts for the optional line-number labels on line crops
# (Debian/Ubuntu paths).  Labels are only an auxiliary cue — the real
# contract is "composite order == ascending line_no" — so when no
# candidate exists on the host, annotation is silently skipped.
_LABEL_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _load_label_font(size: int) -> Any:
    """Load the first available label font candidate; None when none exists."""
    for candidate in _LABEL_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            continue
    return None


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

        image_data, mime_type = _encode_page_image(image_path, self._config.llm_image_max_long_side)

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

        Lines are sorted by line_no before cropping: the composite stacks
        crops top-to-bottom in ascending line_no order and the LLM is
        instructed to answer in that order. Line-number labels drawn on
        each crop are only an auxiliary cue (silently skipped when no
        suitable font exists on the host).

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

        # Sort by line_no ascending: the composite stacks crops
        # top-to-bottom in this exact order, and the prompt contract is
        # "image order == ascending line numbers".
        sorted_lines = sorted(lines, key=lambda line: line.get("line_no", 0))

        # Crop each line region and assemble into vertical composite
        crops: list[Image.Image] = []
        label_font = _load_label_font(14)
        for line in sorted_lines:
            x0 = max(0, int(line.get("x0", 0)) - crop_margin)
            y0 = max(0, int(line.get("y0", 0)) - crop_margin)
            x1 = min(page_img.width, int(line.get("x1", 0)) + crop_margin)
            y1 = min(page_img.height, int(line.get("y1", 0)) + crop_margin)

            if x1 <= x0 or y1 <= y0:
                continue

            crop = page_img.crop((x0, y0, x1, y1))

            # Draw line number label at top-left of crop (auxiliary cue,
            # silently skipped when no label font exists on the host)
            if label_font is not None:
                label = str(line.get("line_no", 0))
                draw = ImageDraw.Draw(crop)
                bbox = draw.textbbox((0, 0), label, font=label_font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                # Draw white background for label
                draw.rectangle([0, 0, tw + 6, th + 4], fill=255)
                draw.text((3, 2), label, fill=0, font=label_font)

            crops.append(crop)

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
        for line in sorted_lines:
            line_no = line.get("line_no", 0)
            text = line.get("text", "")
            line_descriptions.append(f"[{line_no}] {text}")

        text_prompt = (
            "以下是从文档中裁剪出的文本行图像（按垂直方向拼接，"
            "图像自上而下与行号升序一一对应，每行左上角可能带有行号标注作为辅助）：\n\n"
            + "\n".join(line_descriptions)
            + "\n\n请按图像从上到下的顺序，结合裁剪图像，识别每一行文字的字体属性"
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
