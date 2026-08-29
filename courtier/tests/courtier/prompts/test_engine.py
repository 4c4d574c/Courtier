"""Tests for PromptBundle and PromptEngine."""

import logging
import tempfile
from pathlib import Path

from courtier.prompts.engine import (
    FALLBACK_TEMPLATES,
    RESERVED_TEMPLATE_KEYS,
    PromptBundle,
    PromptEngine,
)
from courtier.prompts.engine import (
    logger as engine_logger,
)


class TestPromptBundle:
    def test_empty_bundle(self):
        bundle = PromptBundle(locale="en-US")
        assert bundle.locale == "en-US"
        assert bundle.templates == {}

    def test_from_directory_loads_yaml_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts_dir = Path(tmp) / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "orchestrator.yaml").write_text(
                "orchestrator:\n"
                "  system_prompt: 'You are {{ agent_name }}.'\n"
                "  task_decomposition: 'Decompose: {{ task }}'\n",
                encoding="utf-8",
            )
            (prompts_dir / "behavioral.yaml").write_text(
                "behavioral:\n"
                "  thinking_directive: 'Be concise.'\n"
                "  rules: |\n    - Rule 1\n    - Rule 2\n",
                encoding="utf-8",
            )

            bundle = PromptBundle.from_directory(prompts_dir, "zh-CN")

            assert bundle.locale == "zh-CN"
            assert bundle.templates["orchestrator.system_prompt"] == "You are {{ agent_name }}."
            assert bundle.templates["orchestrator.task_decomposition"] == "Decompose: {{ task }}"
            assert bundle.templates["behavioral.thinking_directive"] == "Be concise."
            assert "- Rule 1\n- Rule 2" in bundle.templates["behavioral.rules"]

    def test_from_directory_missing_directory(self):
        bundle = PromptBundle.from_directory(Path("/nonexistent/prompts"), "en-US")
        assert bundle.locale == "en-US"
        assert bundle.templates == {}

    def test_merge_layers_templates(self):
        base = PromptBundle(
            locale="en-US",
            templates={"a": "base_a", "b": "base_b"},
        )
        overlay = PromptBundle(
            locale="en-US",
            templates={"b": "overlay_b", "c": "overlay_c"},
        )
        merged = base.merge(overlay)
        assert merged.templates["a"] == "base_a"  # preserved
        assert merged.templates["b"] == "overlay_b"  # overridden
        assert merged.templates["c"] == "overlay_c"  # added


class TestPromptEngine:
    def test_render_from_bundle(self):
        bundle = PromptBundle(
            locale="en-US",
            templates={"test.greeting": "Hello, {{ name }}!"},
        )
        engine = PromptEngine(bundle)
        result = engine.render("test.greeting", name="World")
        assert result == "Hello, World!"

    def test_render_falls_back_to_builtin(self):
        engine = PromptEngine()  # empty bundle
        result = engine.render("chat.system_prompt", agent_name="TestBot")
        assert "You are TestBot" in result

    def test_render_missing_template_returns_empty(self):
        engine = PromptEngine()
        result = engine.render("nonexistent.key")
        assert result == ""

    def test_render_with_jinja_loop(self):
        bundle = PromptBundle(
            locale="en-US",
            templates={
                "test.skills": (
                    "Skills:\n" "{% for s in skills %}- {{ s.name }}: {{ s.desc }}\n{% endfor %}"
                ),
            },
        )
        engine = PromptEngine(bundle)
        result = engine.render(
            "test.skills",
            skills=[
                {"name": "audit", "desc": "Audit docs"},
                {"name": "review", "desc": "Review contracts"},
            ],
        )
        assert "- audit: Audit docs" in result
        assert "- review: Review contracts" in result

    def test_locale_property(self):
        bundle = PromptBundle(locale="zh-CN")
        engine = PromptEngine(bundle)
        assert engine.locale == "zh-CN"

    def test_fallback_templates_are_valid_jinja(self):
        """All fallback templates should render without error with empty vars."""
        engine = PromptEngine()
        for key, _template in FALLBACK_TEMPLATES.items():
            result = engine.render(key, agent_name="Test", task="test task")
            assert isinstance(result, str)

    def test_reserved_template_keys_exist_in_fallbacks(self):
        """Every reserved key should be a valid string and present in fallbacks."""
        for key in RESERVED_TEMPLATE_KEYS:
            assert isinstance(key, str)
            assert (
                key in FALLBACK_TEMPLATES
            ), f"Reserved key '{key}' missing from FALLBACK_TEMPLATES"


class TestFromDomainDirectories:
    def test_loads_from_domain_directory(self):
        """from_domain_directories loads config/prompts/{locale}/*.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp) / "app_audit"
            prompts_dir = domain / "config" / "prompts" / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "orchestrator.yaml").write_text(
                "orchestrator:\n" "  system_prompt: 'Hello from app_audit'\n",
                encoding="utf-8",
            )

            engine = PromptEngine.from_domain_directories(
                [domain],
                locale="zh-CN",
            )

            result = engine.render("orchestrator.system_prompt")
            assert "Hello from app_audit" in result

    def test_locale_fallback_when_requested_locale_missing(self):
        """Falls back to domain's first declared locale from domain.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp) / "app_audit"
            # domain.yaml declares zh-CN as first locale
            (domain / "config").mkdir(parents=True)
            (domain / "config" / "domain.yaml").write_text(
                "locales:\n  - zh-CN\n  - en-US\n",
                encoding="utf-8",
            )
            prompts_dir = domain / "config" / "prompts" / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "chat.yaml").write_text(
                "chat:\n  system_prompt: 'Ni hao!'\n",
                encoding="utf-8",
            )

            # Request a locale that doesn't exist, should fall back to zh-CN
            engine = PromptEngine.from_domain_directories(
                [domain],
                locale="ja-JP",
            )

            result = engine.render("chat.system_prompt")
            assert "Ni hao!" in result

    def test_merge_ordering_later_overrides_earlier(self):
        """Later domain templates override earlier ones for the same key."""
        with tempfile.TemporaryDirectory() as tmp:
            # Domain 1 — base
            domain1 = Path(tmp) / "core"
            prompts1 = domain1 / "config" / "prompts" / "en-US"
            prompts1.mkdir(parents=True)
            (prompts1 / "chat.yaml").write_text(
                "chat:\n  system_prompt: 'You are CoreAI.'\n"
                "  welcome_message: 'Welcome to Core'\n",
                encoding="utf-8",
            )

            # Domain 2 — overlay, overrides chat.system_prompt
            domain2 = Path(tmp) / "app_support"
            prompts2 = domain2 / "config" / "prompts" / "en-US"
            prompts2.mkdir(parents=True)
            (prompts2 / "chat.yaml").write_text(
                "chat:\n  system_prompt: 'You are SupportBot.'\n",
                encoding="utf-8",
            )

            engine = PromptEngine.from_domain_directories(
                [domain1, domain2],
                locale="en-US",
            )

            # Overridden by domain2
            assert engine.render("chat.system_prompt") == "You are SupportBot."
            # Preserved from domain1
            assert engine.render("chat.welcome_message") == "Welcome to Core"

    def test_warns_on_unrecognized_template_key(self, caplog):
        """from_directory warns when a key is not in RESERVED_TEMPLATE_KEYS."""
        with tempfile.TemporaryDirectory() as tmp:
            prompts_dir = Path(tmp) / "en-US"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "custom.yaml").write_text(
                "custom:\n  unknown_key: 'some text'\n",
                encoding="utf-8",
            )

            with caplog.at_level(logging.WARNING, logger=engine_logger.name):
                PromptBundle.from_directory(prompts_dir, "en-US")

            assert any(
                "custom.unknown_key" in r.message and "RESERVED_TEMPLATE_KEYS" in r.message
                for r in caplog.records
            )


class TestCoreDefaults:
    """Core 默认 prompt bundle（prompts/defaults/）作为合并基底的行为。"""

    def test_generic_keys_come_from_core_defaults_without_domain(self):
        """无领域包时，领域无关 key 由 Core 默认（本地化）提供。"""
        engine = PromptEngine.from_domain_directories([], locale="zh-CN")

        assert "未注册" in engine.render("errors.tool_not_found", tool_name="t", available_tools="a")
        assert "# 行为准则" in engine.render("behavioral.rules")
        assert "上下文压缩助手" in engine.render("context.compact_prompt", history="h")
        assert "工具调用规则" in engine.render("tools.invocation_rules")
        assert "SubAgent" in engine.render("subagent.system_prompt", task="t")
        assert "有什么可以帮助你的" in engine.render("chat.welcome_message", agent_name="X")

    def test_domain_owned_keys_come_from_core_defaults(self):
        """领域专属 key（orchestrator.*）现由 Core 默认提供（本地化），
        不再落到最小英文 fallback。"""
        engine = PromptEngine.from_domain_directories([], locale="zh-CN")
        result = engine.render("orchestrator.system_prompt", agent_name="X")
        assert "你是 X" in result
        assert "疑似即激活" in result

        engine_en = PromptEngine.from_domain_directories([], locale="en-US")
        result_en = engine_en.render("orchestrator.system_prompt", agent_name="X")
        assert "You are X" in result_en
        assert "Activate on suspicion" in result_en

    def test_domain_overrides_core_default_key(self):
        """领域包可按 key 覆盖 Core 默认。"""
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp) / "app_audit"
            prompts_dir = domain / "config" / "prompts" / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "errors.yaml").write_text(
                "errors:\n  tool_not_found: '自定义：工具不见了 {{ tool_name }}'\n",
                encoding="utf-8",
            )

            engine = PromptEngine.from_domain_directories([domain], locale="zh-CN")

            assert "工具不见了" in engine.render("errors.tool_not_found", tool_name="t")
            # 未覆盖的 key 仍来自 Core 默认
            assert "上下文压缩助手" in engine.render("context.compact_prompt", history="h")

    def test_core_defaults_locale_fallback_to_en(self):
        """请求的 locale 无 Core 默认时回退 en-US。"""
        engine = PromptEngine.from_domain_directories([], locale="ja-JP")
        assert "not registered" in engine.render("errors.tool_not_found", tool_name="t", available_tools="a")
        # locale 属性仍报告请求的 locale（与历史行为一致）
        assert engine.locale == "ja-JP"

    def test_core_defaults_cover_all_generic_reserved_keys(self):
        """zh-CN Core 默认覆盖所有领域无关的 reserved key。

        领域专属（orchestrator.*, chat.system_prompt）与琐碎占位（ui.*）
        不在 Core 默认中，仍由 FALLBACK_TEMPLATES 兜底。
        """
        domain_owned = {
            "orchestrator.system_prompt",
            "orchestrator.task_decomposition",
            "orchestrator.workflow_rules",
            "chat.system_prompt",
        }
        trivial = {k for k in RESERVED_TEMPLATE_KEYS if k.startswith("ui.")}
        bundle = PromptBundle.from_directory(
            Path(__file__).resolve().parents[3] / "courtier" / "prompts" / "defaults" / "zh-CN",
            "zh-CN",
        )
        missing = RESERVED_TEMPLATE_KEYS - domain_owned - trivial - bundle.templates.keys()
        assert not missing, f"Core 默认缺少领域无关 key: {sorted(missing)}"
