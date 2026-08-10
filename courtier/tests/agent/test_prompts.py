"""Tests for PromptPipeline."""

from courtier.agent.prompts.pipeline import PromptPipeline


class TestPromptPipeline:
    def test_build_empty(self):
        pp = PromptPipeline()
        assert pp.build() == ""

    def test_build_single_section(self):
        pp = PromptPipeline()
        pp.set_rules("You are a helpful assistant.")
        result = pp.build()
        assert "# 规则与策略" in result
        assert "You are a helpful assistant." in result

    def test_build_with_context_substitution(self):
        pp = PromptPipeline()
        pp.set_rules("Your name is {{name}}.")
        result = pp.build(context={"name": "Courtier"})
        assert "Your name is Courtier." in result
        assert "{{name}}" not in result

    def test_build_without_context_leaves_placeholders(self):
        pp = PromptPipeline()
        pp.set_rules("Your name is {{name}}.")
        result = pp.build()
        assert "{{name}}" in result

    def test_build_multiple_context_keys(self):
        pp = PromptPipeline()
        pp.set_rules("{{greeting}}, {{name}}.")
        result = pp.build(context={"greeting": "Hello", "name": "World"})
        assert "Hello, World." in result

    def test_build_no_double_substitution(self):
        pp = PromptPipeline()
        pp.set_rules("{{a}}")
        result = pp.build(context={"a": "{{b}}", "b": "injected"})
        # The value of {{a}} is "{{b}}" — it should NOT be re-scanned.
        # Key "b" was not consumed by any placeholder, so it appears in task context.
        assert "# 规则与策略\n{{b}}" in result
        assert "{{b}}" in result  # not re-substituted
        assert "# 任务上下文" in result
        assert "b: injected" in result

    def test_build_unmatched_placeholder_preserved(self):
        pp = PromptPipeline()
        pp.set_rules("{{name}} is here, {{missing}} is not.")
        result = pp.build(context={"name": "Courtier"})
        assert "{{missing}}" in result
        assert "{{name}}" not in result

    def test_build_surfaces_unconsumed_context(self):
        """Context keys not used by any placeholder appear as task context."""
        pp = PromptPipeline()
        pp.set_rules("Hello {{name}}.")
        result = pp.build(context={"name": "Courtier", "file_path": "/tmp/test.docx"})
        assert "file_path: /tmp/test.docx" in result
        assert "# 任务上下文" in result
        # name was consumed by placeholder, so it should NOT be in task context
        assert "name:" not in result.split("# 任务上下文")[1] if "# 任务上下文" in result else True

    def test_build_surfaces_unconsumed_context_truncates_large_values(self):
        """Large context values are truncated to 500 chars."""
        pp = PromptPipeline()
        large_value = "x" * 1000
        result = pp.build(context={"data": large_value})
        assert "x" * 497 + "..." in result
        assert "x" * 500 not in result  # full value not present

    def test_build_no_context_section_when_all_consumed(self):
        """When all context keys are consumed by placeholders, no task context section."""
        pp = PromptPipeline()
        pp.set_rules("{{a}} {{b}}")
        result = pp.build(context={"a": "1", "b": "2"})
        assert "# 任务上下文" not in result

    def test_build_no_context_section_when_empty_context(self):
        """Empty context produces no task context section."""
        pp = PromptPipeline()
        pp.set_rules("Hello.")
        result = pp.build(context={})
        assert "# 任务上下文" not in result

    def test_identity_injects_thinking_directive(self):
        """set_identity accepts an optional thinking_directive parameter.

        When not provided, a minimal English fallback is used.
        When provided (e.g. from PromptBundle), it is injected into the identity.
        """
        pp = PromptPipeline()
        pp.set_identity("TestAgent", "你是测试代理。")
        result = pp.build()
        # Agent-specific role is preserved.
        assert "你是测试代理。" in result
        # Minimal fallback is present when no directive is passed
        assert "Thinking Directive" in result

        # With an explicit directive, the custom text is used
        pp2 = PromptPipeline()
        pp2.set_identity(
            "TestAgent",
            "你是测试代理。",
            thinking_directive="# 思考规范\n- 直接给出结论\n- 默认值时直接用默认值",
        )
        result2 = pp2.build()
        assert "# 思考规范" in result2
        assert "直接给出结论" in result2
        assert "默认值时直接用默认值" in result2

    def test_tool_usage_notes_rendered_after_tools(self):
        pp = PromptPipeline()
        pp.set_tools("可用工具: parse_document")
        pp.set_tool_usage_notes("## parse\n解析插件用法")
        pp.set_context_instructions("ref 说明")
        result = pp.build()
        assert "# 工具使用说明" in result
        assert "解析插件用法" in result
        assert (
            result.index("# 可用工具")
            < result.index("# 工具使用说明")
            < result.index("# 上下文规则")
        )

    def test_tool_usage_notes_empty_clears_section(self):
        pp = PromptPipeline()
        pp.set_tool_usage_notes("## parse\n用法")
        assert "# 工具使用说明" in pp.build()
        pp.set_tool_usage_notes("")
        assert "# 工具使用说明" not in pp.build()
