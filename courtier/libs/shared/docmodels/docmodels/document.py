from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .spec import Alignment


class _DocModel(BaseModel):
    """Base class for all docmodels.

    extra="ignore" 与 pydantic 默认行为一致，此处显式声明是有意为之：
    旧缓存/旧序列化文件中已删除的字段（user_id、raw、exist、block_no、
    Page.save_path 等）在反序列化时被静默丢弃而非报错，保证向后兼容。
    """

    model_config = ConfigDict(extra="ignore")


class Position(_DocModel):
    """Represents a rectangular region on the page.

    坐标单位与坐标系按提取来源不同：
    - PDF（PyMuPDF）：pt（1/72 英寸），页面左上角为原点，y 轴向下；
    - 扫描件（OCR）：缩放后图像的像素坐标（docparse 扫描管线按
      ocr_max_image_long_side 缩放图片，坐标即缩放后图像的像素坐标）；
    - DOCX：未测量，四个值恒为 0.0（python-docx 不提供版面坐标）。
    """

    x0: float = Field(0.0, description="左上角x坐标（单位见类 docstring）")
    y0: float = Field(0.0, description="左上角y坐标（单位见类 docstring）")
    x1: float = Field(0.0, description="右下角x坐标（单位见类 docstring）")
    y1: float = Field(0.0, description="右下角y坐标（单位见类 docstring）")


class Font(_DocModel):
    """Font styling and text content.

    字号单位统一为 pt：DOCX 由 python-docx 直接读取，PDF 取 PyMuPDF
    span size，扫描件由 OCR 框高校准估算。
    """

    font_family: str = Field("", description="字体")
    font_size: float = Field(0.0, ge=0, description="字号（pt；0 表示未测得）")
    font_weight: bool = Field(False, description="字重（加粗）")
    font_style: bool = Field(False, description="字形（倾斜）")
    text: str = Field("", description="内容")
    line_no: int = Field(0, description="字所在的行号（0 基，页内编号）")


class LineElement(_DocModel):
    """一行文字及其格式（位置 + 字体）。

    由原 MetaData 改名而来：它描述的就是"一行文字元素"；spec.py 中已有
    规范侧的 Element，为避免同名冲突改用 LineElement。
    JSON 契约不变——序列化结果只含字段名，不含类名。
    """

    position: Position = Field(default_factory=Position, description="位置")
    font: Font = Field(default_factory=Font, description="字体")


class Paragraph(_DocModel):
    """Paragraph formatting.

    间距/缩进字段均为 Optional（单位 pt）：None 表示未提取，
    0.0 表示实测为零——以此区分"没测"和"为零"。
    """

    space_before: float | None = Field(None, description="段前间距（pt；None=未提取）")
    space_after: float | None = Field(None, description="段后间距（pt；None=未提取）")
    line_spacing: float | None = Field(None, description="行距（pt；None=未提取）")
    first_indent: float | None = Field(None, description="首行缩进（pt；None=未提取）")
    left_indent: float | None = Field(None, description="文本之前缩进（pt；None=未提取）")
    right_indent: float | None = Field(None, description="文本之后缩进（pt；None=未提取）")
    elements: list[LineElement] = Field(default_factory=list, description="段落中的所有元素")
    outline_level: Literal[
        "heading1", "heading2", "heading3", "heading4", "heading5", "body_text", "others"
    ] = Field("others", description="文本所属大纲等级")
    alignment: Alignment = Field("left", description="对齐方式: left, center, right, justify")


class Margin(_DocModel):
    """Page margins (mm)."""

    top_margin: float = Field(0.0, ge=0, description="上边距（mm）")
    bottom_margin: float = Field(0.0, ge=0, description="下边距（mm）")
    left_margin: float = Field(0.0, ge=0, description="左边距（mm）")
    right_margin: float = Field(0.0, ge=0, description="右边距（mm）")


class Header(_DocModel):
    """Document header. 所有槽位 Optional：None 表示该页无此要素。"""

    copy_number: Paragraph | None = Field(None, description="份号")
    classification_duration: Paragraph | None = Field(None, description="密级和保密期限")
    urgency_level: Paragraph | None = Field(None, description="紧急程度")
    issuing_logo: Paragraph | None = Field(None, description="发文机关标志")
    issuing_number: Paragraph | None = Field(None, description="发文字号")
    signatory: Paragraph | None = Field(None, description="签发人")
    ruling_line_pos: Paragraph | None = Field(None, description="红色分割线位置")


class Body(_DocModel):
    """Document body. 段落槽位 Optional；main_text 保持列表（可为空）。"""

    title: Paragraph | None = Field(None, description="标题")
    addressee: Paragraph | None = Field(None, description="主送机关")
    main_text: list[Paragraph] = Field(default_factory=list, description="正文段列表")
    attachment_note: Paragraph | None = Field(None, description="附件说明")
    issuing_signature: Paragraph | None = Field(None, description="发文机关署名")
    issue_date: Paragraph | None = Field(None, description="成文日期")
    stamp: Paragraph | None = Field(None, description="印章")
    note: Paragraph | None = Field(None, description="附注")
    attachments: Paragraph | None = Field(None, description="附件")


class Footer(_DocModel):
    """Document footer. 所有槽位 Optional：None 表示该页无此要素。"""

    closing_line: Paragraph | None = Field(None, description="黑色反线")
    carbon_copy: Paragraph | None = Field(None, description="抄送机关")
    issuing_office: Paragraph | None = Field(None, description="印发机关")
    distribution_date: Paragraph | None = Field(None, description="印发日期")
    page_number: Paragraph | None = Field(None, description="页码")


class PageContent(_DocModel):
    """Single page content."""

    header: Header = Field(default_factory=Header, description="版头")
    body: Body = Field(default_factory=Body, description="主体")
    footer: Footer = Field(default_factory=Footer, description="版记")
    margin: Margin = Field(default_factory=Margin, description="页边距")


class Page(_DocModel):
    """Page model."""

    page_content: PageContent = Field(default_factory=PageContent, description="单页信息")
    page_no: int = Field(0, ge=0, description="页码（0 基）")


class Document(_DocModel):
    """Document model."""

    schema_version: str = Field(
        "1.0", description="模型结构版本号，供下游（缓存/DB/插件）识别序列化格式"
    )
    source: str = Field(
        "",
        description=(
            '解析来源（"docx"/"pdf"/"scanned"，由 docparse registry 在分派后回填；'
            '混排 PDF 走扫描管线的记 "scanned"）。空串=未知（直接调用单个 parser、'
            '或旧缓存反序列化）。供下游用显式来源替代 "position 恒 0" 启发式。'
        ),
    )
    doc_id: str = Field("", description="文件内容的sha256值")
    total_page_num: int = Field(0, description="文件总页数")
    save_path: str = Field("", description="文件保存路径")
    pages: list[Page] = Field(default_factory=list, description="文件所有页的集合")
    warnings: list[str] = Field(
        default_factory=list,
        description="解析过程中产生的告警信息（如扫描页未识别、单页提取失败等）",
    )

    @model_validator(mode="after")
    def _backfill_total_page_num(self) -> Document:
        """pages 非空且 total_page_num 为 0 时回填 len(pages)。

        逻辑页语义允许 total_page_num != len(pages)（例如 DOCX 无显式
        分页符时整篇算 1 个逻辑页），因此只回填、不校验一致性。
        """
        if self.pages and self.total_page_num == 0:
            self.total_page_num = len(self.pages)
        return self
