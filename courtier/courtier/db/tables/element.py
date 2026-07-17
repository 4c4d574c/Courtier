from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .paragraph import ParagraphTable

from .base import Base, utcnow


class ElementCreate(BaseModel):
    """创建文本元素时的数据模型"""

    paragraph_id: int = Field(..., description="所属段落ID")
    x0: float = Field(..., description="左上角x坐标")
    y0: float = Field(..., description="左上角y坐标")
    x1: float = Field(..., description="右下角x坐标")
    y1: float = Field(..., description="右下角y坐标")
    font_family: str = Field(..., description="字体")
    font_size: float = Field(..., description="字号")
    font_weight: bool = Field(..., description="是否加粗")
    font_style: bool = Field(..., description="是否倾斜")
    text: str = Field(..., description="文本内容")
    line_no: int = Field(..., description="行号")
    exist: bool = Field(default=True, description="是否存在")
    create_time: datetime = Field(
        default_factory=utcnow, description="创建时间"
    )

class ElementUpdate(BaseModel):
    """更新文本元素时的数据模型"""

    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None
    font_family: str | None = None
    font_size: float | None = None
    font_weight: bool | None = None
    font_style: bool | None = None
    text: str | None = None
    line_no: int | None = None
    exist: bool | None = None

class ElementTable(Base):
    """文本元素表 ORM 模型"""

    __tablename__ = "elements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paragraph_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("paragraphs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属段落ID",
    )
    x0: Mapped[float] = mapped_column(Float, nullable=False, comment="左上角x坐标")
    y0: Mapped[float] = mapped_column(Float, nullable=False, comment="左上角y坐标")
    x1: Mapped[float] = mapped_column(Float, nullable=False, comment="右下角x坐标")
    y1: Mapped[float] = mapped_column(Float, nullable=False, comment="右下角y坐标")
    font_family: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="字体"
    )
    font_size: Mapped[float] = mapped_column(Float, nullable=False, comment="字号")
    font_weight: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否加粗"
    )
    font_style: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否倾斜"
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="文本内容")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="行号")
    exist: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否存在"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    # 关系
    paragraph: Mapped["ParagraphTable"] = relationship(back_populates="elements")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Element {self.id} (text={self.text[:20]})>"
