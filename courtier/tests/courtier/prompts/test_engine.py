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
                    "Skills:\n"
                    "{% for s in skills %}- {{ s.name }}: {{ s.desc }}\n{% endfor %}"
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
            assert key in FALLBACK_TEMPLATES, (
                f"Reserved key '{key}' missing from FALLBACK_TEMPLATES"
            )


class TestFromDomainDirectories:
    def test_loads_from_domain_directory(self):
        """from_domain_directories loads config/prompts/{locale}/*.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp) / "app_audit"
            prompts_dir = domain / "config" / "prompts" / "zh-CN"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "orchestrator.yaml").write_text(
                "orchestrator:\n"
                "  system_prompt: 'Hello from app_audit'\n",
                encoding="utf-8",
            )

            engine = PromptEngine.from_domain_directories(
                [domain], locale="zh-CN",
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
                [domain], locale="ja-JP",
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
                [domain1, domain2], locale="en-US",
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
                "custom.unknown_key" in r.message
                and "RESERVED_TEMPLATE_KEYS" in r.message
                for r in caplog.records
            )
