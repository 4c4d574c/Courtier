"""Tests for CapabilityRegistry."""


from courtier.agent.core.capability import Capability, CapabilityRegistry


class _FakeTool:
    name = "fake_tool"
    description = "A fake tool."


class TestCapabilityRegistry:
    def test_register_and_get(self):
        reg = CapabilityRegistry()
        cap = Capability(type="tool", name="echo", provider="test")
        reg.register(cap)
        found = reg.get("tool", "echo")
        assert found is not None
        assert found.name == "echo"
        assert found.provider == "test"

    def test_unregister_removes_entry(self):
        reg = CapabilityRegistry()
        reg.register(Capability(type="skill", name="audit"))
        removed = reg.unregister("skill", "audit")
        assert removed is not None
        assert reg.get("skill", "audit") is None

    def test_unregister_by_provider(self):
        reg = CapabilityRegistry()
        reg.register(Capability(type="tool", name="t1", provider="plugin_a"))
        reg.register(Capability(type="tool", name="t2", provider="plugin_b"))
        reg.register(Capability(type="route", name="/a", provider="plugin_a"))
        removed = reg.unregister_by_provider("plugin_a")
        assert {c.name for c in removed} == {"t1", "/a"}
        assert reg.get("tool", "t1") is None
        assert reg.get("route", "/a") is None
        assert reg.get("tool", "t2") is not None

    def test_list_capabilities_filter_by_type(self):
        reg = CapabilityRegistry()
        reg.register(Capability(type="tool", name="t1"))
        reg.register(Capability(type="skill", name="s1"))
        tools = reg.list_capabilities(type_="tool")
        assert [c.name for c in tools] == ["t1"]

    def test_list_capabilities_filter_by_provider(self):
        reg = CapabilityRegistry()
        reg.register(Capability(type="tool", name="t1", provider="p1"))
        reg.register(Capability(type="skill", name="s1", provider="p1"))
        reg.register(Capability(type="tool", name="t2", provider="p2"))
        caps = reg.list_capabilities(provider="p1")
        assert {c.name for c in caps} == {"t1", "s1"}

    def test_fallback_to_tool_registry(self):
        from courtier.agent.tools.registry import ToolRegistry

        tool_reg = ToolRegistry()
        tool_reg.register(_FakeTool())
        cap_reg = CapabilityRegistry(tool_registry=tool_reg)
        cap = cap_reg.get("tool", "fake_tool")
        assert cap is not None
        assert cap.instance is tool_reg.get("fake_tool")

    def test_build_catalog(self):
        reg = CapabilityRegistry()
        reg.register(Capability(type="tool", name="echo", description="Echo input"))
        catalog = reg.build_catalog("tool")
        assert "echo" in catalog
        assert "Echo input" in catalog

    def test_listener_invoked_on_register_and_unregister(self):
        events = []

        def listener(cap, action):
            events.append((cap.name, action))

        reg = CapabilityRegistry()
        reg.add_listener(listener)
        reg.register(Capability(type="tool", name="x"))
        reg.unregister("tool", "x")
        assert events == [("x", "register"), ("x", "unregister")]

    def test_title_defaults_to_name(self):
        cap = Capability(type="tool", name="my_tool")
        assert cap.title == "my_tool"
