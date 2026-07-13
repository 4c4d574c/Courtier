"""骨架模板加载。

从 JSON 骨架文件加载模板，叠加文种变体。
"""
from __future__ import annotations


import json
import logging
from pathlib import Path

from docmodels.document import (
    Body,
    Document,
    Font,
    Footer,
    Header,
    Margin,
    MetaData,
    Page,
    PageContent,
    Paragraph,
    Position,
)

logger = logging.getLogger(__name__)

# 骨架文件目录
SKELETONS_DIR = Path(__file__).parent.parent / "skeletons"
VARIANTS_DIR = SKELETONS_DIR / "variants"

def load_builtin_skeleton(doc_type: str) -> dict | None:
    """加载内置骨架模板。

    先加载 base.json，再叠加 variants/<doc_type>.json 的差异。
    """
    base_path = SKELETONS_DIR / "base.json"
    if not base_path.exists():
        logger.warning("骨架文件不存在: %s", base_path)
        return None

    with open(base_path, "r", encoding="utf8") as f:
        base = json.load(f)

    variant_path = VARIANTS_DIR / f"{doc_type}.json"
    if variant_path.exists():
        with open(variant_path, "r", encoding="utf8") as f:
            variant = json.load(f)
        # 简单合并：variant 的 elements 覆盖 base 中同 key 的 elements
        _merge_variant(base, variant)

    base["doc_type"] = doc_type
    return base

def _merge_variant(base: dict, variant: dict) -> None:
    """将 variant 差异合并到 base 中。"""
    # 合并顶层简单字段
    for key in ("name", "description"):
        if key in variant:
            base[key] = variant[key]

    if "page" in variant:
        base.setdefault("page", {}).update(variant["page"])

    if "elements" not in variant:
        return

    base_elements = base.setdefault("elements", [])
    base_keys = {el["key"]: i for i, el in enumerate(base_elements)}

    for var_el in variant["elements"]:
        key = var_el.get("key")
        if key is None:
            continue
        if key in base_keys:
            # 深度合并：保留 base 中未被 variant 覆盖的字段
            idx = base_keys[key]
            base_el = base_elements[idx]
            for k, v in var_el.items():
                if isinstance(v, dict) and isinstance(base_el.get(k), dict):
                    base_el[k].update(v)
                else:
                    base_el[k] = v
        else:
            base_elements.append(var_el)

    # 重新排序
    base_elements.sort(key=lambda e: e.get("sort_order", 0))

def _block_to_paragraph(
    block: dict, sample_text: str, default_font_size: float = 16.0
) -> Paragraph:
    """将旧格式 block 转为 Paragraph，填入示例文本。"""
    if not block:
        return Paragraph()

    elements = block.get("elements", [])
    meta_elements = []
    if not elements:
        meta_elements.append(
            MetaData(
                exist=True,
                position=Position(),
                font=Font(
                    font_family="仿宋",
                    font_size=default_font_size,
                    font_weight=False,
                    font_style=False,
                    text=sample_text,
                    line_no=1,
                ),
            )
        )
    else:
        for el in elements:
            font_data = el.get("font", {})
            pos_data = el.get("position", {})
            font_size = font_data.get("font_size", 0) or default_font_size
            font_family = font_data.get("font_family", "仿宋") or "仿宋"
            meta_elements.append(
                MetaData(
                    exist=True,
                    position=Position(
                        x0=pos_data.get("x0", 0),
                        y0=pos_data.get("y0", 0),
                        x1=pos_data.get("x1", 0),
                        y1=pos_data.get("y1", 0),
                    ),
                    font=Font(
                        font_family=font_family,
                        font_size=font_size,
                        font_weight=font_data.get("font_weight", False),
                        font_style=font_data.get("font_style", False),
                        text=sample_text,
                        line_no=font_data.get("line_no", 1),
                    ),
                )
            )

    return Paragraph(
        space_before=block.get("space_before", 0) or 0,
        space_after=block.get("space_after", 0) or 0,
        line_spacing=block.get("line_spacing", 1.0) or 1.0,
        first_indent=block.get("first_indent", 0) or 0,
        left_indent=0,
        right_indent=0,
        block_no=block.get("block_no", 0),
        elements=meta_elements,
        outline_level=block.get("outline_level", "others"),
        alignment=block.get("alignment", "left"),
    )
