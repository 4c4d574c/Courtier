"""Project-level pytest configuration and shared fixtures.

TODO: Add Alembic migration smoke tests:
- Verify `alembic upgrade head` succeeds against a fresh test database
- Verify `alembic downgrade -1` and re-upgrade to catch rollback issues
- Run `alembic check` to confirm no unmerged migration branches
- Consider a pytest fixture that creates a temporary MySQL/Postgres container
  (testcontainers) and runs the full migration chain against it
"""

import os
import sys
from pathlib import Path

# MUST run at module level (before any conftest or test module import)
# because tests/agent/conftest.py imports src.agent.core.state, which
# transitively imports src.config → Settings() validates secrets at
# import time.  Overriding DEPLOYMENT_ENVIRONMENT in os.environ takes
# precedence over the .env file value in pydantic-settings v2.
os.environ["DEPLOYMENT_ENVIRONMENT"] = "development"

# Auth middleware requires valid credentials even in development mode.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-pytest-at-least-32-bytes")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password-for-pytest")

# Force fallback (no-DB) login path by default. Integration tests that
# need a real database can set MYSQL_URL explicitly in their own fixtures.
os.environ["MYSQL_URL"] = ""

# When pytest collects from both ``tests/`` and ``src/`` trees in one
# invocation, the ``pythonpath`` setting in pyproject.toml is sometimes
# not resolved before in-source test modules are imported.  Ensure
# ``src/`` is on sys.path unconditionally so that imports like
# ``from docparse.parsers import ...`` work everywhere.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Conftests and tests also import via the ``src.`` and ``plugins.`` prefixes
# (e.g. ``from courtier.agent.core.state import ...`` in tests/agent/conftest.py,
# ``from plugins.audit... import ...`` in tests/plugin).  Those require the
# project root on sys.path.  pytest only guarantees this for some invocations
# (bare ``pytest`` from the root); add it explicitly so ``pytest tests/`` works
# regardless of how collection paths are passed on the command line.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Add package directories for the restructured layout (courtier core + domain)
for _sub in ("packages/core/src", "packages/domains/docaudit", "packages/domains/docaudit/plugins"):
    _pkg_path = str(Path(_ROOT) / _sub)
    if _pkg_path not in sys.path:
        sys.path.insert(0, _pkg_path)
