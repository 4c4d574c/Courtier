
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

if TYPE_CHECKING:
    from .document import DocumentTable
    from .paragraph import ParagraphTable


class PageCreate(BaseModel):
    """创建页面时的数据模型"""

    page_no: int = Field(..., description="页码")
    save_path: str = Field(default="默认路径", description="保存路径")
    raw: bytes | None = Field(default=b"", description="原始内容")
    top_margin: float = Field(..., description="上边距")
    bottom_margin: float = Field(..., description="下边距")
    left_margin: float = Field(..., description="左边距")
    right_margin: float = Field(..., description="右边距")
    create_time: datetime = Field(default_factory=utcnow, description="创建时间")

class PageUpdate(BaseModel):
    """更新页面时的数据模型（所有字段可选）"""

    page_no: int | None = None
    save_path: str | None = None
    raw: bytes | None = None
    top_margin: float | None = None
    bottom_margin: float | None = None
    left_margin: float | None = None
    right_margin: float | None = None

class PageTable(Base):
    """页面表 ORM 模型"""

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
        comment="所属文档ID"
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="页码")
    save_path: Mapped[str] = mapped_column(
        String(512), nullable=False, default="默认路径", comment="保存路径"
    )
    raw: Mapped[bytes | None] = mapped_column(LONGBLOB, default=b"", comment="原始内容")
    top_margin: Mapped[float] = mapped_column(Float, nullable=False, comment="上边距")
    bottom_margin: Mapped[float] = mapped_column(Float, nullable=False, comment="下边距")
    left_margin: Mapped[float] = mapped_column(Float, nullable=False, comment="左边距")
    right_margin: Mapped[float] = mapped_column(Float, nullable=False, comment="右边距")
    create_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")

    # 关系
    document: Mapped["DocumentTable"] = relationship(back_populates="pages")
    paragraphs: Mapped[list["ParagraphTable"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Page {self.id} (page_no={self.page_no})>"
