"""PluginManifest — Pydantic model for plugin.yaml parsing and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    """Plugin subprocess runtime configuration."""

    language: str = "python"
    entry: str = "entry.py"
    env: dict[str, str] = Field(default_factory=dict)


# Known host services that plugins can declare dependencies on
KNOWN_HOST_SERVICES = frozenset({"cache", "artifact_store", "storage", "template_store"})

# Known permission scopes
KNOWN_PERMISSIONS = frozenset(
    {
        "read:documents",
        "write:artifacts",
        "read:cache",
        "write:cache",
        "write:storage",
        "read:templates",
        "network:outbound",
    }
)


class Dependencies(BaseModel):
    """Plugin dependency declarations."""

    python: list[str] = Field(default_factory=list)
    host_services: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class ToolCapability(BaseModel):
    """A tool capability declaration."""

    name: str
    display_name: str = ""
    description: str = ""
    input_contract: dict[str, Any] | None = None
    output_contract: dict[str, Any] | None = None


class CheckerCapability(BaseModel):
    """A content checker capability declaration."""

    name: str
    doc_type: str
    display_name: str = ""


class RouteCapability(BaseModel):
    """An API route capability declaration."""

    prefix: str
    description: str = ""


class ProcessorCapability(BaseModel):
    """A document processor capability declaration."""

    name: str
    type: str  # parser | corrector | builder
    display_name: str = ""


class Capabilities(BaseModel):
    """All capabilities declared by a plugin."""

    tools: list[ToolCapability] = Field(default_factory=list)
    checkers: list[CheckerCapability] = Field(default_factory=list)
    routes: list[RouteCapability] = Field(default_factory=list)
    processors: list[ProcessorCapability] = Field(default_factory=list)
    system_prompt: str = ""


class PluginManifest(BaseModel):
    """Parsed plugin.yaml content.

    The `dir` field records the plugin's filesystem location.
    It is set by the scanner after parsing and is excluded from
    serialization since it is not part of the YAML schema.
    """

    model_config = {"extra": "forbid"}

    name: str
    version: str
    api: str
    description: str = ""
    author: str = ""
    license_: str = Field(default="", alias="license")

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    timeout_ms: int = 120_000

    # Set by scanner after parsing — not in YAML
    dir: Path | None = Field(default=None, exclude=True)
