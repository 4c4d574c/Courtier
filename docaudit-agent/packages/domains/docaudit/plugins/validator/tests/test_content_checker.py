"""测试 content_checker 模块的 prompt 和规则格式。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class FakeRule:
    def __init__(self, id, name, description, severity):
        self.id = id
        self.name = name
        self.description = description
        self.severity = severity


@pytest.mark.asyncio
async def test_load_rules_returns_compact_format():
    """_load_rules 返回的 rules_text 应使用 | 分隔的压缩格式。"""
    from validator.content_checker import ContentChecker

    fake_rules = [
        FakeRule(
            id=1, name="规则一", description="检查开头是否有发文缘由", severity="error"
        ),
        FakeRule(
            id=2,
            name="规则二",
            description="检查结尾是否有执行要求",
            severity="warning",
        ),
    ]
    fake_domain = MagicMock()
    fake_domain.id = "DOM1"
    fake_domain.name = "政务综合"

    checker = ContentChecker.__new__(ContentChecker)
    checker._client = MagicMock()
    checker._model = "test"
    checker._temperature = 0.0

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_db = MagicMock()
    mock_db.session = MagicMock(return_value=mock_session)

    checker.db = mock_db

    with patch("validator.content_checker.CRUDRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.list = AsyncMock()
        mock_repo_cls.side_effect = [mock_repo, mock_repo]

        # 第一次 list 返回领域，第二次 list 返回规则
        mock_repo.list.side_effect = [
            [fake_domain],  # RuleDomain.list
            fake_rules,  # Rule.list
        ]

        rules_text, rule_map = await checker._load_rules("政务综合")

    # 验证格式：每行应为 "R{id}|{description}|{severity}"
    assert len(rules_text) == 2
    assert rules_text[0] == "R1|检查开头是否有发文缘由|error"
    assert rules_text[1] == "R2|检查结尾是否有执行要求|warning"

    # 验证 rule_map
    assert rule_map == {"R1": "规则一", "R2": "规则二"}


def test_validation_system_prompt_length():
    """校验 system prompt 应 <= 250 chars。"""
    from validator.content_checker import _VALIDATION_SYSTEM_PROMPT

    assert len(_VALIDATION_SYSTEM_PROMPT) <= 250, (
        f"system prompt 过长：{len(_VALIDATION_SYSTEM_PROMPT)} chars"
    )


def test_build_validation_prompt_uses_compact_rules():
    """_build_validation_prompt 应正确拼接压缩格式的规则。"""
    from validator.content_checker import _build_validation_prompt

    rules = ["R1|检查开头|error", "R2|检查结尾|warning"]
    text = "测试正文"
    domain = "政务综合"

    result = _build_validation_prompt(text, domain, rules)

    assert "政务综合" in result
    assert "R1|检查开头|error" in result
    assert "R2|检查结尾|warning" in result
    assert "测试正文" in result


def test_build_validation_prompt_empty_rules():
    """空规则时应有兜底提示。"""
    from validator.content_checker import _build_validation_prompt

    result = _build_validation_prompt("正文", "未知领域", [])

    assert "通用合规性检查" in result
