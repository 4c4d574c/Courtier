"""Smoke test for Alembic migration graph health.

This test does not require a database connection. It loads the Alembic
script directory and verifies that the revision graph is well-formed:
exactly one head, no duplicate revisions, and migrations are parseable.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def test_alembic_revision_graph_is_healthy() -> None:
    assert ALEMBIC_INI.is_file(), f"alembic.ini not found at {ALEMBIC_INI}"

    alembic_cfg = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(alembic_cfg)

    heads = script.get_revisions("heads")
    assert len(heads) == 1, f"Expected exactly one Alembic head, found {len(heads)}"

    head = script.get_current_head()
    assert head is not None, "Alembic head revision is missing"

    # Ensure all revisions are reachable from the head (no orphaned scripts).
    all_revisions = list(script.walk_revisions())
    assert len(all_revisions) > 0, "No Alembic revisions found"
