from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class RuleDomain(Base):
    """规则领域表"""
    __tablename__ = "rule_domain"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, comment="领域ID（如GOV、AUD等）")
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, comment="领域名称")
    description: Mapped[str | None] = mapped_column(Text, comment="领域描述")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )


class RuleFile(Base):
    """规则文件表"""
    __tablename__ = "rule_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="文件名")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="存储路径")
    file_type: Mapped[str | None] = mapped_column(String(50), comment="文件类型")
    size: Mapped[int | None] = mapped_column(Integer, comment="文件大小")
    domain_id: Mapped[str | None] = mapped_column(String(20), comment="所属领域ID（如GOV、AUD等）")
    status: Mapped[str] = mapped_column(
        String(20), default="uploaded", comment="状态：uploaded, processed, failed"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="上传时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )


class Rule(Base):
    """规则表"""
    __tablename__ = "rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="规则名称")
    description: Mapped[str | None] = mapped_column(Text, comment="规则描述")
    pattern: Mapped[str] = mapped_column(
        Text, nullable=False, comment="规则模式（正则表达式或其他格式）"
    )
    severity: Mapped[str] = mapped_column(
        String(20), default="warning", comment="严重程度：error, warning, info"
    )
    domain_id: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="所属领域ID（如GOV、AUD等）"
    )
    rule_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("rule_file.id"), comment="来源文件"
    )
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否手动添加")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, comment="更新时间"
    )
