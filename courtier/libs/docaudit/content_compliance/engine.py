import re
from dataclasses import dataclass
from typing import Literal

from .core import CheckScope, Violation

CheckType = Literal["required", "forbidden"]


@dataclass(frozen=True)
class RuleConfig:
    """单条规则配置。"""

    id: str
    name: str
    check_scope: CheckScope
    type: CheckType
    pattern: str
    message: str
    check_length: int | None = None


class RuleEngine:
    """基于正则的通用规则引擎。"""

    def __init__(self, rules: list[RuleConfig]) -> None:
        self._compiled: list[tuple[RuleConfig, re.Pattern]] = [
            (rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in rules
        ]

    def _extract_scope(self, text: str, rule: RuleConfig) -> str:
        """根据检查范围提取待检查文本。"""
        match rule.check_scope:
            case "开头":
                if rule.check_length is None:
                    raise ValueError(f"Rule '{rule.id}' has check_scope='开头' but check_length is unset")
                return text[: rule.check_length]
            case "结尾":
                if rule.check_length is None:
                    raise ValueError(f"Rule '{rule.id}' has check_scope='结尾' but check_length is unset")
                return text[-rule.check_length :] if len(text) > rule.check_length else text
            case "正文" | "全文":
                return text
            case _:
                return text

    def check(self, text: str) -> list[Violation]:
        """对文本执行所有规则检查。"""
        violations: list[Violation] = []
        for rule, compiled in self._compiled:
            scope_text = self._extract_scope(text, rule)
            matched = bool(compiled.search(scope_text))

            if rule.type == "required" and not matched:
                violations.append(
                    Violation(
                        rule_id=rule.id,
                        message=rule.message,
                        severity="error",
                        position=rule.check_scope,
                    )
                )
            elif rule.type == "forbidden" and matched:
                violations.append(
                    Violation(
                        rule_id=rule.id,
                        message=rule.message,
                        severity="error",
                        position=rule.check_scope,
                    )
                )
        return violations
