
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

class LibraryCreate(BaseModel):
    """创建查重库条目时的数据模型"""

    doc_id: str = Field(..., description="文档的uuid")
    doc_name: str = Field(..., description="文件名称")
    total_page_num: int = Field(..., description="文档的总页数")
    page_no: int = Field(..., description="当前页的页码")
    block_no: int = Field(..., description="当前文本所属的块")
    block_text: str = Field(..., description="当前文本块内容")
    create_time: datetime = Field(default_factory=utcnow, description="创建时间")

class LibraryUpdate(BaseModel):
    """更新查重库条目时的数据模型"""

    doc_id: str | None = None
    doc_name: str | None = None
    total_page_num: int | None = None
    page_no: int | None = None
    block_no: int | None = None
    block_text: str | None = None

class LibraryTable(Base):
    """查重库表 ORM 模型"""

    __tablename__ = "librarys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="文档的uuid"
    )
    doc_name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="文件名称"
    )
    total_page_num: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="文档的总页数"
    )
    page_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="当前页的页码"
    )
    block_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="当前文本所属的块"
    )
    block_text: Mapped[str] = mapped_column(
        String(4096), nullable=False, comment="当前文本块内容"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return f"<Library {self.id} (doc={self.doc_name})>"
