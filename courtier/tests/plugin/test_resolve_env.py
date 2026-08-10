"""Tests for ${ENV:VAR} resolution in plugin manifests (_resolve_env)."""

from types import SimpleNamespace

from courtier.plugin import manager as manager_mod
from courtier.plugin.manager import _resolve_env

_DOCPARSE_VARS = (
    "LLM_IP",
    "LLM_API_KEY",
    "LLM_NAME",
    "DOCPARSE_OCR_API_URL",
    "DOCPARSE_OCR_LANG",
    "DOCPARSE_OCR_ENGINE",
    "DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE",
)


class TestResolveEnv:
    def test_allowed_var_resolves_from_os_environ(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
        assert _resolve_env("${ENV:LLM_API_KEY}") == "sk-from-env"

    def test_allowed_var_falls_back_to_settings(self, monkeypatch):
        """pydantic reads .env into Settings without writing os.environ;
        manifests must still resolve .env-only variables."""
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setattr(
            "courtier.config.get_settings",
            lambda: SimpleNamespace(llm_api_key="sk-from-settings"),
        )
        assert _resolve_env("${ENV:LLM_API_KEY}") == "sk-from-settings"

    def test_os_environ_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
        monkeypatch.setattr(
            "courtier.config.get_settings",
            lambda: SimpleNamespace(llm_api_key="sk-from-settings"),
        )
        assert _resolve_env("${ENV:LLM_API_KEY}") == "sk-from-env"

    def test_missing_everywhere_resolves_empty(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setattr("courtier.config.get_settings", lambda: SimpleNamespace(llm_api_key=""))
        assert _resolve_env("${ENV:LLM_API_KEY}") == ""

    def test_non_whitelisted_var_left_unresolved(self, monkeypatch):
        monkeypatch.delenv("MYSQL_URL", raising=False)
        assert _resolve_env("${ENV:MYSQL_URL}") == "${ENV:MYSQL_URL}"

    def test_whitelisted_docparse_vars_accepted(self, monkeypatch):
        for var in _DOCPARSE_VARS:
            monkeypatch.setenv(var, f"val-{var}")
            assert _resolve_env(f"${{ENV:{var}}}") == f"val-{var}"
            monkeypatch.delenv(var)

    def test_whitelisted_but_unmapped_var_has_no_settings_fallback(self, monkeypatch):
        """PATH is whitelisted but not in the settings fallback map."""
        monkeypatch.delenv("PATH", raising=False)
        assert _resolve_env("${ENV:PATH}") == ""


class TestSettingsEnvFallback:
    def test_mapped_vars_read_settings_attrs(self, monkeypatch):
        settings = SimpleNamespace(
            llm_base_url="http://x/v1",
            llm_api_key="sk",
            llm_model="m",
            docparse_ocr_api_url="http://ocr",
            docparse_ocr_lang="ch",
            docparse_ocr_engine="ppstructure",
            docparse_ocr_max_image_long_side=1024,
        )
        monkeypatch.setattr("courtier.config.get_settings", lambda: settings)
        assert manager_mod._settings_env_fallback("LLM_IP") == "http://x/v1"
        assert manager_mod._settings_env_fallback("LLM_API_KEY") == "sk"
        assert manager_mod._settings_env_fallback("LLM_NAME") == "m"
        assert manager_mod._settings_env_fallback("DOCPARSE_OCR_API_URL") == "http://ocr"
        assert manager_mod._settings_env_fallback("DOCPARSE_OCR_LANG") == "ch"
        assert manager_mod._settings_env_fallback("DOCPARSE_OCR_ENGINE") == "ppstructure"
        # int coerced to str for subprocess env
        assert manager_mod._settings_env_fallback("DOCPARSE_OCR_MAX_IMAGE_LONG_SIDE") == "1024"

    def test_unmapped_var_returns_empty(self):
        assert manager_mod._settings_env_fallback("HOME") == ""
