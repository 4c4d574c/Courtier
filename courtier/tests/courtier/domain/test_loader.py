"""Tests for DomainLoader."""
import tempfile
from pathlib import Path
from courtier.domain.loader import DomainConfig, DomainLoader

class TestDomainConfig:
    def test_defaults(self):
        config = DomainConfig(name="test")
        assert config.name == "test"
        assert config.locales == ["en-US"]
        assert config.requires_plugins == []

class TestDomainLoader:
    def test_load_valid_domain_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp)
            cfg = domain / "config"; cfg.mkdir(parents=True)
            (cfg / "domain.yaml").write_text("name: test-domain\ntitle: Test\nlocales: [zh-CN]\n")
            config = DomainLoader.load(domain)
            assert config is not None
            assert config.name == "test-domain"
            assert config.locales == ["zh-CN"]

    def test_load_missing_file_returns_none(self):
        config = DomainLoader.load(Path("/nonexistent"))
        assert config is None

    def test_validate_complete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp)
            (domain / "config" / "prompts" / "zh-CN").mkdir(parents=True)
            (domain / "config" / "prompts" / "zh-CN" / "test.yaml").write_text("key: value\n")
            (domain / "config" / "domain.yaml").write_text("name: test\nlocales: [zh-CN]\n")
            (domain / "plugins" / "my_plugin").mkdir(parents=True)
            (domain / "plugins" / "my_plugin" / "plugin.yaml").write_text("name: my_plugin\n")
            (domain / "skills").mkdir()
            (domain / "skills" / "test.md").write_text("---\nname: test\n---\n# Test\n")

            issues = DomainLoader.validate_domain(domain)
            assert issues == []

    def test_validate_returns_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            domain = Path(tmp)
            (domain / "config").mkdir(parents=True)
            (domain / "config" / "domain.yaml").write_text("name: test\nrequires_plugins: [missing_plugin]\nlocales: [fr-FR]\n")
            (domain / "config" / "prompts").mkdir()  # no fr-FR subdir

            issues = DomainLoader.validate_domain(domain)
            assert len(issues) > 0
