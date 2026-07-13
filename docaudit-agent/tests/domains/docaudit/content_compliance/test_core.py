from content_compliance.core import Violation, ComplianceResult


def test_violation_is_frozen():
    v = Violation(
        rule_id="R001", message="missing title", severity="error", position="开头"
    )
    assert v.rule_id == "R001"
    assert v.message == "missing title"
    assert v.severity == "error"
    assert v.position == "开头"


def test_compliance_result_valid():
    result = ComplianceResult(is_valid=True)
    assert result.is_valid is True
    assert result.violations == ()


def test_compliance_result_invalid():
    v = Violation(rule_id="R001", message="missing title")
    result = ComplianceResult(is_valid=False, violations=(v,))
    assert result.is_valid is False
    assert len(result.violations) == 1
    assert result.violations[0].rule_id == "R001"
