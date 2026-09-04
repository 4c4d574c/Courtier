"""PluginManifest — Pydantic model for plugin.yaml parsing and validation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    """Plugin standalone-server runtime configuration."""

    language: str = "python"
    entry: str = "entry.py"
    # Default TCP listen port for the standalone plugin server; the SDK
    # falls back to it when neither --listen nor COURTIER_PLUGIN_LISTEN is
    # given.  The host never consumes this field (endpoints come from
    # COURTIER_PLUGIN_ENDPOINTS) — it documents the canonical port next to
    # the plugin.
    port: int | None = None
    # Literal values are applied by the plugin SDK as environment defaults
    # at startup (os.environ.setdefault).  Plugins own their environment;
    # the host injects nothing.
    env: dict[str, str] = Field(default_factory=dict)


# Known host services that plugins can declare dependencies on
KNOWN_HOST_SERVICES = frozenset(
    {"cache", "artifact_store", "storage", "template_store", "memory_store"}
)
# Known permission scopes
KNOWN_PERMISSIONS = frozenset(
    {
        "read:documents",
        "write:artifacts",
        "read:cache",
        "write:cache",
        "read:storage",
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
    timeout_ms: int = 120_000

    # Set by scanner after parsing — not in YAML
    dir: Path | None = Field(default=None, exclude=True)
