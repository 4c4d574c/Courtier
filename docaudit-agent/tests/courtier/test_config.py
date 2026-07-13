"""Tests for CourtierConfig and DomainPackage protocol."""

import importlib
from pathlib import Path

from courtier.config import CourtierConfig


class TestCourtierConfig:
    """Tests for CourtierConfig discovery and env-based initialization."""

    def test_from_env_defaults(self):
        """Defaults to docaudit domain and zh-CN locale."""
        config = CourtierConfig.from_env(repo_root=Path("/tmp"))
        assert config.domain_names == ["docaudit"]
        assert config.locale == "zh-CN"

    def test_from_env_custom_domains(self, monkeypatch):
        """Parses COURTIER_DOMAIN_PACKAGES env var correctly."""
        monkeypatch.setenv("COURTIER_DOMAIN_PACKAGES", "docaudit,contract-review")
        config = CourtierConfig.from_env(repo_root=Path("/tmp"))
        assert config.domain_names == ["docaudit", "contract-review"]

    def test_from_env_custom_locale(self, monkeypatch):
        """Parses COURTIER_LOCALE env var correctly."""
        monkeypatch.setenv("COURTIER_LOCALE", "en-US")
        config = CourtierConfig.from_env(repo_root=Path("/tmp"))
        assert config.locale == "en-US"

    def test_from_env_reads_repo_root_env(self, monkeypatch):
        """COURTIER_REPO_ROOT env var is used when repo_root arg is omitted."""
        monkeypatch.setenv("COURTIER_REPO_ROOT", "/tmp/explicit-root")
        config = CourtierConfig.from_env()
        assert config.repo_root == Path("/tmp/explicit-root")

    def test_default_project_root_uses_env_var(self, monkeypatch):
        """_default_project_root() honors COURTIER_REPO_ROOT."""
        import courtier.config as config_module

        monkeypatch.setenv("COURTIER_REPO_ROOT", "/tmp/env-root")
        # Reload the module so the helper sees the new env var.
        importlib.reload(config_module)
        assert config_module._default_project_root() == Path("/tmp/env-root")

    def test_default_project_root_falls_back_to_detected(self, monkeypatch):
        """_default_project_root() falls back to file-based detection."""
        import courtier.config as config_module

        monkeypatch.delenv("COURTIER_REPO_ROOT", raising=False)
        importlib.reload(config_module)
        root = config_module._default_project_root()
        assert (root / "pyproject.toml").exists()

    def test_discover_loads_docaudit_domain(self):
        """Integration test: discover the real docaudit domain package."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        config = CourtierConfig.from_env(repo_root=repo_root)
        domains = config.discover()
        assert len(domains) >= 1
        docaudit = domains[0]
        assert docaudit.name == "docaudit"
        assert docaudit.config.name == "docaudit"
        # Verify prompts were loaded
        assert len(docaudit.prompt_bundle.templates) > 0

    def test_discover_raises_on_missing_domain(self):
        """Raises FileNotFoundError when a configured domain doesn't exist."""
        config = CourtierConfig(
            repo_root=Path("/nonexistent/repo"),
            domain_names=["nonexistent-domain"],
            locale="en-US",
        )
        try:
            config.discover()
            assert False, "Expected FileNotFoundError"
        except FileNotFoundError:
            pass  # expected

    def test_domains_property_calls_discover(self):
        """The .domains property triggers discover() on first access."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        config = CourtierConfig.from_env(repo_root=repo_root)
        # Access .domains property (not .discover()) — should load lazily
        domains = config.domains
        assert len(domains) >= 1
        assert domains[0].name == "docaudit"

    def test_build_prompt_engine(self):
        """build_prompt_engine() returns a functioning PromptEngine."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        config = CourtierConfig.from_env(repo_root=repo_root)
        engine = config.build_prompt_engine()
        # Verify it can render a template
        result = engine.render("orchestrator.system_prompt", agent_name="test")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_discover_no_domains_dir(self):
        """Returns empty list when no domains directory exists."""
        config = CourtierConfig(
            repo_root=Path("/tmp"),
            domain_names=["docaudit"],
            locale="en-US",
        )
        # /tmp/packages/domains doesn't exist, so discover logs a warning
        # and raises FileNotFoundError because the domain wasn't found
        try:
            config.discover()
            assert False, "Expected FileNotFoundError"
        except FileNotFoundError:
            pass

    def test_discover_empty_domain_names(self):
        """Returns empty list when no domain names are configured."""
        config = CourtierConfig(
            repo_root=Path("/tmp"),
            domain_names=[],
            locale="en-US",
        )
        domains = config.discover()
        assert domains == []
