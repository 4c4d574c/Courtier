from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from courtier.domain.loader import DomainConfig, DomainLoader
from courtier.prompts.engine import PromptBundle, PromptEngine

logger = logging.getLogger(__name__)


def _find_project_root() -> Path:
    """Walk upward from this file to locate the project root via sentinel files.

    Searches for pyproject.toml or CLAUDE.md as markers of the repo root.
    Falls back to the 5-level-parent heuristic if no sentinel is found.
    """
    current = Path(__file__).resolve().parent
    for _ in range(10):  # Max 10 levels up
        if (current / "pyproject.toml").exists() or (current / "CLAUDE.md").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback to the original heuristic
    return Path(__file__).resolve().parent.parent


_PROJECT_ROOT = _find_project_root()
_ENV_FILE = str(_PROJECT_ROOT / ".env")


def _default_project_root() -> Path:
    """Return COURTIER_REPO_ROOT if set, otherwise the auto-detected project root.

    This ensures containerized/production deployments default to the explicit
    repo root rather than the site-packages location of the installed wheel.
    """
    env_root = os.getenv("COURTIER_REPO_ROOT", "").strip()
    return Path(env_root) if env_root else _PROJECT_ROOT


def get_settings() -> "Settings":
    """Return the module-level Settings singleton, initialized eagerly at import time."""
    return _settings


class EventBusConfig(BaseModel):
    """In-process event bus configuration."""

    enabled: bool = True
    backpressure: Literal["drop_oldest", "drop_newest", "block"] = "drop_oldest"
    default_maxsize: int = 1000


class ModelRoutingConfig(BaseModel):
    """Model backend routing and fallback configuration."""

    strategy: Literal["primary", "cost", "quality", "ab"] = "primary"
    fallback_backends: list[str] = Field(default_factory=list)
    cost_threshold_chars: int | None = None
    ab_split: float = 0.5


_DEFAULT_MEMORY_LAYERS: list[Literal["working", "session", "long_term", "retrieval"]] = [
    "working",
    "session",
    "long_term",
    "retrieval",
]


class MemoryConfig(BaseModel):
    """Memory hierarchy configuration."""

    enabled_layers: list[Literal["working", "session", "long_term", "retrieval"]] = Field(
        default_factory=lambda: list(_DEFAULT_MEMORY_LAYERS)
    )
    long_term_summarize_after_turns: int = 10


class GuardrailsConfig(BaseModel):
    """Layered guardrail mode configuration."""

    input_layer: Literal["allow", "log", "block", "off"] = "log"
    output_layer: Literal["allow", "log", "block", "off"] = "log"
    tool_layer: Literal["allow", "log", "block", "off"] = "block"
    post_tool_layer: Literal["allow", "log", "block", "off"] = "log"


class ConversationTreeConfig(BaseModel):
    """Conversation tree branching configuration."""

    enabled: bool = True
    max_branches: int = 5
    snapshot_interval_turns: int = 5


class AgentRuntimeConfig(BaseModel):
    """Nested agent runtime configuration matching the migration plan appendix C."""

    events: EventBusConfig = Field(default_factory=EventBusConfig)
    model: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    conversation_tree: ConversationTreeConfig = Field(default_factory=ConversationTreeConfig)


class Settings(BaseSettings):
    """API 配置，从环境变量或 .env 文件读取。

    Environment variable aliases for LLM fields:
      - LLM_IP          → llm_base_url  (LLM API endpoint URL)
      - LLM_NAME        → llm_model     (model name, e.g. qwen3.6-27b)
      - LLM_TEMPERATURE → llm_temperature
      - LLM_MAX_TOKENS  → llm_max_tokens
      - LLM_EXTRA_BODY  → llm_extra_body (JSON string or null)
      - LOGGER_LEVEL    → logger_level
    """

    mysql_url: str = Field(
        default="", description="MySQL 连接 URL，必须通过环境变量 MYSQL_URL 设置"
    )
    upload_dir: str = str(_default_project_root() / "uploads")

    llm_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="llm_ip",
        description="LLM API 端点 URL（环境变量: LLM_IP）",
    )
    llm_api_key: str = ""
    llm_model: str = Field(
        default="qwen3.6-27b",
        alias="llm_name",
        description="模型名称（环境变量: LLM_NAME）",
    )
    llm_temperature: float = Field(default=0.0, alias="llm_temperature")
    llm_max_tokens: int = Field(default=4096, alias="llm_max_tokens")
    llm_timeout: float = Field(
        default=180.0,
        alias="llm_timeout",
        description="LLM 请求超时时间（秒，环境变量: LLM_TIMEOUT）",
    )
    llm_frequency_penalty: float = Field(
        default=0.0,
        alias="llm_frequency_penalty",
        description="频率惩罚，减少重复（环境变量: LLM_FREQUENCY_PENALTY）。temperature=0 时设 0。",
    )
    llm_presence_penalty: float = Field(
        default=0.0,
        alias="llm_presence_penalty",
        description=(
            "存在惩罚，鼓励新话题（环境变量: LLM_PRESENCE_PENALTY）。" "temperature=0 时设 0。"
        ),
    )
    llm_extra_body: dict[str, Any] | None = Field(
        default=None,
        alias="llm_extra_body",
        description="额外的请求体参数，JSON 字符串或 null（环境变量: LLM_EXTRA_BODY）",
    )

    # -- Embedding model (hybrid search) --
    llm_embedding_model: str = Field(
        default="",
        alias="llm_embedding_model",
        description=(
            "Embedding 模型名（环境变量: LLM_EMBEDDING_MODEL）。"
            "空 = 向量检索关闭，搜索与入库均退化为纯词法；"
            "host 同时用它计算检索时的查询向量（注入 search 工具）。"
        ),
    )
    llm_embedding_dim: int = Field(
        default=1024,
        alias="llm_embedding_dim",
        description="Embedding 向量维度（环境变量: LLM_EMBEDDING_DIM）",
    )
    llm_embedding_batch_size: int = Field(
        default=25,
        alias="llm_embedding_batch_size",
        description="单次 embedding 请求的文本批量大小（环境变量: LLM_EMBEDDING_BATCH_SIZE）",
    )

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def parse_llm_extra_body(cls, v: Any) -> dict[str, Any] | None:
        """Parse llm_extra_body from JSON string (env vars always pass as str)."""
        if v is None or v == "":
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"llm_extra_body must be a valid JSON string, got: {v!r}")
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"llm_extra_body must be a JSON object, got: {type(parsed).__name__}"
                )
            return parsed
        raise ValueError(
            f"llm_extra_body must be a dict, JSON string, or null, got: {type(v).__name__}"
        )

    # -- Context budget (token-based, model-aware) --
    llm_context_window_tokens: int = Field(
        default=32768,
        alias="llm_context_window_tokens",
        description="部署模型的上下文窗口 token 数（环境变量: LLM_CONTEXT_WINDOW_TOKENS）",
    )
    context_budget_ratio: float = Field(
        default=0.75,
        alias="context_budget_ratio",
        description="触发全量压缩的窗口占比（环境变量: CONTEXT_BUDGET_RATIO）",
    )
    context_micro_compact_ratio: float = Field(
        default=0.60,
        alias="context_micro_compact_ratio",
        description="启用微压缩的窗口占比（环境变量: CONTEXT_MICRO_COMPACT_RATIO）",
    )
    context_compact_target_ratio: float = Field(
        default=0.50,
        alias="context_compact_target_ratio",
        description="压缩后的目标占比（环境变量: CONTEXT_COMPACT_TARGET_RATIO）",
    )
    context_recent_tool_results_tokens: int = Field(
        default=4000,
        alias="context_recent_tool_results_tokens",
        description=(
            "微压缩保留最近工具结果的 token 预算" "（环境变量: CONTEXT_RECENT_TOOL_RESULTS_TOKENS）"
        ),
    )
    context_preview_max_chars: int = Field(
        default=1000,
        alias="context_preview_max_chars",
        description="大结果落盘时的预览长度（环境变量: CONTEXT_PREVIEW_MAX_CHARS）",
    )
    context_max_user_message_chars: int = Field(
        default=10000,
        alias="context_max_user_message_chars",
        description="大用户输入 persist 治理阈值（环境变量: CONTEXT_MAX_USER_MESSAGE_CHARS）",
    )

    docparse_ocr_api_url: str = Field(default="", alias="docparse_ocr_api_url")
    anydoc_ocr_api_url: str = Field(default="", alias="anydoc_ocr_api_url")
    docparse_ocr_lang: str = Field(default="ch", alias="docparse_ocr_lang")
    docparse_ocr_engine: str = Field(default="ppstructure", alias="docparse_ocr_engine")
    docparse_ocr_max_image_long_side: int = Field(
        default=2048, alias="docparse_ocr_max_image_long_side"
    )
    docparse_ocr_deskew: bool = Field(default=False, alias="docparse_ocr_deskew")
    docparse_max_llm_concurrent: int = Field(default=4, alias="docparse_max_llm_concurrent")
    docparse_max_ocr_concurrent: int = Field(default=10, alias="docparse_max_ocr_concurrent")
    docparse_llm_image_max_long_side: int = Field(
        default=1280, alias="docparse_llm_image_max_long_side"
    )
    docparse_classify_mode: str = Field(default="llm", alias="docparse_classify_mode")

    # 字体识别模型（自训练 ResNet）：配置后扫描件字体识别优先走模型，
    # 未识别/低置信行回退 LLM。
    font_model_url: str = Field(default="", alias="font_model_url")
    font_model_conf_threshold: float = Field(default=0.6, alias="font_model_conf_threshold")
    font_model_margin_threshold: float = Field(default=0.15, alias="font_model_margin_threshold")

    cec_api_base: str = ""
    cec_api_key: str = ""
    cec_model_name: str = "ChineseErrorCorrector3-4B"
    cec_max_length: int = Field(default=16383, alias="cec_max_length")
    cec_allowed_patterns: str = Field(
        default="看一看,想一想,试一试,人人,一一", alias="cec_allowed_patterns"
    )

    minio_endpoint: str = Field(default="", description="MinIO 服务端点，如 localhost:9000")
    minio_access_key: str = Field(default="", description="MinIO access key")
    minio_secret_key: str = Field(default="", description="MinIO secret key")
    minio_secure: bool = Field(default=False, description="MinIO 是否使用 HTTPS")
    cache_dir: str = str(_default_project_root() / "uploads" / ".cache")
    minio_bucket_docs: str = "courtier-docs"
    minio_bucket_library: str = "courtier-library"
    minio_bucket_resources: str = "courtier-resources"
    minio_bucket_documents: str = "courtier-documents"
    minio_bucket_plugin_io: str = Field(
        default="courtier-plugin-io",
        description="插件文件中转 bucket（环境变量: MINIO_BUCKET_PLUGIN_IO，24h 生命周期）",
    )

    # -- Plugin system (standalone TCP plugins, host dials out) --
    courtier_plugin_endpoints: str = Field(
        default="",
        description=(
            "插件端点映射（环境变量: COURTIER_PLUGIN_ENDPOINTS），形如 "
            "'parse=127.0.0.1:9101,anydoc=127.0.0.1:9102'；扫描到的每个插件都必须有端点"
        ),
    )
    courtier_plugin_token: str = Field(
        default="",
        description="主进程与插件之间的共享鉴权 token（环境变量: COURTIER_PLUGIN_TOKEN）",
    )

    def plugin_endpoints(self) -> dict[str, tuple[str, int]]:
        """Parse COURTIER_PLUGIN_ENDPOINTS into {name: (host, port)}.

        Raises ValueError on malformed entries so misconfiguration fails
        loudly at startup instead of surfacing as silent BLOCKED plugins.
        """
        raw = self.courtier_plugin_endpoints.strip()
        if not raw:
            return {}
        endpoints: dict[str, tuple[str, int]] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            name, sep, addr = item.partition("=")
            host, sep2, port = addr.rpartition(":")
            if not sep or not sep2 or not name.strip() or not host.strip() or not port.isdigit():
                raise ValueError(
                    f"COURTIER_PLUGIN_ENDPOINTS 条目格式错误: {item!r}（应为 name=host:port）"
                )
            endpoints[name.strip()] = (host.strip(), int(port))
        return endpoints

    es_hosts: str = ""
    es_username: str = ""
    es_password: str = ""
    es_index_chunks: str = "courtier_chunks"
    es_index_results: str = "courtier_results"

    logger_level: str = Field(default="INFO", alias="logger_level")

    audit_log_enabled: bool = Field(
        default=True,
        alias="audit_log_enabled",
        description="是否启用结构化审计日志（记录 LLM prompts、reasoning、工具调用等）",
    )
    audit_log_dir: str = Field(
        default=str(_default_project_root() / ".agent_logs"),
        alias="audit_log_dir",
        description="审计日志保存目录",
    )

    # -- Background run management (task queue / reconnect replay) --
    run_log_max_events: int = Field(
        default=50_000,
        alias="run_log_max_events",
        description="单个 run 事件日志最大条数，超限按安全边界驱逐（环境变量: RUN_LOG_MAX_EVENTS）",
    )
    run_log_max_bytes: int = Field(
        default=8_388_608,
        alias="run_log_max_bytes",
        description="单个 run 事件日志最大字节数，默认 8MB（环境变量: RUN_LOG_MAX_BYTES）",
    )
    run_grace_seconds: int = Field(
        default=600,
        alias="run_grace_seconds",
        description=(
            "run 终态后事件日志的保留宽限期（秒），供迟到的 attach 拿到完整重放"
            "（环境变量: RUN_GRACE_SECONDS）"
        ),
    )
    max_runs_per_user: int = Field(
        default=3,
        alias="max_runs_per_user",
        description=(
            "每用户并发运行上限，超出进入 FIFO 排队；0 = 不限（环境变量: MAX_RUNS_PER_USER）"
        ),
    )
    max_total_runs: int = Field(
        default=20,
        alias="max_total_runs",
        description="全局并发兜底上限（所有用户合计），0 = 不限（环境变量: MAX_TOTAL_RUNS）",
    )

    # -- Host-side search rerank (listwise rerank of search tool hits) --
    search_rerank_fetch: int = Field(
        default=100,
        alias="search_rerank_fetch",
        description=(
            "search_documents 且 rerank=true 时，取前 N 个候选做 listwise 重排"
            "（环境变量: SEARCH_RERANK_FETCH）"
        ),
    )
    search_rerank_candidate_budget_chars: int = Field(
        default=24_000,
        alias="search_rerank_candidate_budget_chars",
        description=(
            "重排提示词中候选正文的总字符预算，按候选数均分并钳制在 240-800/条"
            "（环境变量: SEARCH_CANDIDATE_BUDGET_CHARS）"
        ),
    )

    otel_service_name: str = Field(
        default="courtier",
        alias="otel_service_name",
        description="OpenTelemetry service name",
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        alias="otel_exporter_otlp_endpoint",
        description="OTLP exporter endpoint",
    )
    otel_log_level: str = Field(
        default="INFO",
        alias="otel_log_level",
        description="OpenTelemetry log level (DEBUG enables console exporter)",
    )

    cors_origins: list[str] = Field(
        default=["http://localhost:5173"],
        description="允许的 CORS 来源列表，JSON 数组格式",
    )
    cors_allow_credentials: bool = Field(default=False, description="是否允许携带凭证的跨域请求")

    jwt_secret: str = Field(default="", description="JWT 签名密钥，生产环境必须设置为强随机字符串")
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    jwt_expire_seconds: int = Field(
        default=86400, description="JWT token 过期时间（秒），默认 24 小时"
    )
    jwt_access_expire_seconds: int = Field(
        default=900, description="Access token 过期时间（秒），默认 15 分钟"
    )
    bcrypt_rounds: int = Field(default=12, ge=4, le=14, description="bcrypt 哈希轮数")
    admin_user: str = Field(default="admin", description="管理员用户名")
    admin_password: str = Field(default="", description="管理员密码，生产环境必须设置")

    deployment_env: str = Field(
        default="development",
        alias="deployment_environment",
        description="部署环境: development / staging / production",
    )

    # Loop guard thresholds — tunable via environment variables
    loop_max_turns_without_business_artifacts: int = Field(
        default=8,
        description="连续无业务产出轮次上限，超过则强制终止",
    )
    loop_max_reasoning_dupe_steps: int = Field(
        default=3,
        description="推理步骤去重检测窗口大小",
    )
    loop_reasoning_similarity_threshold: float = Field(
        default=0.85,
        description="推理步骤相似度阈值，超过视为重复",
    )
    loop_max_null_tool_results: int = Field(
        default=5,
        description="连续空工具结果上限，超过则强制终止",
    )
    loop_max_same_tool_calls: int = Field(
        default=3,
        description="重复相同工具调用上限，超过则强制终止",
    )
    loop_max_consecutive_exploratory: int = Field(
        default=8,
        description="连续探索性工具调用上限，超过则强制终止",
    )

    # Sub-agent runtime budget — tunable via environment variables
    subagent_max_runtime_seconds: float = Field(
        default=300.0,
        description="单个子代理最大挂钟时间（秒），0=不限时",
    )
    subagent_max_cumulative_runtime_seconds: float = Field(
        default=600.0,
        description="同一根节点下所有子代理累计最大运行时间（秒）",
    )
    subagent_max_turns: int = Field(
        default=20,
        description="单个子代理最大思考轮数",
    )
    subagent_max_depth: int = Field(
        default=5,
        description="子代理最大嵌套深度",
    )
    subagent_max_total_spawns: int = Field(
        default=20,
        description="最大子代理派生总数",
    )

    agent_runtime: AgentRuntimeConfig = Field(
        default_factory=AgentRuntimeConfig,
        description=(
            "Agent runtime configuration (events, model routing, memory, "
            "guardrails, conversation tree)"
        ),
    )

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """In production, require JWT secret and admin password to be set."""
        if self.deployment_env not in ("staging", "production"):
            return self
        missing: list[str] = []
        if not self.jwt_secret:
            missing.append("JWT_SECRET")
        if not self.admin_password:
            missing.append("ADMIN_PASSWORD")
        if missing:
            raise ValueError(f"Production environment requires {', '.join(missing)} to be set.")
        return self

    model_config = {
        "env_file": _ENV_FILE,
        "extra": "ignore",
        "env_nested_delimiter": "__",
    }


@runtime_checkable
class DomainPackage(Protocol):
    """Runtime representation of a loaded domain package.

    Combines the validated domain.yaml metadata with the loaded
    PromptBundle and paths to plugins/skills.
    """

    name: str
    config: DomainConfig
    plugins_path: Path
    skills_path: Path
    prompt_bundle: PromptBundle


@dataclass
class _DomainPackageImpl:
    """Concrete implementation of DomainPackage protocol."""

    name: str
    config: DomainConfig
    plugins_path: Path
    skills_path: Path
    prompt_bundle: PromptBundle


@dataclass
class CourtierConfig:
    """Top-level platform configuration.

    Discovers and loads domain packages from the filesystem based on
    the COURTIER_DOMAIN_PACKAGES environment variable.
    """

    # Repository root — resolved from __file__ at startup
    repo_root: Path

    # Domain package names to load (e.g. ["docaudit"])
    domain_names: list[str] = field(default_factory=list)

    # True when COURTIER_DOMAIN_PACKAGES was explicitly set — then it acts
    # as an allowlist.  When unset, discover() scans all domain packages on
    # disk minus the names in domains/.disabled.
    explicit_domain_names: bool = False

    # Default locale
    locale: str = "zh-CN"

    # Loaded domain packages (populated by discover())
    _domains: list[_DomainPackageImpl] = field(default_factory=list, repr=False)

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "CourtierConfig":
        """Create CourtierConfig from environment variables.

        Reads:
        - COURTIER_REPO_ROOT: explicit repository root path (default: auto-detect)
        - COURTIER_DOMAIN_PACKAGES: comma-separated domain names (default: "docaudit")
        - COURTIER_LOCALE: locale string (default: "zh-CN")

        Args:
            repo_root: Repository root path. If None, auto-detected from the
                       domains/ directory relative to the caller, unless
                       COURTIER_REPO_ROOT is set.
        """
        env_root = os.getenv("COURTIER_REPO_ROOT", "").strip()
        if env_root:
            repo_root = Path(env_root)
        elif repo_root is None:
            # Try to auto-detect: look for domains/ relative to cwd
            cwd = Path.cwd()
            if (cwd / "domains").is_dir():
                repo_root = cwd
            else:
                repo_root = Path(".")

        raw_domains = os.getenv("COURTIER_DOMAIN_PACKAGES", "").strip()
        domain_names = [name.strip() for name in raw_domains.split(",") if name.strip()]
        locale = os.getenv("COURTIER_LOCALE", "zh-CN")

        return cls(
            repo_root=repo_root,
            domain_names=domain_names,
            explicit_domain_names=bool(raw_domains),
            locale=locale,
        )

    def _resolve_domain_names(self, domains_dir: Path) -> list[str]:
        """Resolve which domain packages to load.

        Explicit allowlist (COURTIER_DOMAIN_PACKAGES set) wins.  Otherwise
        scan every on-disk package (a dir with config/domain.yaml) minus the
        names in domains/.disabled.
        """
        if self.explicit_domain_names:
            return self.domain_names
        if not domains_dir.is_dir():
            return []
        from courtier.domain.disabled import read_disabled_domains

        disabled = read_disabled_domains(domains_dir)
        return [
            entry.name
            for entry in sorted(domains_dir.iterdir())
            if entry.is_dir()
            and (entry / "config" / "domain.yaml").is_file()
            and entry.name not in disabled
        ]

    def discover(self) -> list[DomainPackage]:
        """Discover and load all configured domain packages.

        Returns:
            List of loaded DomainPackage instances.
        Raises:
            FileNotFoundError: If an explicitly configured domain package does not exist.
        """
        domains_dir = self.repo_root / "domains"
        names = self._resolve_domain_names(domains_dir)
        if not domains_dir.is_dir():
            if not names:
                logger.warning("No domains directory found at %s", domains_dir)
                return []
            # Domains are configured but the directory doesn't exist —
            # fall through to raise FileNotFoundError for each domain.

        self._domains = []
        for name in names:
            domain_path = domains_dir / name
            if not domain_path.is_dir():
                raise FileNotFoundError(
                    f"Domain package '{name}' not found at {domain_path}. "
                    f"Configured domains: {self.domain_names}"
                )

            config = DomainLoader.load(domain_path)
            if config is None:
                raise ValueError(
                    f"Domain package '{name}' at {domain_path} has invalid or "
                    f"missing config/domain.yaml"
                )

            # Load prompt bundle for this domain
            prompt_engine = PromptEngine.from_domain_directories(
                domain_paths=[domain_path],
                locale=self.locale,
            )

            pkg = _DomainPackageImpl(
                name=name,
                config=config,
                plugins_path=self.repo_root / "plugins",
                skills_path=domain_path / "skills",
                prompt_bundle=prompt_engine._bundle,
            )
            self._domains.append(pkg)
            logger.info(
                "Loaded domain '%s': %s (locales: %s, plugins: %s)",
                name,
                config.title,
                config.locales,
                config.requires_plugins,
            )

        return self._domains  # type: ignore[return-value]

    @property
    def domains(self) -> list[DomainPackage]:
        """Return loaded domain packages (discover if not yet loaded)."""
        if not self._domains:
            self.discover()
        return self._domains  # type: ignore[return-value]

    def build_prompt_engine(self) -> PromptEngine:
        """Build a PromptEngine from all loaded domains.

        Domain prompt bundles are merged in order — later domains
        override template keys from earlier ones.

        Reads the *discovered* domain set (``self.domains``) rather than
        ``domain_names``: when COURTIER_DOMAIN_PACKAGES is unset,
        ``domain_names`` stays empty even after ``discover()`` has scanned
        the filesystem, which silently dropped every domain-contributed
        template key (e.g. orchestrator.workflow_rules) from the merged
        engine.
        """
        domain_paths = [self.repo_root / "domains" / pkg.name for pkg in self.domains]
        return PromptEngine.from_domain_directories(
            domain_paths=domain_paths,
            locale=self.locale,
        )


# Eagerly initialize the singleton at import time to avoid races.
_settings = Settings()
