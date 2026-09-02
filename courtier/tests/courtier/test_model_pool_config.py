"""Model pool config: pool reference validators, settings meta wiring,
endpoint key map secret round-trip (JSON-string tolerance), and snapshot
composition of the new llm_* pool fields."""

import json

import pytest
from pydantic import ValidationError

from courtier.config import SETTINGS_META, ModelPoolConfig, Settings
from courtier.settings_store import compose_snapshot


def _pool() -> dict:
    return {
        "endpoints": [
            {
                "id": "ep_main",
                "name": "主接入点",
                "base_url": "https://api.example.com/v1",
                "enabled": True,
                "models": [
                    {"id": "mdl_main", "name": "主模型", "model": "main-model"},
                    {
                        "id": "mdl_big",
                        "name": "大窗口模型",
                        "model": "big-model",
                        "context_window_tokens": 131072,
                        "max_tokens": 8192,
                        "temperature": 0.3,
                    },
                ],
            }
        ],
        "default_model_id": "mdl_main",
    }


class TestPoolValidators:
    def test_valid_pool_parses(self):
        pool = ModelPoolConfig.model_validate(_pool())

        assert pool.default_model_id == "mdl_main"
        endpoint = pool.endpoints[0]
        assert endpoint.name == "主接入点"
        assert endpoint.models[1].context_window_tokens == 131072
        assert endpoint.models[1].temperature == 0.3

    def test_empty_pool_is_valid(self):
        pool = ModelPoolConfig()
        assert pool.endpoints == []
        assert pool.default_model_id == ""

    def test_duplicate_endpoint_ids_rejected(self):
        raw = _pool()
        raw["endpoints"].append(dict(raw["endpoints"][0], models=[]))
        with pytest.raises(ValidationError, match="duplicate endpoint ids"):
            ModelPoolConfig.model_validate(raw)

    def test_duplicate_model_ids_across_endpoints_rejected(self):
        raw = _pool()
        raw["endpoints"].append(
            {
                "id": "ep_second",
                "name": "第二接入点",
                "base_url": "https://other.example.com/v1",
                "models": [{"id": "mdl_main", "name": "重名", "model": "x"}],
            }
        )
        with pytest.raises(ValidationError, match="duplicate model ids"):
            ModelPoolConfig.model_validate(raw)

    def test_empty_ids_rejected(self):
        raw = _pool()
        raw["endpoints"][0]["id"] = "  "
        with pytest.raises(ValidationError, match="non-empty"):
            ModelPoolConfig.model_validate(raw)

    def test_default_not_found_rejected(self):
        raw = _pool()
        raw["default_model_id"] = "mdl_ghost"
        with pytest.raises(ValidationError, match="not found in pool"):
            ModelPoolConfig.model_validate(raw)

    def test_default_on_disabled_endpoint_rejected(self):
        raw = _pool()
        raw["endpoints"][0]["enabled"] = False
        with pytest.raises(ValidationError, match="disabled endpoint"):
            ModelPoolConfig.model_validate(raw)

    def test_out_of_range_overrides_rejected(self):
        with pytest.raises(ValidationError):
            ModelPoolConfig.model_validate(
                {
                    "endpoints": [
                        {
                            "id": "ep_x",
                            "name": "x",
                            "base_url": "https://x/v1",
                            "models": [
                                {"id": "mdl_x", "name": "x", "model": "m", "temperature": 5.0}
                            ],
                        }
                    ]
                }
            )


class TestSettingsFields:
    def test_defaults_are_empty_pool(self):
        settings = Settings(_env_file=None)
        assert settings.llm_model_pool.endpoints == []
        assert settings.llm_endpoint_keys == {}
        assert settings.llm_embedding_base_url == ""
        assert settings.llm_embedding_api_key == ""

    def test_endpoint_keys_accept_dict_and_json_string(self):
        from_dict = Settings(_env_file=None, llm_endpoint_keys={"ep_main": "sk-1"})
        from_json = Settings(_env_file=None, llm_endpoint_keys=json.dumps({"ep_main": "sk-1"}))
        assert from_dict.llm_endpoint_keys == from_json.llm_endpoint_keys == {"ep_main": "sk-1"}

    def test_endpoint_keys_empty_shapes(self):
        assert Settings(_env_file=None, llm_endpoint_keys="").llm_endpoint_keys == {}
        assert Settings(_env_file=None, llm_endpoint_keys=None).llm_endpoint_keys == {}

    def test_endpoint_keys_rejects_bad_payloads(self):
        with pytest.raises(ValidationError, match="valid JSON string"):
            Settings(_env_file=None, llm_endpoint_keys="not-json{")
        with pytest.raises(ValidationError, match="JSON object"):
            Settings(_env_file=None, llm_endpoint_keys='["a"]')

    def test_env_json_pool_override(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL_POOL", raising=False)
        monkeypatch.setenv("LLM_MODEL_POOL", json.dumps(_pool()))
        settings = Settings(_env_file=None)
        assert settings.llm_model_pool.default_model_id == "mdl_main"

    def test_meta_wiring(self):
        pool_meta = SETTINGS_META["llm_model_pool"]
        assert (pool_meta.category, pool_meta.is_secret, pool_meta.effect) == (
            "model",
            False,
            "hot",
        )
        keys_meta = SETTINGS_META["llm_endpoint_keys"]
        assert (keys_meta.category, keys_meta.is_secret, keys_meta.effect) == (
            "model",
            True,
            "hot",
        )
        emb_url = SETTINGS_META["llm_embedding_base_url"]
        assert (emb_url.category, emb_url.is_secret) == ("model", False)
        emb_key = SETTINGS_META["llm_embedding_api_key"]
        assert (emb_key.category, emb_key.is_secret) == ("model", True)


class TestSnapshotComposition:
    def test_pool_db_override_dict_coerced(self):
        merged = compose_snapshot(Settings(_env_file=None), {"llm_model_pool": _pool()})
        assert isinstance(merged.llm_model_pool, ModelPoolConfig)
        assert merged.llm_model_pool.default_model_id == "mdl_main"

    def test_endpoint_keys_json_string_round_trip(self):
        # Simulates the secret store round-trip: save encrypts str(value),
        # load returns the plaintext JSON string.
        merged = compose_snapshot(
            Settings(_env_file=None),
            {"llm_endpoint_keys": json.dumps({"ep_main": "sk-1"})},
        )
        assert merged.llm_endpoint_keys == {"ep_main": "sk-1"}

    def test_invalid_pool_rejected_loudly(self):
        bad = _pool()
        bad["default_model_id"] = "mdl_ghost"
        with pytest.raises(Exception):
            compose_snapshot(Settings(_env_file=None), {"llm_model_pool": bad})

    def test_pool_survives_recomposition(self):
        # model_dump() → model_validate() round-trip must not lose the
        # validators' guarantees (compose_snapshot recomposes on every save).
        first = compose_snapshot(Settings(_env_file=None), {"llm_model_pool": _pool()})
        second = compose_snapshot(first, {"logger_level": "DEBUG"})
        assert second.llm_model_pool.default_model_id == "mdl_main"
