"""Test fixtures for the artifacts package.

Resets module-level singletons (ProjectorRegistry, MaterializerRegistry)
so tests that mutate the default instances don't pollute later tests.
"""
from __future__ import annotations

import domain_artifacts  # domains/docaudit is on the pytest pythonpath
import pytest

from courtier.agent.artifacts.executor import MaterializerRegistry
from courtier.agent.artifacts.models import default_registry
from courtier.agent.artifacts.projectors import register_rebuild_hook


# Production registers domain artifact profiles via rebuild hooks on domain
# activation; the test suites exercise docaudit types directly, so hook it
# here (survives the *_reset_default() teardowns).
def _register_docaudit_materializers(reg) -> None:
    # reg IS the materializer registry being built — calling default()
    # here would recurse.
    domain_artifacts.register_domain_artifacts(default_registry, reg)


def _register_docaudit_projectors(reg) -> None:
    domain_artifacts.register_domain_artifacts(
        default_registry, MaterializerRegistry.default(), reg
    )


MaterializerRegistry.register_rebuild_hook("docaudit", _register_docaudit_materializers)
register_rebuild_hook("docaudit", _register_docaudit_projectors)


@pytest.fixture(autouse=True)
def _reset_artifact_singletons() -> None:
    """Reset singleton registries before every test."""
    from courtier.agent.artifacts.executor import MaterializerRegistry
    from courtier.agent.artifacts.projectors import reset_default_projector_registry

    reset_default_projector_registry()
    MaterializerRegistry.reset_default()
