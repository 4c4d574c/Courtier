import logging
from pathlib import Path

from .checkers.rule_based import RuleBasedContentChecker
from .core import ComplianceResult, ContentChecker, Violation
from .loaders.json_loader import load_checker_config
from .registry import get_checker, list_supported_types, register

logger = logging.getLogger(__name__)

# 子类型 → 文种 映射
SUBTYPE_TO_DOCTYPE = {
    # 通知子类
    "发布性通知": "通知",
    "批转性通知": "通知",
    "转发性通知": "通知",
    "指示性通知": "通知",
    "任免性通知": "通知",
    "事务性通知": "通知",
    # 新增文种
    "请示": "请示",
    "报告": "报告",
    "批复": "批复",
    "函": "函",
}

__all__ = [
    "ComplianceResult",
    "ContentChecker",
    "Violation",
    "get_checker",
    "init_checkers",
    "list_supported_types",
    "register",
    "RuleBasedContentChecker",
    "SUBTYPE_TO_DOCTYPE",
]


_RULES_BASE = Path(__file__).parent / "rules"


def init_checkers(rules_base: Path | None = None) -> None:
    """Explicitly register all content compliance checkers.

    Args:
        rules_base: Path to rules directory. Uses default if None.
    """
    base = rules_base or _RULES_BASE
    if not base.exists():
        logger.warning("Rules directory %s not found, no checkers registered", base)
        return
    for rules_file in base.glob("*.json"):
        try:
            doc_type, subtypes = load_checker_config(rules_file)
            checker = RuleBasedContentChecker(doc_type, subtypes)
            register(checker)
        except Exception as e:
            logger.error("Failed to load rules from %s: %s", rules_file, e)
