"""Domain package loader — discovers and validates domain packages."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class DomainConfig(BaseModel):
    """Validated domain.yaml metadata."""
    name: str
    title: str = ""
    description: str = ""
    locales: list[str] = Field(default_factory=lambda: ["en-US"])
    requires_plugins: list[str] = Field(default_factory=list)
    requires_services: list[str] = Field(default_factory=list)


class DomainLoader:
    """Discover and validate domain packages."""

    @staticmethod
    def load(domain_path: Path) -> DomainConfig | None:
        """Load and validate domain.yaml from a domain package directory."""
        config_file = domain_path / "config" / "domain.yaml"
        if not config_file.is_file():
            logger.warning("No domain.yaml found in %s", domain_path)
            return None
        try:
            raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            return DomainConfig.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            logger.error("Invalid domain.yaml in %s: %s", domain_path, exc)
            return None

    @staticmethod
    def validate_domain(domain_path: Path, plugins_root: Path | None = None) -> list[str]:
        """Validate a complete domain package. Returns list of issues.

        Args:
            domain_path: Path to the domain package directory (e.g. domains/docaudit/).
            plugins_root: Path to the plugins directory. If None, inferred from
                domain_path (plugins are expected at {repo_root}/plugins/).
        """
        issues: list[str] = []

        # Infer plugins root if not provided
        if plugins_root is None:
            # If domain_path is under domains/, plugins are at repo root level
            if domain_path.parent.name == "domains":
                plugins_root = domain_path.parent.parent / "plugins"
            else:
                # Fallback: look for plugins/ inside domain (legacy layout)
                plugins_root = domain_path / "plugins"

        # 1. Validate domain.yaml
        config = DomainLoader.load(domain_path)
        if config is None:
            issues.append("Missing or invalid config/domain.yaml")
            return issues

        # 2. Check prompts directory for each locale
        prompts_dir = domain_path / "config" / "prompts"
        if not prompts_dir.is_dir():
            issues.append("Missing config/prompts/ directory")
        else:
            for locale in config.locales:
                locale_dir = prompts_dir / locale
                if not locale_dir.is_dir():
                    issues.append(f"Locale '{locale}' declared but dir missing: {locale_dir}")
                elif not list(locale_dir.glob("*.yaml")):
                    issues.append(f"Locale '{locale}' has no YAML files")

        # 3. Check each required plugin exists (plugins may be nested in subdirs)
        plugins_dir = plugins_root
        if plugins_dir.is_dir():
            # Build a name→path map from all plugin.yaml files
            existing_plugins: dict[str, Path] = {}
            for manifest in plugins_dir.glob("**/plugin.yaml"):
                try:
                    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and "name" in raw:
                        existing_plugins[raw["name"]] = manifest.parent
                except yaml.YAMLError:
                    pass
            for plugin_name in config.requires_plugins:
                if plugin_name not in existing_plugins:
                    issues.append(f"Required plugin '{plugin_name}' not found under {plugins_dir}")
        else:
            for plugin_name in config.requires_plugins:
                issues.append(f"Required plugin '{plugin_name}' — plugins directory missing")

        # 4. Check skills directory
        skills_dir = domain_path / "skills"
        if not skills_dir.is_dir():
            issues.append("Missing skills/ directory")
        elif not list(skills_dir.glob("*.md")):
            issues.append("No skill .md files found in skills/")

        return issues
