"""Test that ProxyTool correctly reads contract fields from plugin tool spec."""
from courtier.plugin.proxies import ProxyTool


class MockClient:
    async def call(self, method, params=None, timeout=30.0):
        return {"success": True, "data": {}}

    @property
    def plugin_name(self):
        return "test"


class TestProxyToolContractPropagation:
    """Verify #2: ProxyTool reads contract fields from tool_spec."""

    def test_reads_output_artifact_type(self):
        tool_spec = {
            "name": "parse_document",
            "description": "Parse a document",
            "output_artifact_type": "docaudit.parsed_document",
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.output_artifact_type == "docaudit.parsed_document"

    def test_reads_input_fields(self):
        from courtier.agent.artifacts.models import InputField
        tool_spec = {
            "name": "audit_content",
            "description": "Audit content",
            "input_fields": [
                {"name": "paragraphs", "artifact_type": "docaudit.paragraph_list"},
            ],
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert len(proxy.input_fields) == 1
        f = proxy.input_fields[0]
        assert isinstance(f, InputField)
        assert f.name == "paragraphs"
        assert f.artifact_type == "docaudit.paragraph_list"

    def test_reads_runtime_policy(self):
        from courtier.agent.artifacts.models import RuntimePolicy
        tool_spec = {
            "name": "limited_tool",
            "description": "Has limits",
            "runtime_policy": {"max_calls": 3, "max_consecutive": 1},
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert isinstance(proxy.runtime_policy, RuntimePolicy)
        assert proxy.runtime_policy.max_calls == 3
        assert proxy.runtime_policy.max_consecutive == 1

    def test_reads_skip_persist(self):
        tool_spec = {
            "name": "ephemeral_tool",
            "description": "Don't cache me",
            "skip_persist": True,
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.skip_persist is True

    def test_defaults_when_no_contract(self):
        tool_spec = {"name": "simple_tool", "description": "No contracts"}
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.output_artifact_type is None
        assert proxy.input_fields == ()
        assert proxy.runtime_policy is None
        assert proxy.skip_persist is False
