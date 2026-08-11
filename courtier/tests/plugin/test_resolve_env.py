"""Tests for ${ENV:VAR} resolution in plugin manifests (_resolve_env)."""

from pathlib import Path
from types import SimpleNamespace

import yaml

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
    "DOCPARSE_OCR_DESKEW",
    "DOCPARSE_MAX_LLM_CONCURRENT",
    "DOCPARSE_MAX_OCR_CONCURRENT",
    "DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE",
    "DOCPARSE_CLASSIFY_MODE",
    "FONT_MODEL_URL",
    "FONT_MODEL_CONF_THRESHOLD",
    "FONT_MODEL_MARGIN_THRESHOLD",
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
            docparse_ocr_deskew=True,
            docparse_max_llm_concurrent=8,
            docparse_max_ocr_concurrent=16,
            docparse_llm_image_max_long_side=1280,
            docparse_classify_mode="rule_first",
            font_model_url="http://font:5000",
            font_model_conf_threshold=0.6,
            font_model_margin_threshold=0.15,
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
        # bool coerced to "True"/"False" — _env_flag parses "true" as enabled
        assert manager_mod._settings_env_fallback("DOCPARSE_OCR_DESKEW") == "True"
        assert manager_mod._settings_env_fallback("DOCPARSE_MAX_LLM_CONCURRENT") == "8"
        assert manager_mod._settings_env_fallback("DOCPARSE_MAX_OCR_CONCURRENT") == "16"
        assert manager_mod._settings_env_fallback("DOCPARSE_LLM_IMAGE_MAX_LONG_SIDE") == "1280"
        assert manager_mod._settings_env_fallback("DOCPARSE_CLASSIFY_MODE") == "rule_first"
        assert manager_mod._settings_env_fallback("FONT_MODEL_URL") == "http://font:5000"
        # float coerced to str for subprocess env
        assert manager_mod._settings_env_fallback("FONT_MODEL_CONF_THRESHOLD") == "0.6"
        assert manager_mod._settings_env_fallback("FONT_MODEL_MARGIN_THRESHOLD") == "0.15"

    def test_unmapped_var_returns_empty(self):
        assert manager_mod._settings_env_fallback("HOME") == ""


class TestShippedManifestEnvRefs:
    """防回归：已发布 plugin.yaml 里的 ${ENV:VAR} 引用必须全部落在白名单内。

    新增插件环境变量时只改 plugin.yaml 不够——忘记同步
    _ALLOWED_MANIFEST_ENV_VARS 会导致引用被静默拦截（插件收到未解析的
    字面量），本测试强制两者保持一致。
    """

    def test_shipped_manifests_reference_only_whitelisted_vars(self):
        plugins_root = Path(__file__).resolve().parents[2] / "plugins"
        manifests = sorted(plugins_root.rglob("plugin.yaml"))
        assert manifests, f"no plugin.yaml found under {plugins_root}"

        offenders: list[str] = []
        for manifest in manifests:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            env = (data.get("runtime") or {}).get("env") or {}
            for key, value in env.items():
                for var_name in manager_mod._ENV_REF_RE.findall(str(value)):
                    if var_name not in manager_mod._ALLOWED_MANIFEST_ENV_VARS:
                        offenders.append(f"{manifest}: {key} -> {var_name}")
        assert not offenders, "non-whitelisted manifest env refs: " + "; ".join(offenders)
