"""模板内容校验与印章默认值处理。"""

from __future__ import annotations

import base64
import os


def spec_from_template_content(content: dict) -> dict:
    """提取并返回新格式模板内容。

    仅接受新格式（version + page + elements），其他格式视为无效。
    """
    if not isinstance(content, dict):
        raise TypeError("template content must be a dict")
    # New format: has 'version' and 'elements'
    if "version" in content and "elements" in content:
        return content
    raise ValueError(
        "Unsupported template format: expected new format with 'version' and 'elements'"
    )


# ===================== 默认印章图片处理 =====================

_DEFAULT_SEAL_PATH = os.path.join(os.path.dirname(__file__), "..", "seals", "default_seal.png")

_default_seal_base64: str | None = None


def get_default_seal_base64() -> str:
    """读取默认印章图片并返回 data URI 格式的 base64 字符串。"""
    global _default_seal_base64
    if _default_seal_base64 is not None:
        return _default_seal_base64

    if not os.path.exists(_DEFAULT_SEAL_PATH):
        raise FileNotFoundError(f"默认印章图片不存在: {_DEFAULT_SEAL_PATH}")

    with open(_DEFAULT_SEAL_PATH, "rb") as f:
        img_data = f.read()
    _default_seal_base64 = "data:image/png;base64," + base64.b64encode(img_data).decode("utf-8")
    return _default_seal_base64


def replace_default_seal(content: dict) -> dict:
    """将模板 content 中印章元素的 '__default_seal__' 替换为默认印章图片的 base64。"""
    if not content or not isinstance(content, dict):
        return content

    elements = content.get("elements")
    if not elements or not isinstance(elements, list):
        return content

    try:
        default_seal = get_default_seal_base64()
    except FileNotFoundError:
        default_seal = ""

    for el in elements:
        if (
            isinstance(el, dict)
            and el.get("type") == "印章"
            and isinstance(el.get("style"), dict)
            and el["style"].get("sealImage") == "__default_seal__"
        ):
            el["style"]["sealImage"] = default_seal

    return content
