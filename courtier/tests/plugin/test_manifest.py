"""Tests for PluginManifest model."""

import pytest
from pydantic import ValidationError

from courtier.plugin.manifest import (
    PluginManifest,
    RuntimeConfig,
    Dependencies,
    Capabilities,
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
        assert m.capabilities == Capabilities()

    def test_full_manifest_with_all_capabilities(self):
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
            "capabilities": {
                "tools": [
                    {
                        "name": "my_tool",
                        "display_name": "My Tool",
                        "description": "Does stuff",
                    }
                ],
                "checkers": [
                    {
                        "name": "my_checker",
                        "doc_type": "通知",
                        "display_name": "My Checker",
                    }
                ],
                "routes": [
                    {
                        "prefix": "/api/v1/custom",
                        "description": "Custom API",
                    }
                ],
                "processors": [
                    {
                        "name": "my_parser",
                        "type": "parser",
                        "display_name": "My Parser",
                    }
                ],
            },
        }
        m = PluginManifest.model_validate(data)
        assert m.runtime.language == "python"
        assert m.runtime.entry == "main.py"
        assert m.runtime.env == {"DEBUG": "true"}
        assert m.dependencies.python == ["httpx>=0.28.0"]
        assert m.dependencies.host_services == ["cache"]
        assert m.dependencies.permissions == ["read:documents"]
        assert len(m.capabilities.tools) == 1
        assert m.capabilities.tools[0].name == "my_tool"
        assert len(m.capabilities.checkers) == 1
        assert m.capabilities.checkers[0].doc_type == "通知"
        assert len(m.capabilities.routes) == 1
        assert len(m.capabilities.processors) == 1

    def test_defaults_for_empty_capabilities(self):
        m = PluginManifest.model_validate({"name": "p", "version": "0.1", "api": "1.0"})
        assert m.capabilities.tools == []
        assert m.capabilities.checkers == []
        assert m.capabilities.routes == []
        assert m.capabilities.processors == []

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
