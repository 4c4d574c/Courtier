"""User ORM model and Pydantic schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class UserRole(str, Enum):
    admin = "admin"
    auditor = "auditor"


class UserStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    pending = "pending"


class UserTable(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="用户ID")
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, comment="用户名"
    )
    password_hash: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="bcrypt 密码哈希"
    )
    email: Mapped[str] = mapped_column(String(128), nullable=False, default="", comment="邮箱地址")
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole), nullable=False, default=UserRole.auditor,
        comment="角色：admin / auditor"
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus), nullable=False, default=UserStatus.pending,
        comment="状态：active / disabled / pending"
    )
    avatar_url: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", comment="头像URL"
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="连续登录失败次数"
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, comment="账户锁定到期时间"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<User {self.id} ({self.username}) role={self.role.value}>"


class UserCreate(BaseModel):
    """创建用户时的数据模型"""

    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(
        ..., min_length=8, max_length=128, description="密码（明文，服务端哈希后存储）"
    )
    email: str = Field(default="", max_length=128, description="邮箱地址")


class UserUpdate(BaseModel):
    """更新用户时的数据模型（管理员操作，所有字段可选）"""

    role: UserRole | None = Field(default=None, description="角色：admin / auditor")
    status: UserStatus | None = Field(default=None, description="状态：active / disabled / pending")
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码（设置后覆盖原密码）"
    )
    email: str | None = Field(default=None, max_length=128, description="邮箱地址")


class ProfileUpdate(BaseModel):
    """用户自行更新个人信息的数据模型"""

    email: str | None = Field(default=None, max_length=128, description="邮箱地址")
    current_password: str | None = Field(default=None, description="当前密码（修改密码时必填）")
    new_password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码"
    )
