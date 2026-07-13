from pathlib import Path

import pytest

from content_compliance.loaders.json_loader import load_checker_config
from content_compliance.checkers.rule_based import RuleBasedContentChecker


RULES_DIR = Path(__file__).parent.parent.parent / "src" / "content_compliance" / "rules"


@pytest.fixture(scope="module")
def loaded_rules():
    """Load all rule JSON files once per module (instead of per test function)."""
    rules = {}
    rule_files = [
        ("通知", RULES_DIR / "通知.json"),
        ("请示", RULES_DIR / "请示.json"),
        ("报告", RULES_DIR / "报告.json"),
        ("批复", RULES_DIR / "批复.json"),
        ("函", RULES_DIR / "函.json"),
    ]
    for doc_type, path in rule_files:
        if not path.exists():
            pytest.skip(f"Rule file not found: {path}")
        doc_type_name, subtypes = load_checker_config(path)
        rules[doc_type] = (doc_type_name, subtypes)
    return rules


@pytest.fixture(scope="module")
def notification_rules(loaded_rules):
    return loaded_rules["通知"]


@pytest.mark.asyncio
async def test_notification_published_passes(notification_rules):
    doc_type, subtypes = notification_rules
    assert doc_type == "通知"
    assert "发布性通知" in subtypes

    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "现将《XXX办法》发布，请遵照执行。"
    result = await checker.check(text, subtype="发布性通知")
    assert result.is_valid is True


@pytest.mark.asyncio
async def test_notification_published_fails_missing_book_title(notification_rules):
    doc_type, subtypes = notification_rules
    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "现发布XXX办法，请遵照执行。"
    result = await checker.check(text, subtype="发布性通知")
    assert result.is_valid is False
    assert any("书名号" in v.message for v in result.violations)


@pytest.mark.asyncio
async def test_request_fails_missing_ending(loaded_rules):
    doc_type, subtypes = loaded_rules["请示"]
    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "现将有关情况请示如下。"
    result = await checker.check(text, subtype="请示")
    assert result.is_valid is False
    assert any("请示结语" in v.message for v in result.violations)


@pytest.mark.asyncio
async def test_report_fails_with_request_content(loaded_rules):
    doc_type, subtypes = loaded_rules["报告"]
    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "现将有关情况报告如下。妥否，请批示。"
    result = await checker.check(text, subtype="报告")
    assert result.is_valid is False
    assert any("请示事项" in v.message for v in result.violations)


@pytest.mark.asyncio
async def test_reply_fails_missing_reference(loaded_rules):
    doc_type, subtypes = loaded_rules["批复"]
    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "同意你们的意见。此复。"
    result = await checker.check(text, subtype="批复")
    assert result.is_valid is False
    assert any("请示文号" in v.message for v in result.violations)


@pytest.mark.asyncio
async def test_letter_passes(loaded_rules):
    doc_type, subtypes = loaded_rules["函"]
    checker = RuleBasedContentChecker(doc_type, subtypes)
    text = "兹函告贵单位，关于XX事项，请函复。"
    result = await checker.check(text, subtype="函")
    assert result.is_valid is True
