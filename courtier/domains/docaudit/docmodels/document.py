from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Position(BaseModel):
    """Represents a rectangular region on the page."""
    x0: float = Field(0.0, description="左上角x坐标")
    y0: float = Field(0.0, description="左上角y坐标")
    x1: float = Field(0.0, description="右下角x坐标")
    y1: float = Field(0.0, description="右下角y坐标")

class Font(BaseModel):
    """Font styling and text content."""
    font_family: str = Field("", description="字体")
    font_size: float = Field(0.0, description="字号")
    font_weight: bool = Field(False, description="字重（加粗）")
    font_style: bool = Field(False, description="字形（倾斜）")
    text: str = Field("", description="内容")
    line_no: int = Field(0, description="字所在的行号")

class MetaData(BaseModel):
    """Common metadata structure."""
    exist: bool = Field(False, description="存在与否")
    position: Position = Field(default_factory=Position, description="位置")
    font: Font = Field(default_factory=Font, description="字体")

class Paragraph(BaseModel):
    """Paragraph formatting."""
    space_before: float = Field(0.0, description="段前间距")
    space_after: float = Field(0.0, description="段后间距")
    line_spacing: float = Field(0.0, description="行距")
    first_indent: float = Field(0.0, description="首行缩进")
    left_indent: float = Field(0.0, description="文本之前缩进(pt)")
    right_indent: float = Field(0.0, description="文本之后缩进(pt)")
    block_no: int = Field(0, description="块的序号")
    elements: list[MetaData] = Field(default_factory=list, description="段落中的所有元素")
    outline_level: Literal[
        "heading1", "heading2", "heading3", "heading4",
        "heading5", "body_text", "others", "ruling_line", "closing_line"
    ] = Field("others", description="文本所属大纲等级")
    alignment: Literal["left", "center", "right", "justify"] = Field(
        "left", description="对齐方式: left, center, right, justify"
    )

class Margin(BaseModel):
    """Page margins."""
    top_margin: float = Field(0.0, description="上边距(mm)")
    bottom_margin: float = Field(0.0, description="下边距(mm)")
    left_margin: float = Field(0.0, description="左边距(mm)")
    right_margin: float = Field(0.0, description="右边距(mm)")

class Header(BaseModel):
    """Document header."""
    copy_number: Paragraph | None = Field(None, description="份号")
    classification_duration: Paragraph | None = Field(None, description="密级和保密期限")
    urgency_level: Paragraph | None = Field(None, description="紧急程度")
    issuing_logo: Paragraph = Field(default_factory=Paragraph, description="发文机关标志")
    issuing_number: Paragraph = Field(default_factory=Paragraph, description="发文字号")
    signatory: Paragraph = Field(default_factory=Paragraph, description="签发人")
    ruling_line_pos: Paragraph = Field(default_factory=Paragraph, description="红色分割线位置")

class Body(BaseModel):
    """Document body."""
    title: Paragraph = Field(default_factory=Paragraph, description="标题")
    addressee: Paragraph = Field(default_factory=Paragraph, description="主送机关")
    main_text: list[Paragraph] = Field(default_factory=list, description="正文段列表")
    attachment_note: Paragraph | None = Field(None, description="附件说明")
    issuing_signature: Paragraph = Field(default_factory=Paragraph, description="发文机关署名")
    issue_date: Paragraph = Field(default_factory=Paragraph, description="成文日期")
    stamp: Paragraph | None = Field(None, description="印章")
    note: Paragraph | None = Field(None, description="附注")
    attachments: Paragraph | None = Field(None, description="附件")

class Footer(BaseModel):
    """Document footer."""
    closing_line: Paragraph = Field(default_factory=Paragraph, description="黑色反线")
    carbon_copy: Paragraph | None = Field(None, description="抄送机关")
    issuing_office: Paragraph = Field(default_factory=Paragraph, description="印发机关")
    distribution_date: Paragraph = Field(default_factory=Paragraph, description="印发日期")
    page_number: Paragraph = Field(default_factory=Paragraph, description="页码")

class PageContent(BaseModel):
    """Single page content."""
    header: Header = Field(default_factory=Header, description="版头")
    body: Body = Field(default_factory=Body, description="主体")
    footer: Footer = Field(default_factory=Footer, description="版记")
    margin: Margin = Field(default_factory=Margin, description="页边距")

class Page(BaseModel):
    """Page model."""
    raw: bytes = Field(b'', description="原始内容")
    page_content: PageContent = Field(default_factory=PageContent, description="单页信息")
    save_path: str = Field("", description="该页的保存路径")
    page_no: int = Field(0, description="页码")

class Document(BaseModel):
    """Document model"""
    user_id: str = Field("", description="文件所属用户的id值")
    doc_id: str = Field("", description="文件的内容的md5值")
    total_page_num: int = Field(0, description="文件总页数")
    save_path: str = Field("", description="文件保存路径")
    pages: list[Page] = Field(default_factory=list, description="文件所有页的集合")
