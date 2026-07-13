
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

class DocumentCreate(BaseModel):
    """创建文档时的数据模型"""

    user_id: str = Field(..., description="文件所属用户的id值")
    doc_id: str = Field(..., description="文件的内容的md5值")
    total_page_num: int = Field(..., description="文件总页数")
    save_path: str = Field(default="默认路径", description="保存路径")

class DocumentUpdate(BaseModel):
    """更新文档时的数据模型（所有字段可选）"""

    user_id: str | None = None
    doc_id: str | None = None
    total_page_num: int | None = None
    save_path: str | None = None
    reconstructed_path: str | None = None

class DocumentTable(Base):
    """文档表 ORM 模型"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(512), nullable=False, default="", comment="文件所属用户的id值")
    doc_id: Mapped[str] = mapped_column(String(512), nullable=False, default="", comment="文件的内容的md5值")
    total_page_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="文件总页数")
    save_path: Mapped[str] = mapped_column(String(512), nullable=False, default="默认路径", comment="保存路径")
    reconstructed_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, default=None,
        comment="重建/转换后的docx MinIO存储路径"
    )
    create_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")
    imported_to_resource: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已导入资源库"
    )

    # 关系
    pages: Mapped[list["PageTable"]] = relationship(back_populates="document", cascade="all, delete-orphan")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Document {self.id} (doc_id={self.doc_id})>"
