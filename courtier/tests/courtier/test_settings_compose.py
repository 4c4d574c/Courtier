"""Snapshot composition: precedence, CORS escape hatch, .env seeding,
scalar→model-pool migration, JWT bootstrap, env-only degradation."""

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

    def test_guardrail_layer_modes_compose(self):
        merged = compose_snapshot(Settings(), {"guardrail_output_layer": "block"})
        assert merged.guardrail_output_layer == "block"

    def test_guardrail_tool_call_layer_rejects_release_modes(self):
        """tool_call 权限层只允许 block/log——off/allow 等旁路档位直接拒绝。"""
        for value in ("off", "allow"):
            with pytest.raises(Exception):
                compose_snapshot(Settings(), {"guardrail_tool_call_layer": value})


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


class TestModelPoolSeed:
    async def _seeded(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "legacy-model")
        monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
        monkeypatch.delenv("LLM_MODEL_POOL", raising=False)
        codec = FernetCodec("ab" * 32)
        store = _store(db, codec)
        service = ConfigService()
        info = await refresh_settings_snapshot(service, store, base=Settings(_env_file=None))
        return store, service, info

    async def test_seeds_single_endpoint_pool_from_scalars(self, db, monkeypatch):
        store, service, info = await self._seeded(db, monkeypatch)

        assert info["pool_seeded"] is True
        pool = service.get().llm_model_pool
        assert pool.default_model_id == "mdl_main"
        assert pool.endpoints[0].id == "ep_main"
        assert pool.endpoints[0].base_url
        assert pool.endpoints[0].models[0].model == "legacy-model"
        assert service.get().llm_endpoint_keys == {"ep_main": "sk-legacy"}

        # Stable on re-refresh: the pool row exists, no re-seed.
        info2 = await refresh_settings_snapshot(service, store, base=Settings(_env_file=None))
        assert info2["pool_seeded"] is False
        assert service.get().llm_model_pool.default_model_id == "mdl_main"

    async def test_admin_clear_is_respected(self, db, monkeypatch):
        store, service, _ = await self._seeded(db, monkeypatch)
        await store.delete(["llm_model_pool", "llm_endpoint_keys"], actor="admin")

        info = await refresh_settings_snapshot(service, store, base=Settings(_env_file=None))

        assert info["pool_seeded"] is False
        assert service.get().llm_model_pool.endpoints == []
        # Scalar fallback stays active after the clear.
        assert service.get().llm_model == "legacy-model"

    async def test_db_pool_wins_over_seed(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "legacy-model")
        codec = FernetCodec("ab" * 32)
        store = _store(db, codec)
        await store.save(
            {
                "llm_model_pool": {
                    "endpoints": [
                        {
                            "id": "ep_custom",
                            "name": "自定义",
                            "base_url": "https://custom/v1",
                            "models": [{"id": "mdl_custom", "name": "C", "model": "c"}],
                        }
                    ],
                    "default_model_id": "mdl_custom",
                }
            },
            actor="admin",
        )
        service = ConfigService()

        info = await refresh_settings_snapshot(service, store, base=Settings(_env_file=None))

        assert info["pool_seeded"] is False
        assert service.get().llm_model_pool.default_model_id == "mdl_custom"

    async def test_no_seed_without_codec(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "legacy-model")
        store = _store(db, codec=None)
        service = ConfigService()

        info = await refresh_settings_snapshot(service, store, base=Settings(_env_file=None))

        assert info["pool_seeded"] is False
        assert service.get().llm_model_pool.endpoints == []

    async def test_no_seed_when_scalars_empty(self, db, monkeypatch):
        monkeypatch.delenv("LLM_NAME", raising=False)
        monkeypatch.delenv("LLM_IP", raising=False)
        codec = FernetCodec("ab" * 32)
        store = _store(db, codec)
        service = ConfigService()
        base = Settings(_env_file=None, llm_base_url="", llm_model="")

        info = await refresh_settings_snapshot(service, store, base=base)

        assert info["pool_seeded"] is False
        assert service.get().llm_model_pool.endpoints == []


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
        # API-facing version is the durable store sequence, not the
        # process-local service counter.
        assert info["version"] == await store.current_version()

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


class TestAdvancedDefaultsMaterialization:
    """存量池的一次性物化：None 高级字段写入标量默认值，标记防重跑。"""

    async def _unmaterialized_pool(self):
        return {
            "endpoints": [
                {
                    "id": "ep_legacy",
                    "name": "旧接入点",
                    "base_url": "https://legacy/v1",
                    "models": [
                        {
                            "id": "mdl_legacy",
                            "name": "旧模型",
                            "model": "legacy-model",
                            "temperature": 0.4,  # 已有值 → 不覆盖
                            # 其余高级字段 None → 物化
                        }
                    ],
                }
            ],
            "default_model_id": "mdl_legacy",
        }

    async def test_startup_materializes_once(self, db, monkeypatch):
        monkeypatch.setenv("LLM_NAME", "legacy-model")
        monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
        monkeypatch.delenv("LLM_MODEL_POOL", raising=False)
        codec = FernetCodec("ab" * 32)
        store = _store(db, codec)
        await store.save({"llm_model_pool": await self._unmaterialized_pool()}, actor="admin")
        service = ConfigService()

        info = await refresh_settings_snapshot(
            service, store, base=Settings(_env_file=None)
        )

        assert info["pool_advanced_baked"] is True
        pool = service.get().llm_model_pool
        assert pool.advanced_defaults_materialized is True
        entry = pool.endpoints[0].models[0]
        assert entry.temperature == 0.4  # 已有值不被覆盖
        assert entry.timeout_seconds == Settings(_env_file=None).llm_timeout
        assert entry.max_tokens == Settings(_env_file=None).llm_max_tokens

        # 幂等：第二次 refresh 不再改写（标记已置位）
        entry_before = service.get().llm_model_pool.endpoints[0].models[0]
        info2 = await refresh_settings_snapshot(
            service, store, base=Settings(_env_file=None)
        )
        assert info2["pool_advanced_baked"] is False
        assert service.get().llm_model_pool.endpoints[0].models[0] == entry_before
