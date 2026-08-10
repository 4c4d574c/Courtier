from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field
from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ResourceCreate(BaseModel):
    """创建资源库条目时的数据模型"""

    title: str = Field(..., description="标题")
    author: str | None = Field(None, description="作者")
    source: str | None = Field(None, description="来源")
    tags: str | None = Field(None, description="标签，逗号分隔")
    publish_date: date | None = Field(None, description="发布日期")
    file_type: str = Field(..., description="文件类型")
    file_size: int = Field(..., description="文件大小（字节）")
    minio_path: str = Field(..., description="MinIO 对象路径")
    md5: str = Field(..., description="文件 MD5")
    chunk_count: int = Field(default=0, description="切片数量")
    char_count: int = Field(default=0, description="总字符数")
    status: str = Field(default="ready", description="状态：ready / processing / error")
    resource_type: str = Field(
        default="manual", description="资源类型：manual / original / annotated"
    )
    document_id: int | None = Field(None, description="关联文档 ID")
    owner_id: int | None = Field(None, description="上传者用户 ID（个人资源库归属）")
    visibility: str = Field(
        default="public", description="可见性：personal（个人库）/ public（公共库）"
    )


class ResourceUpdate(BaseModel):
    """更新资源库条目时的数据模型"""

    title: str | None = None
    author: str | None = None
    source: str | None = None
    tags: str | None = None
    publish_date: date | None = None
    file_type: str | None = None
    file_size: int | None = None
    minio_path: str | None = None
    md5: str | None = None
    chunk_count: int | None = None
    char_count: int | None = None
    status: str | None = None
    resource_type: str | None = None
    document_id: int | None = None
    owner_id: int | None = None
    visibility: str | None = None


class ResourceTable(Base):
    """资源库表 ORM 模型"""

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, comment="标题")
    author: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="作者")
    source: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="来源")
    tags: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="标签，逗号分隔")
    publish_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="发布日期")
    file_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="文件类型")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, comment="文件大小（字节）")
    minio_path: Mapped[str] = mapped_column(String(512), nullable=False, comment="MinIO 对象路径")
    md5: Mapped[str] = mapped_column(String(64), nullable=False, comment="文件 MD5")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, comment="切片数量")
    char_count: Mapped[int] = mapped_column(Integer, default=0, comment="总字符数")
    status: Mapped[str] = mapped_column(
        String(32), default="ready", comment="状态：ready / processing / error"
    )
    resource_type: Mapped[str] = mapped_column(
        String(32),
        default="manual",
        comment="资源类型：manual / original / annotated",
    )
    document_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
        default=None,
        comment="关联文档 ID",
    )
    owner_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        default=None,
        comment="上传者用户 ID（个人资源库归属；NULL 为存量公共数据）",
    )
    visibility: Mapped[str] = mapped_column(
        String(16),
        default="public",
        comment="可见性：personal（个人库）/ public（公共库）",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<Resource {self.id} (title={self.title})>"
