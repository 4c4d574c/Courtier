"""Model profile resolution (pool pick precedence) and per-profile model
client construction."""

from types import SimpleNamespace

import pytest

import courtier.agent.core.backends.openai_backend as backend_mod
from courtier.agent.api.services.agent_service import (
    UnknownModelError,
    _context_budget_kwargs,
    build_model_client,
    resolve_model_profile,
)
from courtier.config import (
    AgentRuntimeConfig,
    ModelRoutingConfig,
    PoolModelConfig,
    Settings,
)


def _pool() -> dict:
    return {
        "endpoints": [
            {
                "id": "ep_a",
                "name": "接入点A",
                "base_url": "https://a.example.com/v1",
                "enabled": True,
                "models": [
                    {"id": "mdl_a1", "name": "A1", "model": "model-a1"},
                    {
                        "id": "mdl_a2",
                        "name": "A2",
                        "model": "model-a2",
                        "context_window_tokens": 65536,
                        "max_tokens": 2048,
                        "temperature": 0.5,
                    },
                ],
            },
            {
                "id": "ep_b",
                "name": "接入点B",
                "base_url": "https://b.example.com/v1",
                "enabled": False,
                "models": [{"id": "mdl_b1", "name": "B1", "model": "model-b1"}],
            },
        ],
        "default_model_id": "mdl_a1",
    }


def _settings(keys: dict | None = None, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        llm_model_pool=_pool(),
        llm_endpoint_keys={"ep_a": "sk-a"} if keys is None else keys,
        **kwargs,
    )


def _broken_pool_settings() -> SimpleNamespace:
    """A pool whose default points at a disabled endpoint — unreachable via
    the validated Settings type, but possible through external DB edits."""
    return SimpleNamespace(
        llm_model_pool=SimpleNamespace(
            endpoints=[
                SimpleNamespace(
                    id="ep_x",
                    name="x",
                    base_url="https://x/v1",
                    enabled=False,
                    models=[
                        SimpleNamespace(
                            id="mdl_x",
                            name="x",
                            model="m",
                            context_window_tokens=None,
                            max_tokens=None,
                            temperature=None,
                        )
                    ],
                )
            ],
            default_model_id="mdl_x",
        ),
        llm_endpoint_keys={},
    )


class TestResolveModelProfile:
    def test_empty_pool_returns_none(self):
        assert resolve_model_profile(Settings(_env_file=None)) is None

    def test_explicit_id_on_empty_pool_raises(self):
        with pytest.raises(UnknownModelError):
            resolve_model_profile(Settings(_env_file=None), model_id="mdl_any")

    def test_explicit_id_wins(self):
        profile = resolve_model_profile(_settings(), model_id="mdl_a2")
        assert profile.model_id == "mdl_a2"
        assert profile.name == "A2"
        assert profile.base_url == "https://a.example.com/v1"
        assert profile.api_key == "sk-a"
        assert profile.model == "model-a2"
        assert profile.context_window_tokens == 65536
        assert profile.max_tokens == 2048
        assert profile.temperature == 0.5

    def test_unknown_id_raises(self):
        with pytest.raises(UnknownModelError):
            resolve_model_profile(_settings(), model_id="mdl_ghost")

    def test_disabled_endpoint_is_unresolvable(self):
        with pytest.raises(UnknownModelError):
            resolve_model_profile(_settings(), model_id="mdl_b1")

    def test_session_last_used_without_explicit(self):
        profile = resolve_model_profile(_settings(), session_last_model_id="mdl_a2")
        assert profile.model_id == "mdl_a2"

    def test_stale_session_last_falls_to_default(self):
        profile = resolve_model_profile(_settings(), session_last_model_id="mdl_gone")
        assert profile.model_id == "mdl_a1"

    def test_default_used_when_no_hints(self):
        assert resolve_model_profile(_settings()).model_id == "mdl_a1"

    def test_missing_key_entry_yields_empty_key(self):
        profile = resolve_model_profile(_settings(keys={}), model_id="mdl_a2")
        assert profile.api_key == ""

    def test_unresolvable_pool_returns_none(self):
        assert resolve_model_profile(_broken_pool_settings()) is None

    def test_explicit_id_on_broken_pool_raises(self):
        with pytest.raises(UnknownModelError):
            resolve_model_profile(_broken_pool_settings(), model_id="mdl_x")


@pytest.fixture
def captured_backend(monkeypatch):
    captured: dict = {}

    class _FakeBackend:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(backend_mod, "OpenAIModelBackend", _FakeBackend)
    return captured


class TestBuildModelClientWithProfile:
    def test_backend_targets_profile(self, captured_backend):
        settings = _settings(
            llm_temperature=0.9,
            llm_max_tokens=777,
            llm_extra_body={"foo": "bar"},
        )
        profile = resolve_model_profile(settings, model_id="mdl_a2")
        client = build_model_client(settings, profile)

        assert captured_backend["base_url"] == "https://a.example.com/v1"
        assert captured_backend["api_key"] == "sk-a"
        assert captured_backend["model"] == "model-a2"
        # per-entry overrides
        assert captured_backend["temperature"] == 0.5
        assert captured_backend["max_tokens"] == 2048
        # globals still apply where the entry has no override
        assert captured_backend["extra_body"] == {"foo": "bar"}
        assert client.model_name == "model-a2"
        assert client.temperature == 0.5

    def test_advanced_overrides_shadow_globals(self, captured_backend):
        pool = _pool()
        pool["endpoints"][0]["models"][0].update(
            {
                "timeout_seconds": 60.0,
                "frequency_penalty": 0.5,
                "presence_penalty": 0.3,
                "extra_body": {"per_model": True},
            }
        )
        settings = Settings(
            _env_file=None,
            llm_model_pool=pool,
            llm_endpoint_keys={"ep_a": "sk-a"},
            llm_timeout=180.0,
            llm_frequency_penalty=0.0,
            llm_presence_penalty=0.0,
            llm_extra_body={"global": True},
        )
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        build_model_client(settings, profile)

        assert captured_backend["timeout"] == 60.0
        assert captured_backend["frequency_penalty"] == 0.5
        assert captured_backend["presence_penalty"] == 0.3
        assert captured_backend["extra_body"] == {"per_model": True}

    def test_advanced_unset_falls_back_to_globals(self, captured_backend):
        settings = _settings(
            llm_timeout=42.0,
            llm_frequency_penalty=0.7,
            llm_presence_penalty=0.8,
            llm_extra_body={"g": 1},
        )
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        build_model_client(settings, profile)

        assert captured_backend["timeout"] == 42.0
        assert captured_backend["frequency_penalty"] == 0.7
        assert captured_backend["presence_penalty"] == 0.8
        assert captured_backend["extra_body"] == {"g": 1}

    def test_entry_overrides_fall_back_to_globals(self, captured_backend):
        settings = _settings(llm_temperature=0.9, llm_max_tokens=777)
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        build_model_client(settings, profile)

        assert captured_backend["temperature"] == 0.9
        assert captured_backend["max_tokens"] == 777

    def test_profile_bypasses_fallback_router(self, captured_backend):
        settings = _settings(
            agent_runtime=AgentRuntimeConfig(
                model=ModelRoutingConfig(fallback_backends=["fb-model"])
            )
        )
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        client = build_model_client(settings, profile)

        assert captured_backend["model"] == "model-a1"
        assert type(client._backend).__name__ == "_FakeBackend"  # not a ModelRouter

    def test_scalar_path_still_routes_fallbacks(self, captured_backend):
        from courtier.agent.core.backends.router import ModelRouter

        settings = Settings(
            _env_file=None,
            agent_runtime=AgentRuntimeConfig(
                model=ModelRoutingConfig(fallback_backends=["fb-model"])
            ),
        )
        client = build_model_client(settings)

        assert isinstance(client._backend, ModelRouter)
        assert client.model_name == settings.llm_model


class TestContextWindowOverride:
    def test_profile_window_overrides_global(self):
        settings = Settings(_env_file=None, llm_context_window_tokens=32768)
        profile = SimpleNamespace(context_window_tokens=131072)

        kwargs = _context_budget_kwargs(settings, profile)

        assert kwargs["max_context_tokens"] == int(131072 * 0.75)
        assert kwargs["micro_compact_tokens"] == int(131072 * 0.60)

    def test_no_profile_uses_global_window(self):
        settings = Settings(_env_file=None, llm_context_window_tokens=32768)

        kwargs = _context_budget_kwargs(settings, None)

        assert kwargs["max_context_tokens"] == int(32768 * 0.75)

    def test_profile_without_window_keeps_global(self):
        settings = Settings(_env_file=None, llm_context_window_tokens=32768)
        profile = SimpleNamespace(context_window_tokens=None)

        kwargs = _context_budget_kwargs(settings, profile)

        assert kwargs["max_context_tokens"] == int(32768 * 0.75)


class TestModalities:
    """静态 modalities 声明：解析透传 + 媒体附件门控（T2）。"""

    def test_pool_entry_modalities_default_empty(self):
        settings = _settings()
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        assert profile is not None
        assert profile.modalities == ()

    def test_pool_entry_modalities_pass_through(self):
        pool = _pool()
        pool["endpoints"][0]["models"][1]["modalities"] = ["vision", "audio"]
        settings = Settings(
            _env_file=None, llm_model_pool=pool, llm_endpoint_keys={"ep_a": "sk-a"}
        )
        profile = resolve_model_profile(settings, model_id="mdl_a2")
        assert profile is not None
        assert profile.modalities == ("vision", "audio")

    def test_pool_entry_rejects_unknown_modality(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            PoolModelConfig(id="m", name="m", model="m", modalities=["smell"])

    def test_gate_passes_without_media(self):
        from courtier.agent.api.services.agent_service import (
            ensure_model_supports_media,
        )

        ensure_model_supports_media(None, [])
        profile = resolve_model_profile(_settings(), model_id="mdl_a1")
        ensure_model_supports_media(profile, [])

    def test_gate_passes_when_modality_declared(self):
        from courtier.agent.api.services.agent_service import (
            ensure_model_supports_media,
        )

        pool = _pool()
        pool["endpoints"][0]["models"][0]["modalities"] = ["vision"]
        settings = Settings(
            _env_file=None, llm_model_pool=pool, llm_endpoint_keys={"ep_a": "sk-a"}
        )
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        ensure_model_supports_media(profile, ["image"])

    def test_gate_rejects_missing_modality(self):
        from courtier.agent.api.services.agent_service import (
            MediaUnsupportedError,
            ensure_model_supports_media,
        )

        pool = _pool()
        pool["endpoints"][0]["models"][0]["modalities"] = ["vision"]
        settings = Settings(
            _env_file=None, llm_model_pool=pool, llm_endpoint_keys={"ep_a": "sk-a"}
        )
        profile = resolve_model_profile(settings, model_id="mdl_a1")
        with pytest.raises(MediaUnsupportedError, match="audio"):
            ensure_model_supports_media(profile, ["audio"])
        # 多附件报告全部缺失的 kind，image 在声明内不被点名
        with pytest.raises(MediaUnsupportedError) as exc:
            ensure_model_supports_media(profile, ["image", "audio", "video"])
        assert "audio" in str(exc.value) and "video" in str(exc.value)
        assert "image" not in str(exc.value)

    def test_gate_scalar_fallback_is_text_only(self):
        from courtier.agent.api.services.agent_service import (
            MediaUnsupportedError,
            ensure_model_supports_media,
        )

        with pytest.raises(MediaUnsupportedError):
            ensure_model_supports_media(None, ["image"])
