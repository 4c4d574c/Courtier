"""Courtier backend startup entrypoint.

Usage:
    uv run main.py
    uv run main.py --host 127.0.0.1 --port 8080 --reload
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from alembic.config import Config as AlembicConfig

from alembic import command as alembic_command

# Alias for testability.
uvicorn_run = uvicorn.run


def _inject_monorepo_paths() -> None:
    """Add source directories to sys.path when running from sources.

    libs/, docmodels and the plugin SDK are installed into the venv as
    editable packages (see [tool.uv.sources] in pyproject.toml).  The domain
    package directory stays on sys.path because domain Python helpers
    (``skills.schemas.*``) are imported lazily by the agent runtime
    (``agents/input_models.py``).
    """
    project_root = Path(__file__).resolve().parent
    paths = [
        str(project_root),
        str(project_root / "domains" / "docaudit"),
    ]
    # Prepend so these take priority over any other installed packages.
    for path in reversed(paths):
        if path not in sys.path:
            sys.path.insert(0, path)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the backend server."""
    parser = argparse.ArgumentParser(description="Courtier backend server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind socket to this host")
    parser.add_argument("--port", type=int, default=8000, help="Bind socket to this port")
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload on code changes",
    )
    return parser.parse_args(argv)


def _run_migrations() -> None:
    """Apply all pending Alembic migrations."""
    project_root = Path(__file__).resolve().parent
    alembic_cfg = AlembicConfig(str(project_root / "alembic.ini"))
    alembic_command.upgrade(alembic_cfg, "head")


def main(argv: list[str] | None = None) -> None:
    """Run migrations and start the uvicorn server."""
    _inject_monorepo_paths()
    args = _parse_args(argv)
    _run_migrations()
    uvicorn_run(
        "courtier.agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
