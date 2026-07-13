from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, DateTime, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

class FormatTemplateCreate(BaseModel):
    """创建格式模板请求。"""

    name: str = Field(..., description="模板名称")
    doc_type: str = Field(..., description="公文类型，如 通知、函、请示")
    is_default: bool = Field(default=False, description="是否为该文种默认模板")
    content: dict = Field(..., description="模板内容（JSON）")

class FormatTemplateUpdate(BaseModel):
    """更新格式模板请求。"""

    name: str | None = None
    doc_type: str | None = None
    is_default: bool | None = None
    content: dict | None = None

class FormatTemplateTable(Base):
    """格式模板表 ORM 模型。"""

    __tablename__ = "format_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="模板名称")
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="公文类型")
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否为该文种默认模板"
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否为系统内置模板（不可删除）"
    )
    content: Mapped[dict] = mapped_column(JSON, nullable=False, comment="模板内容（JSON）")
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<FormatTemplate {self.id} ({self.doc_type}: {self.name})>"
