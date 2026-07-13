import inspect

import pytest

from content_compliance import (
    ComplianceResult,
    ContentChecker,
    Violation,
    get_checker,
    init_checkers,
    list_supported_types,
    register,
    SUBTYPE_TO_DOCTYPE,
)


def test_public_api_exports():
    assert inspect.isclass(ComplianceResult)
    assert inspect.isclass(ContentChecker)
    assert inspect.isclass(Violation)
    assert callable(get_checker)
    assert callable(list_supported_types)
    assert callable(register)
    assert isinstance(SUBTYPE_TO_DOCTYPE, dict)


@pytest.fixture
def registered_checkers():
    """Register checkers for tests that need them, and clean up after."""
    init_checkers()
    yield
    # Clean up: none of the explicitly-registered checkers affect
    # module state, but we import fresh for isolation in other tests.


def test_auto_registered_checkers(registered_checkers):
    types = list_supported_types()
    assert "通知" in types
    assert "请示" in types
    assert "报告" in types
    assert "批复" in types
    assert "函" in types


def test_get_notification_checker(registered_checkers):
    checker = get_checker("通知")
    assert checker is not None
    assert checker.doc_type == "通知"
    subtypes = checker.list_subtypes()
    assert "发布性通知" in subtypes
    assert "批转性通知" in subtypes


def test_subtype_mapping():
    assert SUBTYPE_TO_DOCTYPE["发布性通知"] == "通知"
    assert SUBTYPE_TO_DOCTYPE["请示"] == "请示"
    assert SUBTYPE_TO_DOCTYPE["报告"] == "报告"
