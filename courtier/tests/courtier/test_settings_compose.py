"""Snapshot composition: precedence, CORS escape hatch, .env seeding,
JWT bootstrap, env-only degradation."""

import pytest

from courtier.config import ConfigService, Settings
from courtier.settings_store import (
    FernetCodec,
    SettingsStore,
    compose_snapshot,
    refresh_settings_snapshot,
    seed_from_env,
)


@pytest.fixture
async def db(tmp_path):
    from courtier.db.db_manager import AsyncDatabase

    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'compose.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


def _store(db, codec=None):
    return SettingsStore(db, codec)


class TestComposeSnapshot:
    def test_db_overrides_env(self):
        base = Settings(llm_model="env-model", logger_level="INFO")
        merged = compose_snapshot(base, {"llm_model": "db-model"})
        assert merged.llm_model == "db-model"
        assert merged.logger_level == "INFO"

    def test_tier0_fields_never_overridden(self):
        base = Settings(mysql_url="mysql://env", upload_dir="/env-uploads")
        merged = compose_snapshot(
            base, {"mysql_url": "mysql://attacker", "upload_dir": "/db-uploads"}
        )
        assert merged.mysql_url == "mysql://env"
        assert merged.upload_dir == "/env-uploads"

    def test_cors_env_escape_hatch(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", '["http://rescue:5173"]')
        base = Settings(cors_origins=["http://rescue:5173"])
        merged = compose_snapshot(base, {"cors_origins": ["http://db:5173"]})
        assert merged.cors_origins == ["http://rescue:5173"]

    def test_cors_db_value_used_without_env_escape(self, monkeypatch):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        base = Settings(cors_origins=["http://base:5173"])
        merged = compose_snapshot(base, {"cors_origins": ["http://db:5173"]})
        assert merged.cors_origins == ["http://db:5173"]

    def test_invalid_merged_value_raises(self):
        base = Settings()
        with pytest.raises(Exception):
            compose_snapshot(base, {"llm_extra_body": "not-json{"})

    def test_nested_agent_runtime_preserved(self):
        base = Settings()
        merged = compose_snapshot(base, {"logger_level": "DEBUG"})
        assert merged.agent_runtime.events.enabled is True


class TestSeedFromEnv:
    async def test_seeds_only_non_default_values(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "seeded-model")
        # _env_file=None keeps the local developer .env out of the test.
        base = Settings(_env_file=None)
        assert base.llm_model == "seeded-model"

        seeded = await seed_from_env(_store(db), base)

        assert "llm_model" in seeded
        # Only genuinely non-default fields are seeded (semantics, not an
        # exact set — the ambient test env may set other vars).
        from courtier.settings_store import _defaults_settings

        defaults = _defaults_settings()
        assert seeded
        assert all(getattr(base, k) != getattr(defaults, k) for k in seeded)

    async def test_secrets_skipped_without_key(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "seeded-model")
        monkeypatch.setenv("LLM_API_KEY", "sk-env")
        monkeypatch.delenv("COURTIER_SETTINGS_KEY", raising=False)
        base = Settings(_env_file=None)

        seeded = await seed_from_env(_store(db), base)

        assert "llm_model" in seeded
        assert "llm_api_key" not in seeded  # no encryption key → skipped

    async def test_secrets_seeded_with_key(self, db, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-env")
        codec = FernetCodec("ab" * 32)
        base = Settings(_env_file=None)

        seeded = await seed_from_env(_store(db, codec), base)

        assert "llm_api_key" in seeded
        overrides, _ = await _store(db, codec).load_overrides()
        assert overrides["llm_api_key"] == "sk-env"


class TestRefreshSnapshot:
    async def test_degrades_to_env_without_store(self):
        service = ConfigService()
        service.get()
        before = service.get()

        info = await refresh_settings_snapshot(service, None)

        assert info["mode"] == "env"
        assert service.get() is before  # snapshot untouched

    async def test_degrades_when_db_unreachable(self):
        class BrokenStore:
            codec = None

            async def load_overrides(self):
                raise RuntimeError("db down")

            async def current_version(self):
                return 0

        service = ConfigService()
        service.get()
        before = service.get()

        info = await refresh_settings_snapshot(service, BrokenStore())

        assert info["mode"] == "env"
        assert service.get() is before

    async def test_full_cycle_replaces_snapshot(self, db, monkeypatch):
        monkeypatch.delenv("COURTIER_SETTINGS_KEY", raising=False)
        monkeypatch.setenv("JWT_SECRET", "env-jwt-secret")  # skip bootstrap path
        codec = FernetCodec("ab" * 32)
        store = _store(db, codec)
        await store.save({"llm_model": "db-model"}, actor="admin")

        service = ConfigService()
        base = Settings()  # env-effective base

        info = await refresh_settings_snapshot(service, store, base=base)

        assert info["mode"] == "db"
        assert service.source == "db"
        assert service.get().llm_model == "db-model"
        assert info["version"] == service.version

    async def test_jwt_bootstrap_generates_and_persists(self, db, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "")
        codec = FernetCodec("cd" * 32)
        store = _store(db, codec)

        service = ConfigService()
        base = Settings(_env_file=None)
        assert base.jwt_secret == ""

        info = await refresh_settings_snapshot(service, store, base=base)

        assert info["jwt_generated"] is True
        assert service.get().jwt_secret  # generated, non-empty
        assert len(service.get().jwt_secret) >= 48

    async def test_jwt_bootstrap_skipped_without_key(self, db, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "")
        store = _store(db, codec=None)

        service = ConfigService()
        base = Settings(_env_file=None)

        info = await refresh_settings_snapshot(service, store, base=base)

        assert info["jwt_generated"] is False
        assert service.get().jwt_secret == ""

    async def test_seeding_runs_once_then_stable(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "seed-once-model")
        monkeypatch.setenv("JWT_SECRET", "env-jwt")
        codec = FernetCodec("ef" * 32)
        store = _store(db, codec)

        service = ConfigService()
        first = await refresh_settings_snapshot(service, store, base=Settings())
        assert first["seeded"]  # imported the env value

        # Second refresh: audit trail non-empty → no re-seed, no duplicate rows.
        monkeypatch.setenv("LLM_NAME", "changed-later")
        second = await refresh_settings_snapshot(service, store, base=Settings())
        assert second["seeded"] == []
        assert service.get().llm_model == "seed-once-model"  # DB wins over new env
