from typing import Literal, Protocol, runtime_checkable

from courtier_plugin_sdk.types import ComplianceResult, Violation
from courtier_plugin_sdk.types import ContentChecker as _CoreContentChecker

CheckScope = Literal["开头", "结尾", "正文", "全文"]


# Re-export core types for backward compatibility
__all__ = [
    "CheckScope",
    "Violation",
    "ComplianceResult",
    "ContentChecker",
]


@runtime_checkable
class ContentChecker(_CoreContentChecker, Protocol):
    """内容合规检查器协议 —— 每种文种一个实现。

    Extends the core ContentChecker protocol. Domain-specific checkers
    implement this interface.
    """

    @property
    def doc_type(self) -> str:
        """支持的公文类型，如 '通知'、'请示'。"""
        ...

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        """对公文正文进行合规检查。"""
        ...
