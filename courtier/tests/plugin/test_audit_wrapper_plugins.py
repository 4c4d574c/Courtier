"""Tests for JSON-RPC wrapper plugins in plugins/docaudit/audit/.

These plugins wrap existing domain tools (validator, content_compliance,
doccorrector) behind JSON-RPC tool interfaces. Tests verify plugin
registration contracts and error handling without requiring actual
LLM/OCR backends.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import _ensure_plugin_path

# ---------------------------------------------------------------------------
# format_audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_format_audit_plugin_registers_check_format():
    """Format audit plugin registers check_format tool."""
    _ensure_plugin_path("format_audit")

    from plugins.docaudit.audit.format_audit.entry import FormatAuditPlugin

    plugin = FormatAuditPlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "check_format" in tool_names


@pytest.mark.asyncio
async def test_format_audit_tool_validates_doc():
    """check_format calls validator and returns structured result."""
    _ensure_plugin_path("format_audit")

    from plugins.docaudit.audit.format_audit.tools import FormatAuditTool

    tool = FormatAuditTool()
    sample_result = [{"block": "title", "error": "font_mismatch", "detail": "expected 22pt"}]

    with patch("plugins.docaudit.audit.format_audit.tools.validator", return_value=sample_result):
        result = await tool.execute(doc={"mock": "doc"}, doc_type="通知")

    assert result.success is True
    assert result.data == sample_result


@pytest.mark.asyncio
async def test_format_audit_tool_handles_error():
    """check_format returns ToolResult with success=False on exception."""
    _ensure_plugin_path("format_audit")

    from plugins.docaudit.audit.format_audit.tools import FormatAuditTool

    tool = FormatAuditTool()

    with patch(
        "plugins.docaudit.audit.format_audit.tools.validator",
        side_effect=ValueError("invalid doc structure"),
    ):
        result = await tool.execute(doc={}, doc_type="通知")

    assert result.success is False
    assert "invalid doc structure" in result.error


# ---------------------------------------------------------------------------
# content_audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_content_audit_plugin_registers_check_content():
    """Content audit plugin registers check_content tool."""
    _ensure_plugin_path("content_audit")

    from plugins.docaudit.audit.content_audit.entry import ContentAuditPlugin

    plugin = ContentAuditPlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "check_content" in tool_names


@pytest.mark.asyncio
async def test_content_audit_tool_returns_violations():
    """check_content returns violations from registered checker."""
    _ensure_plugin_path("content_audit")

    from plugins.docaudit.audit.content_audit.tools import ContentAuditTool

    tool = ContentAuditTool()
    # Override _ensure_initialized to skip real init_checkers()
    tool._initialized = True

    mock_checker = MagicMock()
    mock_checker.doc_type = "通知"

    from courtier.plugin.types import ComplianceResult, Violation

    mock_checker.check = AsyncMock(
        return_value=ComplianceResult(
            is_valid=False,
            violations=(
                Violation(rule_id="R001", message="missing keyword", severity="error"),
                Violation(rule_id="R002", message="bad phrasing", severity="warning"),
            ),
        )
    )

    with patch(
        "plugins.docaudit.audit.content_audit.tools.get_checker", return_value=mock_checker
    ):
        result = await tool.execute(text="test text", doc_type="通知")

    assert result.success is True
    assert result.data["is_valid"] is False
    assert len(result.data["violations"]) == 2
    assert result.data["violations"][0]["rule_id"] == "R001"


@pytest.mark.asyncio
async def test_content_audit_tool_unknown_doc_type():
    """check_content returns error when no checker exists for doc_type."""
    _ensure_plugin_path("content_audit")

    from plugins.docaudit.audit.content_audit.tools import ContentAuditTool

    tool = ContentAuditTool()
    tool._initialized = True

    with patch("plugins.docaudit.audit.content_audit.tools.get_checker", return_value=None):
        result = await tool.execute(text="test", doc_type="unknown_type")

    assert result.success is False
    assert "No checker registered" in result.error


@pytest.mark.asyncio
async def test_content_audit_tool_handles_checker_exception():
    """check_content handles checker errors gracefully."""
    _ensure_plugin_path("content_audit")

    from plugins.docaudit.audit.content_audit.tools import ContentAuditTool

    tool = ContentAuditTool()
    tool._initialized = True

    mock_checker = MagicMock()
    mock_checker.check = AsyncMock(side_effect=RuntimeError("checker crash"))

    with patch(
        "plugins.docaudit.audit.content_audit.tools.get_checker", return_value=mock_checker
    ):
        result = await tool.execute(text="test", doc_type="通知")

    assert result.success is False
    assert "checker crash" in result.error


# ---------------------------------------------------------------------------
# text_correction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_correction_plugin_registers_correct_text():
    """Text correction plugin registers correct_text tool."""
    _ensure_plugin_path("text_correction")

    from plugins.docaudit.audit.text_correction.entry import TextCorrectionPlugin

    plugin = TextCorrectionPlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "correct_text" in tool_names


@pytest.mark.asyncio
async def test_text_correction_tool_returns_results():
    """correct_text returns correction results from ErrorCorrect."""
    _ensure_plugin_path("text_correction")

    from plugins.docaudit.audit.text_correction.tools import TextCorrectionTool

    tool = TextCorrectionTool()
    mock_corrections = [{"source": "测试文本", "target": "测试文本", "errors": []}]

    with patch(
        "plugins.docaudit.audit.text_correction.tools.ErrorCorrect",
        return_value=MagicMock(infer=MagicMock(return_value=mock_corrections)),
    ):
        result = await tool.execute(text="测试文本")

    assert result.success is True
    assert result.data["results"] == mock_corrections


@pytest.mark.asyncio
async def test_text_correction_tool_handles_error():
    """correct_text handles ErrorCorrect failures gracefully."""
    _ensure_plugin_path("text_correction")

    from plugins.docaudit.audit.text_correction.tools import TextCorrectionTool

    tool = TextCorrectionTool()

    with patch(
        "plugins.docaudit.audit.text_correction.tools.ErrorCorrect",
        side_effect=ConnectionError("API unavailable"),
    ):
        result = await tool.execute(text="测试")

    assert result.success is False
    assert "API unavailable" in result.error


# ---------------------------------------------------------------------------
# plagiarism (existing — verify no regressions)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plagiarism_plugin_registers_detect_plagiarism():
    """Plagiarism plugin exposes only detect_plagiarism as a public tool."""
    _ensure_plugin_path("plagiarism")

    from plugins.docaudit.audit.plagiarism.entry import PlagiarismPlugin

    plugin = PlagiarismPlugin()
    plugin._setup_handlers()
    caps, _ = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "detect_plagiarism" in tool_names
    # Internal helper functions MUST NOT be exposed as tools.
    assert "compute_similarity" not in tool_names
    assert "compute_dynamic_threshold" not in tool_names
