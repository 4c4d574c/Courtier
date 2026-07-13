import pytest

from content_compliance.checkers.rule_based import RuleBasedContentChecker
from content_compliance.engine import RuleConfig


@pytest.mark.asyncio
async def test_rule_based_checker_basic():
    rules = {
        "发布性": [
            RuleConfig(
                id="PUB_001",
                name="has book title",
                check_scope="开头",
                check_length=50,
                type="required",
                pattern="《",
                message="缺少书名号",
            ),
        ],
    }
    checker = RuleBasedContentChecker("通知", rules)
    assert checker.doc_type == "通知"
    assert checker.list_subtypes() == ["发布性"]

    result = await checker.check("《 test 》发布通知，请遵照执行。", subtype="发布性")
    assert result.is_valid is True
    assert result.violations == ()


@pytest.mark.asyncio
async def test_rule_based_checker_fails():
    rules = {
        "发布性": [
            RuleConfig(
                id="PUB_001",
                name="has book title",
                check_scope="开头",
                check_length=50,
                type="required",
                pattern="《",
                message="缺少书名号",
            ),
        ],
    }
    checker = RuleBasedContentChecker("通知", rules)
    result = await checker.check("发布通知，请遵照执行。", subtype="发布性")
    assert result.is_valid is False
    assert len(result.violations) == 1
    assert result.violations[0].message == "缺少书名号"


@pytest.mark.asyncio
async def test_rule_based_checker_empty_text_raises():
    rules = {"发布性": []}
    checker = RuleBasedContentChecker("通知", rules)
    with pytest.raises(ValueError, match="text must not be empty"):
        await checker.check("", subtype="发布性")


@pytest.mark.asyncio
async def test_rule_based_checker_invalid_subtype():
    rules = {"发布性": []}
    checker = RuleBasedContentChecker("通知", rules)
    with pytest.raises(ValueError, match="不支持的子类型"):
        await checker.check("test", subtype="不存在")


@pytest.mark.asyncio
async def test_rule_based_checker_auto_subtype_when_single():
    rules = {"唯一": []}
    checker = RuleBasedContentChecker("测试", rules)
    result = await checker.check("test text")
    assert result.is_valid is True


@pytest.mark.asyncio
async def test_rule_based_checker_requires_subtype_when_multiple():
    rules = {"A": [], "B": []}
    checker = RuleBasedContentChecker("测试", rules)
    with pytest.raises(ValueError, match="必须指定子类型"):
        await checker.check("test text")
