from __future__ import annotations

from pydantic import BaseModel

class SpecPosition(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

class SpecFont(BaseModel):
    font_family: str
    font_size: float
    font_weight: bool
    font_style: bool
    text: str
    line_no: int

class Element(BaseModel):
    position: SpecPosition
    font: SpecFont

class Block(BaseModel):
    required: bool
    space_before: int
    space_after: int
    line_spacing: float
    first_indent: int
    block_no: int
    outline_level: str
    elements: list[Element]
    alignment: str = "left"

class PageDimensions(BaseModel):
    width: int
    height: int

class SpecMargin(BaseModel):
    top_margin: int
    bottom_margin: int
    left_margin: int
    right_margin: int

class SpecHeader(BaseModel):
    copy_number: Block | None = None
    classification_duration: Block | None = None
    urgency_level: Block | None = None
    issuing_logo: Block
    issuing_number: Block
    signatory: Block | None = None
    ruling_line_pos: Block

class MainText(BaseModel):
    heading1: Block | None = None
    heading2: Block | None = None
    heading3: Block | None = None
    heading4: Block | None = None
    body_text: Block
    others: Block | None = None

class SpecBody(BaseModel):
    title: Block
    addressee: Block
    main_text: MainText
    attachment_note: Block | None = None
    issuing_signature: Block
    issue_date: Block
    stamp: Block
    note: Block | None = None
    attachments: Block | None = None

class SpecFooter(BaseModel):
    closing_line: Block
    carbon_copy: Block | None = None
    issuing_office: Block
    distribution_date: Block
    page_number: Block

class SpecPage(BaseModel):
    page_size: str
    page_dimensions: PageDimensions
    margin: SpecMargin
    header: SpecHeader
    body: SpecBody
    footer: SpecFooter

class DocumentFormatSpec(BaseModel):
    """文档格式规范顶层模型。

    注意：spec.py 中的 Spec* 类（如 SpecPosition、SpecFont 等）与
    document.py 中的运行时模型（Position、Font 等）有意保持分离：
    - spec 模型描述格式规范中*要求什么*（整数尺寸，必填字段）
    - document 模型描述文档中*实际测量到什么*（浮点数，默认值）
    """
    page: SpecPage
