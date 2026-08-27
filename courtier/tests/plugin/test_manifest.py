"""Tests for PluginManifest model."""

import pytest
from pydantic import ValidationError

from courtier.plugin.manifest import (
    Dependencies,
    PluginManifest,
    RuntimeConfig,
)


class TestPluginManifest:
    def test_minimal_valid_manifest(self):
        data = {
            "name": "my_plugin",
            "version": "0.1.0",
            "api": "1.0",
        }
        m = PluginManifest.model_validate(data)
        assert m.name == "my_plugin"
        assert m.version == "0.1.0"
        assert m.api == "1.0"
        assert m.description == ""
        assert m.runtime == RuntimeConfig()
        assert m.dependencies == Dependencies()

    def test_full_manifest_without_capabilities(self):
        data = {
            "name": "full_plugin",
            "version": "1.2.3",
            "api": "1.0",
            "description": "A full-featured plugin",
            "author": "Test Author",
            "runtime": {
                "language": "python",
                "entry": "main.py",
                "env": {"DEBUG": "true"},
            },
            "dependencies": {
                "python": ["httpx>=0.28.0"],
                "host_services": ["cache"],
                "permissions": ["read:documents"],
            },
        }
        m = PluginManifest.model_validate(data)
        assert m.runtime.language == "python"
        assert m.runtime.entry == "main.py"
        assert m.runtime.env == {"DEBUG": "true"}
        assert m.dependencies.python == ["httpx>=0.28.0"]
        assert m.dependencies.host_services == ["cache"]
        assert m.dependencies.permissions == ["read:documents"]

    def test_legacy_capabilities_block_is_rejected(self):
        """Manifests are runtime-only: a capabilities block is a hard error.

        ``extra="forbid"`` rejects the undeclared key so leftover dead
        config surfaces at scan time instead of being silently tolerated.
        """
        data = {
            "name": "legacy_plugin",
            "version": "1.2.3",
            "api": "1.0",
            "capabilities": {"tools": [{"name": "my_tool"}]},
        }
        with pytest.raises(ValidationError) as exc:
            PluginManifest.model_validate(data)
        assert "capabilities" in str(exc.value)

    def test_license_alias_mapping(self):
        m = PluginManifest.model_validate(
            {"name": "p", "version": "0.1", "api": "1.0", "license": "MIT"}
        )
        assert m.license_ == "MIT"
        assert m.model_dump(by_alias=True)["license"] == "MIT"

    def test_name_is_required(self):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({"version": "0.1", "api": "1.0"})

    def test_api_is_required(self):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({"name": "p", "version": "0.1"})
