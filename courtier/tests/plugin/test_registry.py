"""Tests for ExtensionRegistry."""

import logging

import pytest

from courtier.agent.tools.registry import ToolRegistry
from content_compliance.registry import CheckerRegistry
from courtier.plugin.registry import ExtensionRegistry


class MockClient:
    """Minimal mock for JSONRPCClient."""

    def __init__(self, name="test_plugin"):
        self._name = name

    async def call(self, method, params=None, timeout=30.0):
        return {}

    @property
    def plugin_name(self):
        return self._name


class TestExtensionRegistry:
    @pytest.fixture
    def tool_registry(self):
        return ToolRegistry()

    @pytest.fixture
    def checker_registry(self):
        return CheckerRegistry()

    @pytest.fixture
    def ext_registry(self, tool_registry, checker_registry):
        return ExtensionRegistry(
            tool_registry=tool_registry,
            checker_registry=checker_registry,
        )

    def test_register_tool_injects_proxy_into_tool_registry(
        self, ext_registry, tool_registry
    ):
        client = MockClient()
        caps = [
            {
                "type": "tool",
                "name": "my_tool",
                "display_name": "My Tool",
                "description": "A test tool",
            },
        ]

        ext_registry.on_register("plugin_a", client, caps, system_prompt="")

        tool = tool_registry.get("my_tool")
        assert tool is not None
        assert tool.name == "my_tool"
        assert tool.display_name == "My Tool"

    def test_register_checker_injects_proxy_into_checker_registry(
        self, ext_registry, checker_registry
    ):
        client = MockClient()
        caps = [
            {
                "type": "checker",
                "name": "my_checker",
                "doc_type": "通知",
                "display_name": "My Checker",
            },
        ]

        ext_registry.on_register("plugin_b", client, caps, system_prompt="")

        checker = checker_registry.get("通知")
        assert checker is not None
        assert checker.doc_type == "通知"

    def test_register_ignores_unknown_capability_type(
        self, ext_registry, tool_registry, caplog
    ):
        """Unknown capability types should be ignored with a warning."""
        client = MockClient()
        caps = [{"type": "unknown_type", "name": "whatever"}]

        with caplog.at_level(logging.WARNING):
            ext_registry.on_register("plugin_c", client, caps, system_prompt="")

        assert "unknown_type" in caplog.text.lower()

    def test_register_multiple_capabilities(
        self, ext_registry, tool_registry, checker_registry
    ):
        client = MockClient()
        caps = [
            {"type": "tool", "name": "tool_a", "description": "Tool A"},
            {"type": "tool", "name": "tool_b", "description": "Tool B"},
            {"type": "checker", "name": "check_a", "doc_type": "报告"},
        ]

        ext_registry.on_register("multi_plugin", client, caps, system_prompt="")

        assert tool_registry.get("tool_a") is not None
        assert tool_registry.get("tool_b") is not None
        assert checker_registry.get("报告") is not None

    def test_register_duplicate_is_rejected_with_error(
        self, ext_registry, tool_registry, caplog
    ):
        """Duplicate tool names from different plugins are rejected — first wins."""
        client1 = MockClient("plugin_1")
        client2 = MockClient("plugin_2")

        ext_registry.on_register(
            "plugin_1",
            client1,
            [
                {"type": "tool", "name": "same_tool", "description": "Original"},
            ],
            system_prompt="",
        )

        with caplog.at_level(logging.ERROR):
            ext_registry.on_register(
                "plugin_2",
                client2,
                [
                    {"type": "tool", "name": "same_tool", "description": "Override"},
                ],
                system_prompt="",
            )

        # The original tool should still exist (duplicate was rejected)
        tool = tool_registry.get("same_tool")
        assert tool is not None
        assert "conflicts with an existing tool" in caplog.text

    def test_unregister_removes_all_proxies(
        self, ext_registry, tool_registry, checker_registry
    ):
        client = MockClient()
        caps = [
            {"type": "tool", "name": "tool_x", "description": "X"},
            {"type": "checker", "name": "check_x", "doc_type": "请示"},
        ]

        ext_registry.on_register("plugin_x", client, caps, system_prompt="")
        assert tool_registry.get("tool_x") is not None
        assert checker_registry.get("请示") is not None

        ext_registry.on_unregister("plugin_x")

        # Proxies should be removed
        with pytest.raises(KeyError):
            tool_registry.get("tool_x")
        assert checker_registry.get("请示") is None

    def test_register_agent_capability_is_treated_as_unknown(
        self, ext_registry, caplog
    ):
        """Plugin sub-agent capabilities are no longer supported."""
        client = MockClient()
        caps = [
            {
                "type": "agent",
                "name": "my_auditor",
                "display_name": "My Auditor",
                "role": "audit",
            },
        ]

        with caplog.at_level(logging.WARNING):
            ext_registry.on_register("agent_plugin", client, caps, system_prompt="")

        assert "unknown capability type" in caplog.text.lower()

    def test_register_route(self, ext_registry):
        caps = [
            {"type": "route", "prefix": "/api/v1/custom", "description": "Custom API"},
        ]
        ext_registry.on_register("route_plugin", MockClient(), caps, system_prompt="")

        routes = ext_registry.get_routes()
        assert "/api/v1/custom" in routes
        assert routes["/api/v1/custom"].prefix == "/api/v1/custom"

        # Unregister should remove route
        ext_registry.on_unregister("route_plugin")
        assert ext_registry.get_routes() == {}
