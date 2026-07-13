"""Test fixtures for the artifacts package.

Resets module-level singletons (ProjectorRegistry, MaterializerRegistry)
so tests that mutate the default instances don't pollute later tests.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_artifact_singletons() -> None:
    """Reset singleton registries before every test."""
    from courtier.agent.artifacts.projectors import reset_default_projector_registry
    from courtier.agent.artifacts.executor import MaterializerRegistry

    reset_default_projector_registry()
    MaterializerRegistry.reset_default()
