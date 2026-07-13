"""RefreshToken ORM model — persisted for httpOnly-cookie token rotation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class RefreshTokenTable(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="Token ID")
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, comment="关联用户ID"
    )
    token_hash: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, comment="SHA-256 哈希后的 refresh token")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="过期时间（7天）")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否已撤销")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")

    def __repr__(self) -> str:
        return f"<RefreshToken {self.id} user={self.user_id} revoked={self.revoked}>"
