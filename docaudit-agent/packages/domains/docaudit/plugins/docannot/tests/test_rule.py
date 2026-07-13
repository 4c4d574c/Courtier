import pytest

from docannot import Rule


class TestRule:
    def test_create_rule(self) -> None:
        rule = Rule(keyword="落实", comment="请检查", author="校对员")
        assert rule.keyword == "落实"
        assert rule.comment == "请检查"
        assert rule.author == "校对员"

    def test_default_author(self) -> None:
        rule = Rule(keyword="测试", comment="批注")
        assert rule.author == "docannot"

    def test_frozen(self) -> None:
        rule = Rule(keyword="测试", comment="批注")
        with pytest.raises(AttributeError):
            rule.keyword = "其他"  # type: ignore[misc]

    def test_empty_keyword_raises(self) -> None:
        with pytest.raises(ValueError, match="keyword must be non-empty"):
            Rule(keyword="", comment="批注")

    def test_equality(self) -> None:
        r1 = Rule(keyword="测试", comment="批注")
        r2 = Rule(keyword="测试", comment="批注")
        assert r1 == r2

    def test_hashable(self) -> None:
        rule = Rule(keyword="测试", comment="批注")
        assert hash(rule) is not None
