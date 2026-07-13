import logging

from .core import ContentChecker

logger = logging.getLogger(__name__)


class CheckerRegistry:
    """文种 → 检查器 的注册表。"""

    def __init__(self) -> None:
        self._checkers: dict[str, ContentChecker] = {}

    def register(self, checker: ContentChecker) -> None:
        if checker.doc_type in self._checkers:
            logger.warning("Overwriting checker for doc_type '%s'", checker.doc_type)
        self._checkers[checker.doc_type] = checker

    def get(self, doc_type: str) -> ContentChecker | None:
        return self._checkers.get(doc_type)

    def unregister(self, doc_type: str) -> None:
        """Remove a checker by doc_type. No-op if not found."""
        self._checkers.pop(doc_type, None)

    def list_supported(self) -> list[str]:
        return list(self._checkers.keys())


# 全局注册表实例
_registry = CheckerRegistry()


def register(checker: ContentChecker) -> None:
    _registry.register(checker)


def get_checker(doc_type: str) -> ContentChecker | None:
    return _registry.get(doc_type)


def list_supported_types() -> list[str]:
    return _registry.list_supported()
