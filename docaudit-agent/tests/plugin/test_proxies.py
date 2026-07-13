"""Tests for proxy objects (ProxyTool, ProxyChecker)."""

import pytest

from courtier.plugin.proxies import ProxyTool, ProxyChecker


class MockClient:
    """Mock JSONRPCClient for testing proxies without real subprocess."""

    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self._responses = responses or []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append({"method": method, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return {"success": True, "data": {}}

    @property
    def plugin_name(self):
        return "test_plugin"


class TestProxyTool:
    def test_proxy_tool_has_required_attributes(self):
        tool_spec = {
            "name": "my_tool",
            "display_name": "My Tool",
            "description": "A test tool",
        }
        mock_client = MockClient()
        proxy = ProxyTool(mock_client, tool_spec)

        assert proxy.name == "my_tool"
        assert proxy.display_name == "My Tool"
        assert proxy.description == "A test tool"
        assert proxy.parameters == {}
        assert proxy.skip_persist is False
        assert proxy.output_schema is None

    @pytest.mark.asyncio
    async def test_execute_forwards_to_client(self):
        tool_spec = {"name": "my_tool", "description": "desc"}
        mock_client = MockClient(
            [
                {"success": True, "data": {"result": 42}},
            ]
        )
        proxy = ProxyTool(mock_client, tool_spec)

        progress_calls = []

        def on_progress(p):
            progress_calls.append(p)

        result = await proxy.execute(on_progress=on_progress, arg1="hello", arg2=123)
        assert result.success is True
        assert result.data == {"result": 42}

        assert len(mock_client.calls) == 1
        assert mock_client.calls[0]["method"] == "tool.execute"
        assert mock_client.calls[0]["params"]["tool"] == "my_tool"
        assert mock_client.calls[0]["params"]["args"] == {"arg1": "hello", "arg2": 123}

        assert len(progress_calls) == 2
        assert progress_calls[0]["status"] == "running"
        assert progress_calls[1]["status"] == "done"

    @pytest.mark.asyncio
    async def test_execute_error_response(self):
        tool_spec = {"name": "bad_tool", "description": "desc"}
        mock_client = MockClient(
            [
                {"success": False, "error": "something went wrong"},
            ]
        )
        proxy = ProxyTool(mock_client, tool_spec)

        progress_calls = []

        def on_progress(p):
            progress_calls.append(p)

        result = await proxy.execute(on_progress=on_progress)
        assert result.success is False
        assert result.error == "something went wrong"

        assert len(progress_calls) == 2
        assert progress_calls[0]["status"] == "running"
        assert progress_calls[1]["status"] == "done"

    @pytest.mark.asyncio
    async def test_execute_with_parameters_from_spec(self):
        tool_spec = {
            "name": "param_tool",
            "description": "Has params",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.parameters["type"] == "object"
        assert "text" in proxy.parameters["properties"]


class TestProxyChecker:
    def test_proxy_checker_has_doc_type(self):
        checker_spec = {"name": "my_checker", "doc_type": "通知"}
        proxy = ProxyChecker(MockClient(), checker_spec)
        assert proxy.doc_type == "通知"

    @pytest.mark.asyncio
    async def test_check_forwards_to_client(self):
        checker_spec = {"name": "my_checker", "doc_type": "通知"}
        mock_client = MockClient(
            [
                {"is_valid": True, "violations": []},
            ]
        )
        proxy = ProxyChecker(mock_client, checker_spec)

        result = await proxy.check("some text", subtype=None)
        assert result.is_valid is True
        assert result.violations == ()

        assert mock_client.calls[0]["method"] == "checker.check"
        assert mock_client.calls[0]["params"]["text"] == "some text"
        assert mock_client.calls[0]["params"]["subtype"] is None

    @pytest.mark.asyncio
    async def test_check_with_violations(self):
        checker_spec = {"name": "my_checker", "doc_type": "请示"}
        mock_client = MockClient(
            [
                {
                    "is_valid": False,
                    "violations": [
                        {"rule_id": "R001", "message": "缺少标题", "severity": "error"},
                    ],
                },
            ]
        )
        proxy = ProxyChecker(mock_client, checker_spec)

        result = await proxy.check("bad text", subtype="请示")
        assert result.is_valid is False
        assert len(result.violations) == 1
        assert result.violations[0].rule_id == "R001"
        assert result.violations[0].message == "缺少标题"

    @pytest.mark.asyncio
    async def test_check_handles_non_dict_response(self):
        checker_spec = {"name": "bad_checker", "doc_type": "通知"}
        mock_client = MockClient(["not a dict"])
        proxy = ProxyChecker(mock_client, checker_spec)

        result = await proxy.check("some text")
        assert result.is_valid is False
        assert any("instead of dict" in v.message for v in result.violations)
