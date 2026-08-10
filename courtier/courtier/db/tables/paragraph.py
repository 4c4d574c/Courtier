from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .._utils import SECTION_FIELDS
from .base import Base, utcnow

if TYPE_CHECKING:
    from .element import ElementTable
    from .page import PageTable

# 定义段落类型的枚举（对应文档中的区域），从 SECTION_FIELDS 单一来源生成
SECTION_TYPE_ENUM = [entry[0] for entry in SECTION_FIELDS]

# 大纲等级枚举
OUTLINE_LEVEL_ENUM = [
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "body_text",
    "others",
]


class ParagraphCreate(BaseModel):
    """创建段落时的数据模型"""

    page_id: int = Field(..., description="所属页面ID")
    section_type: str = Field(
        ...,
        description="段落类型（所属区域）",
        # json_schema_extra 要求 JsonDict 值类型；list 不变性导致 list[str] 不兼容，故 cast
        json_schema_extra={"enum": cast(list[JsonValue], SECTION_TYPE_ENUM)},
    )
    order_index: int = Field(default=0, description="同一类型内的顺序")
    # 间距/缩进 6 字段与 docmodels.Paragraph 对齐：Optional，None 表示未提取
    space_before: float | None = Field(None, description="段前间距（None=未提取）")
    space_after: float | None = Field(None, description="段后间距（None=未提取）")
    line_spacing: float | None = Field(None, description="行距（None=未提取）")
    first_indent: float | None = Field(None, description="首行缩进（None=未提取）")
    left_indent: float | None = Field(None, description="文本之前缩进(pt；None=未提取)")
    right_indent: float | None = Field(None, description="文本之后缩进(pt；None=未提取)")
    alignment: str = Field(default="left", description="对齐方式: left, center, right, justify")
    outline_level: str = Field(
        ...,
        description="大纲等级",
        json_schema_extra={"enum": cast(list[JsonValue], OUTLINE_LEVEL_ENUM)},
    )
    create_time: datetime = Field(default_factory=utcnow, description="创建时间")


class ParagraphUpdate(BaseModel):
    """更新段落时的数据模型"""

    section_type: str | None = None
    order_index: int | None = None
    space_before: float | None = None
    space_after: float | None = None
    line_spacing: float | None = None
    first_indent: float | None = None
    left_indent: float | None = None
    right_indent: float | None = None
    alignment: str | None = None
    outline_level: str | None = None


class ParagraphTable(Base):
    """段落表 ORM 模型"""

    __tablename__ = "paragraphs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, comment="所属页面ID"
    )
    section_type: Mapped[str] = mapped_column(
        Enum(*SECTION_TYPE_ENUM), nullable=False, comment="段落类型"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="同一类型内的顺序"
    )
    # 间距/缩进 6 列全部 nullable：None 表示"未提取"，与 0.0（实测为零）区分。
    space_before: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="段前间距"
    )
    space_after: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="段后间距"
    )
    line_spacing: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="行距"
    )
    first_indent: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="首行缩进"
    )
    left_indent: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="文本之前缩进(pt)"
    )
    right_indent: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None, comment="文本之后缩进(pt)"
    )
    # 对齐方式（left/center/right/justify）。用 String 而非 Enum 与模型 Literal
    # 对齐即可；nullable——应用层总是写入实际值（模型默认 "left"），NULL 仅
    # 标记该列新增之前的存量行，读出时按 NULL → "left" 归一化。
    alignment: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default=None, comment="对齐方式"
    )
    outline_level: Mapped[str] = mapped_column(
        Enum(*OUTLINE_LEVEL_ENUM), nullable=False, comment="大纲等级"
    )
    create_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")

    # 关系
    page: Mapped["PageTable"] = relationship(back_populates="paragraphs")
    elements: Mapped[list["ElementTable"]] = relationship(
        back_populates="paragraph", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Paragraph {self.id} (type={self.section_type})>"
