
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer
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
    "heading1", "heading2", "heading3", "heading4", "heading5", "body_text", "others"
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
    space_before: float = Field(..., description="段前间距")
    space_after: float = Field(..., description="段后间距")
    line_spacing: float = Field(..., description="行距")
    first_indent: float = Field(..., description="首行缩进")
    left_indent: float = Field(0.0, description="文本之前缩进(pt)")
    right_indent: float = Field(0.0, description="文本之后缩进(pt)")
    block_no: int = Field(..., description="块序号")
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
    block_no: int | None = None
    outline_level: str | None = None

class ParagraphTable(Base):
    """段落表 ORM 模型"""

    __tablename__ = "paragraphs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pages.id", ondelete="CASCADE"), nullable=False,
        comment="所属页面ID"
    )
    section_type: Mapped[str] = mapped_column(
        Enum(*SECTION_TYPE_ENUM), nullable=False, comment="段落类型"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="同一类型内的顺序"
    )
    space_before: Mapped[float] = mapped_column(Float, nullable=False, comment="段前间距")
    space_after: Mapped[float] = mapped_column(Float, nullable=False, comment="段后间距")
    line_spacing: Mapped[float] = mapped_column(Float, nullable=False, comment="行距")
    first_indent: Mapped[float] = mapped_column(Float, nullable=False, comment="首行缩进")
    left_indent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="文本之前缩进(pt)"
    )
    right_indent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="文本之后缩进(pt)"
    )
    block_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="块序号")
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
