from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# 对齐方式取值的单点定义：document.py 的 Paragraph.alignment 与此处的
# Block.alignment 共用同一 Literal，取值集合不得再各自内联维护。
Alignment = Literal["left", "center", "right", "justify"]


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
    """规范侧段落块：描述该文种*要求*的格式（与 document.py 的实测语义相对）。

    间距/缩进字段为 float（单位 pt），与文档侧实测值可直接比较；
    block_no 表示块在规范中的顺序号，仅规范侧使用（文档侧 Paragraph
    已无此字段）。
    line_spacing 为 Optional：None 表示规范未规定该块的行距（GB/T 9704
    不规定版头/版记块的行距，规则 JSON 中省略该键），校验时跳过行距检查。
    """

    required: bool
    space_before: float
    space_after: float
    line_spacing: float | None = None
    first_indent: float
    block_no: int
    outline_level: str
    elements: list[Element]
    alignment: Alignment = "left"


class PageDimensions(BaseModel):
    width: float
    height: float


class SpecMargin(BaseModel):
    top_margin: float
    bottom_margin: float
    left_margin: float
    right_margin: float


class SpecHeader(BaseModel):
    """规范侧版头。可选性语义与文档侧不同，注意区分：

    - spec 侧 None 表示"该文种规范中无此要素"；非 None 表示规范定义了
      该要素（再由 Block.required 区分必须/可有）。
    - document 侧（Header）None 表示"这份文档中未提取到该要素"。

    例如 signatory：规范中仅上行文需要，故此处为 Optional；文档中
    未出现签发人时文档侧同样为 None，两者含义各自成立。
    """

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
    """规范侧主体。stamp 为必填 Block：现行文种规范均要求印章要素

    （是否允许缺失由校验器按 Block.required 等规则判断）；文档侧
    Body.stamp 为 Optional，None 仅表示"这份文档未提取到印章"，
    两侧语义不同，勿互相推导。
    """

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
