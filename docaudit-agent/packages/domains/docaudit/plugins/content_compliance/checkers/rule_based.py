from ..core import ComplianceResult
from ..engine import RuleConfig, RuleEngine


class RuleBasedContentChecker:
    """基于规则配置的通用内容检查器。"""

    def __init__(
        self, doc_type: str, subtype_rules: dict[str, list[RuleConfig]]
    ) -> None:
        self._doc_type = doc_type
        self._subtype_rules = subtype_rules

    @property
    def doc_type(self) -> str:
        return self._doc_type

    def list_subtypes(self) -> list[str]:
        return list(self._subtype_rules.keys())

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        if not text:
            raise ValueError("text must not be empty")

        if subtype is None:
            if len(self._subtype_rules) == 1:
                subtype = next(iter(self._subtype_rules.keys()))
            else:
                supported = ", ".join(self._subtype_rules.keys())
                raise ValueError(f"必须指定子类型，支持的子类型: {supported}")

        if subtype not in self._subtype_rules:
            supported = ", ".join(self._subtype_rules.keys())
            raise ValueError(f"不支持的子类型 '{subtype}'，支持的子类型：{supported}")

        rules = self._subtype_rules[subtype]
        engine = RuleEngine(rules)
        violations = engine.check(text)
        return ComplianceResult(
            is_valid=len(violations) == 0,
            violations=tuple(violations),
        )
