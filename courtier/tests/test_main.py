"""Tests for the root main.py startup entrypoint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_main_module() -> ModuleType:
    """Load the project root main.py explicitly to avoid name collisions."""
    main_path = _project_root() / "main.py"
    spec = importlib.util.spec_from_file_location("courtier_main", str(main_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load main.py from {main_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMainPathSetup:
    def test_injects_monorepo_paths(self):
        """main.py adds the project root and domain dir to sys.path.

        libs/ and docmodels are installed as editable venv packages and
        must NOT appear on sys.path; the domain dir stays for domain
        Python helpers (skills.schemas.*).
        """
        root = _project_root()
        expected_paths = [
            str(root),
            str(root / "domains" / "docaudit"),
        ]
        excluded_paths = [
            str(root / "libs" / "shared"),
            str(root / "libs" / "docaudit"),
        ]

        with patch.object(sys, "path", list(sys.path)):
            _load_main_module()

        for p in expected_paths:
            assert p in sys.path
        for p in excluded_paths:
            assert p not in sys.path


class TestMainArgumentParsing:
    def test_defaults(self):
        """Default host/port/reload values are correct."""
        main = _load_main_module()
        args = main._parse_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 8000
        assert args.reload is False

    def test_custom_values(self):
        """Custom flags override defaults."""
        main = _load_main_module()
        args = main._parse_args(["--host", "127.0.0.1", "--port", "8080", "--reload"])
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.reload is True


class TestMainStartupFlow:
    def test_runs_migrations_and_starts_uvicorn(self):
        """main() runs alembic upgrade then uvicorn.run with factory app."""
        main = _load_main_module()

        with (
            patch.object(main, "alembic_command") as mock_alembic,
            patch.object(main, "uvicorn_run") as mock_uvicorn,
        ):
            main.main(["--host", "127.0.0.1", "--port", "9000"])

        mock_alembic.upgrade.assert_called_once()
        config_arg = mock_alembic.upgrade.call_args[0][0]
        assert config_arg.config_file_name.endswith("alembic.ini")
        assert mock_alembic.upgrade.call_args[0][1] == "head"

        mock_uvicorn.assert_called_once()
        call_args = mock_uvicorn.call_args.args
        call_kwargs = mock_uvicorn.call_args.kwargs
        assert call_args[0] == "courtier.agent.api.app:create_app"
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9000
        assert call_kwargs["factory"] is True
