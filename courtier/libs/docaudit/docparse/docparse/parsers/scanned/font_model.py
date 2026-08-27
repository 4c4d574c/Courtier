"""ResNet font-model client for line-level font recognition.

Replaces per-page LLM font calls with a self-trained single-char
classifier: OCR word boxes are subdivided into per-char crops, sent to
the model in batches, and each line's font is decided by majority vote
over confident chars.  Lines with too few confident chars are reported
as unrecognized so the caller can fall back to the crop-based LLM path.
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# Model label → pipeline-standard family name.  The model was trained
# with GB/T printing font names; the pipeline uses the shorter forms.
LABEL_TO_FAMILY: dict[str, str] = {
    "仿宋": "仿宋",
    "黑体": "黑体",
    "楷体": "楷体",
    "宋体": "宋体",
    "小标宋": "小标宋",
    "小标宋体": "小标宋",
    "新宋体": "新宋体",
}

# Labels that carry no usable family information.
_IGNORED_LABELS = frozenset({"", "其他"})

# The model service rejects batches larger than this.
_MAX_BATCH = 64


def _map_label(label: str) -> str:
    """Map a raw model label to a pipeline-standard family name."""
    if label in LABEL_TO_FAMILY:
        return LABEL_TO_FAMILY[label]
    # Unknown "X体" labels degrade gracefully by stripping the suffix.
    if label.endswith("体") and label[:-1] in LABEL_TO_FAMILY.values():
        return label[:-1]
    return ""


@dataclass
class _CharPrediction:
    """One char-crop prediction from the font model."""

    family: str  # mapped pipeline-standard name, "" when unusable
    font_confidence: float
    font_margin: float
    weight: str  # "常规" / "粗体"
    slant: str  # "常规" / "斜体"


class FontModelClient:
    """HTTP client for the self-trained ResNet font recognition model.

    The model accepts single-char grayscale crops only; feeding whole
    line images yields confidently wrong results, so line regions must
    be subdivided into per-char boxes first (see :meth:`recognize_lines`).
    """

    def __init__(
        self,
        base_url: str,
        conf_threshold: float = 0.6,
        margin_threshold: float = 0.15,
        *,
        timeout: float = 30.0,
        min_qualified_chars: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._conf_threshold = conf_threshold
        self._margin_threshold = margin_threshold
        self._timeout = timeout
        self._min_qualified_chars = min_qualified_chars

    @staticmethod
    def _char_boxes(line: dict[str, Any]) -> list[tuple[float, float, float, float]]:
        """Subdivide a line's OCR word boxes into per-char boxes.

        Full-width (CJK) word boxes are split into ``len(text)`` equal
        slices; pure-ASCII words are skipped because the model is only
        trained on CJK glyphs.
        """
        boxes: list[tuple[float, float, float, float]] = []
        for word in line.get("chars") or []:
            text = word.get("text") or ""
            if not text or any(ord(c) < 128 for c in text):
                continue
            n = len(text)
            step = (word["x1"] - word["x0"]) / n
            for i in range(n):
                boxes.append(
                    (word["x0"] + i * step, word["y0"], word["x0"] + (i + 1) * step, word["y1"])
                )
        return boxes

    def _predict_batch(self, crops: list[PILImage.Image]) -> list[_CharPrediction]:
        """POST one batch of char crops to the model (≤ _MAX_BATCH each)."""
        files: list[tuple[str, tuple[str, io.BytesIO, str]]] = []
        for i, crop in enumerate(crops):
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            buf.seek(0)
            files.append(("images", (f"char_{i}.png", buf, "image/png")))
        response = requests.post(
            f"{self._base_url}/predict_batch",
            files=files,
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Font model batch failed: {payload!r}")
        predictions: list[_CharPrediction] = []
        for item in payload.get("data", []):
            if not item.get("success"):
                predictions.append(_CharPrediction("", 0.0, 0.0, "常规", "常规"))
                continue
            result = item.get("result") or {}
            predictions.append(
                _CharPrediction(
                    family=_map_label(str(result.get("font", ""))),
                    font_confidence=float(result.get("font_confidence", 0.0)),
                    font_margin=float(result.get("font_top2_margin", 0.0)),
                    weight=str(result.get("weight", "常规")),
                    slant=str(result.get("slant", "常规")),
                )
            )
        return predictions

    def recognize_lines(
        self,
        page_image_path: str,
        lines: list[dict[str, Any]],
    ) -> tuple[dict[int, dict[str, Any]], list[int]]:
        """Recognize per-line fonts with the model.

        Args:
            page_image_path: Page image in the same coordinate space as
                the lines' bounding boxes.
            lines: Extracted line dicts (need ``line_no`` and ``chars``
                word boxes).

        Returns:
            (font_info, unrecognized): ``font_info`` maps line_no to
            ``{"font_family", "font_weight", "font_style"}`` in the same
            shape as the crop-based LLM path; ``unrecognized`` lists
            line_nos with too few confident chars that need the LLM
            fallback.
        """
        page_img = PILImage.open(page_image_path)
        if page_img.mode != "L":
            page_img = page_img.convert("L")

        # Collect all char crops across lines, keeping per-line spans.
        spans: list[tuple[int, int, int]] = []  # (line_no, start, end)
        all_crops: list[PILImage.Image] = []
        for line in lines:
            line_no = line.get("line_no", 0)
            start = len(all_crops)
            for box in self._char_boxes(line):
                x0, y0, x1, y1 = box
                x0 = max(0, min(int(x0), page_img.width - 1))
                y0 = max(0, min(int(y0), page_img.height - 1))
                x1 = max(x0 + 1, min(int(x1) + 1, page_img.width))
                y1 = max(y0 + 1, min(int(y1) + 1, page_img.height))
                all_crops.append(page_img.crop((x0, y0, x1, y1)))
            if len(all_crops) > start:
                spans.append((line_no, start, len(all_crops)))

        font_info: dict[int, dict[str, Any]] = {}
        unrecognized: list[int] = [line.get("line_no", 0) for line in lines]
        recognized_nos: set[int] = set()
        if not spans:
            return font_info, unrecognized

        predictions: list[_CharPrediction] = []
        for offset in range(0, len(all_crops), _MAX_BATCH):
            batch = all_crops[offset : offset + _MAX_BATCH]
            predictions.extend(self._predict_batch(batch))

        for line_no, start, end in spans:
            qualified = [
                p
                for p in predictions[start:end]
                if p.family
                and p.font_confidence >= self._conf_threshold
                and p.font_margin >= self._margin_threshold
            ]
            if len(qualified) < self._min_qualified_chars:
                continue
            family = Counter(p.family for p in qualified).most_common(1)[0][0]
            bold_votes = sum(1 for p in qualified if p.weight == "粗体")
            italic_votes = sum(1 for p in qualified if p.slant == "斜体")
            font_info[line_no] = {
                "font_family": family,
                "font_weight": bold_votes > len(qualified) / 2,
                "font_style": italic_votes > len(qualified) / 2,
            }
            recognized_nos.add(line_no)

        unrecognized = [no for no in unrecognized if no not in recognized_nos]
        logger.info(
            "Font model recognized %d lines, %d need LLM fallback (%s)",
            len(font_info),
            len(unrecognized),
            Path(page_image_path).name,
        )
        return font_info, unrecognized
