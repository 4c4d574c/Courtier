"""staging/production lifespan must refuse misconfigured boots.

The codec fail-fast lives on a branch tests/dev never executes — without
this smoke test a bad import or dropped check ships silently (it once
did: a wrong relative import would have crashed every production boot).
"""

import tempfile

import pytest
from fastapi.testclient import TestClient

import courtier.config as cfg
from courtier.agent.api.app import create_app
from courtier.config import get_settings


def _boot_with(settings_overrides: dict, monkeypatch) -> None:
    """Run create_app + lifespan with the factory snapshot overridden."""
    real = get_settings()
    fake = real.model_copy(update=settings_overrides)
    # create_app imports get_settings at call time from courtier.config.
    monkeypatch.setattr(cfg, "get_settings", lambda: fake)
    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        with TestClient(app):
            pass


def test_production_without_mysql_url_refuses_to_start(monkeypatch):
    with pytest.raises(RuntimeError, match="MYSQL_URL"):
        _boot_with(
            {"deployment_env": "production", "mysql_url": "", "match": "MYSQL_URL"},
            monkeypatch
        )


def test_production_without_settings_key_refuses_to_start(monkeypatch):
    # from_env also reads the repo .env — force the "key missing" branch.
    monkeypatch.setattr(
        "courtier.settings_store.FernetCodec.from_env",
        classmethod(lambda cls: None),
    )
    with pytest.raises(RuntimeError, match="COURTIER_SETTINGS_KEY"):
        _boot_with(
            {
                "deployment_env": "production",
                "mysql_url": "mysql+asyncmy://u:p@db:3306/x",
                "match": "COURTIER_SETTINGS_KEY",
            },
            monkeypatch,
        )


def test_development_boots_without_key(monkeypatch):
    """Dev semantics unchanged: no key, empty mysql_url — boots fine."""
    monkeypatch.setattr(
        "courtier.settings_store.FernetCodec.from_env",
        classmethod(lambda cls: None),
    )
    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
