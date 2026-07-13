"""docannot - 基于关键词的 DOCX 批注库。"""

from . import _patch  # noqa: F401 — apply monkey-patch before any docxnote usage
from ._annotate import annotate
from ._rule import Rule

__all__ = ["Rule", "annotate"]
