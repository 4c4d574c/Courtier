from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class FormatAuditResultTable(Base):
    """格式审核结果表"""

    __tablename__ = "format_audit_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="公文类型，如'通知'"
    )
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="JSON: [{block_name, block_no, error_type, details, page_no}]",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return f"<FormatAuditResult {self.id} (doc={self.document_id}, errors={self.error_count})>"


class ContentAuditResultTable(Base):
    """内容审核结果表"""

    __tablename__ = "content_audit_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    domain_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="领域名称"
    )
    violation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    violations_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="JSON: [{rule_id, violat_dsc, origin_text, severity, cor_suggest, domain_name}]",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return (
            f"<ContentAuditResult {self.id} (doc={self.document_id}, "
            f"violations={self.violation_count})>"
        )


class PlagiarismAuditResultTable(Base):
    """查重审核结果表"""

    __tablename__ = "plagiarism_audit_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    is_plagiarism: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    dynamic_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    matched_doc_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    matched_substring_length: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    matched_text: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(512), default="")
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return (
            f"<PlagiarismAuditResult {self.id} (doc={self.document_id}, "
            f"is_plagiarism={self.is_plagiarism})>"
        )


class CorrectionAuditResultTable(Base):
    """通用纠错结果表"""

    __tablename__ = "correction_audit_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    corrections_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="JSON: [{source, target, errors}]"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return (
            f"<CorrectionAuditResult {self.id} (doc={self.document_id}, "
            f"corrections={self.correction_count})>"
        )


class WritingStyleAuditResultTable(Base):
    """行文风格审查结果表"""

    __tablename__ = "writing_style_audit_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    notif_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="通知类型"
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    errors_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", comment="JSON: 错误字符串列表"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, comment="创建时间"
    )

    def __repr__(self) -> str:
        return (
            f"<WritingStyleAuditResult {self.id} (doc={self.document_id}, "
            f"is_valid={self.is_valid})>"
        )
