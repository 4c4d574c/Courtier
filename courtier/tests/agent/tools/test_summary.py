"""summarize_result 对 metadata.issue_counts 的消费机制。

约定：插件在 ToolResult.metadata["issue_counts"] 放
``{"err": n, "warn": m, "ok": k, ...}``——err/warn 驱动 ToolSummary.status
（卡片左框颜色），整个 dict 原样透传给展示层；扩展键（如 format_audit
的 unchecked）不影响状态推导。
"""

from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.summary import summarize_result


def _result_with_counts(issue_counts: dict) -> ToolResult:
    return ToolResult(
        success=True,
        data={"total_pages": 1, "errors": [], "unchecked": []},
        metadata={"issue_counts": issue_counts},
    )


class TestIssueCountsStatus:
    def test_errors_drive_err_status(self):
        counts = {"err": 20, "warn": 0, "ok": 0, "unchecked": 3}
        summary = summarize_result(_result_with_counts(counts))
        assert summary.status == "err"
        assert summary.issue_counts == counts

    def test_warnings_only_drive_warn_status(self):
        summary = summarize_result(_result_with_counts({"err": 0, "warn": 2, "ok": 8}))
        assert summary.status == "warn"

    def test_clean_result_stays_ok(self):
        summary = summarize_result(_result_with_counts({"err": 0, "warn": 0, "ok": 1}))
        assert summary.status == "ok"

    def test_unchecked_alone_does_not_change_status(self):
        """unchecked 为扩展键：随 dict 透传，但不参与 err/warn 状态推导。"""
        summary = summarize_result(
            _result_with_counts({"err": 0, "warn": 0, "ok": 1, "unchecked": 5})
        )
        assert summary.status == "ok"
        assert summary.issue_counts is not None
        assert summary.issue_counts["unchecked"] == 5

    def test_no_issue_counts_defaults_ok_and_none(self):
        summary = summarize_result(ToolResult(success=True, data={"a": 1}))
        assert summary.status == "ok"
        assert summary.issue_counts is None

    def test_failed_result_ignores_issue_counts(self):
        result = ToolResult(
            success=False,
            error="boom",
            metadata={"issue_counts": {"err": 3, "warn": 0}},
        )
        summary = summarize_result(result)
        assert summary.status == "err"
        assert summary.issue_counts is None
