from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
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


def get_settings_encryption_key() -> str:
    """COURTIER_SETTINGS_KEY from the process env, falling back to the
    .env file — dev runs (uv run / python main.py) do not export .env
    into os.environ, only pydantic-settings reads it (Tier 0 vars are
    not Settings fields, so they need this dedicated path)."""
    raw = os.getenv("COURTIER_SETTINGS_KEY", "").strip()
    if raw:
        return raw
    try:
        for line in Path(_ENV_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("COURTIER_SETTINGS_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def get_config_service() -> "ConfigService":
    """Return the process-wide ConfigService (snapshot owner)."""
    return _config_service


def get_settings() -> "Settings":
    """Return the effective Settings snapshot owned by the ConfigService.

    The snapshot is built eagerly at import (same semantics as the old
    module-level singleton); later phases replace it as a whole on
    configuration changes, so every consumer sees one consistent view."""
    return _config_service.get()


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


class PoolModelConfig(BaseModel):
    """A selectable model entry inside a pool endpoint.

    The optional overrides shadow the scalar ``llm_*`` defaults; ``None``
    means "use the global default" — the scalars stay the single source
    for everything an entry does not explicitly set."""

    id: str
    name: str
    model: str
    # Declared input modalities the entry can natively consume (static admin
    # declaration — the single gating source for media attachments). Empty =
    # text-only. Values follow model-capability naming (vision/audio/video);
    # attachment kinds map onto these via agent_service.KIND_TO_MODALITY.
    modalities: list[Literal["vision", "audio", "video"]] = Field(default_factory=list)
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_seconds: float | None = Field(default=None, gt=0)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    extra_body: dict[str, Any] | None = None


class PoolEndpointConfig(BaseModel):
    """An OpenAI-compatible endpoint hosting one or more pool models."""

    id: str
    name: str
    base_url: str
    enabled: bool = True
    models: list[PoolModelConfig] = Field(default_factory=list)


class ToolConfirmationRule(BaseModel):
    """One entry of the tool confirmation list (settings key tool_confirmation)."""

    tool: str
    message: str = ""


class GuardDeclaration(BaseModel):
    """One entry of the guard declaration list (settings key guardrail_guards).

    ``builtin`` entries are seed-managed: the server forces their identity
    fields back to the seed definition on every save — only ``enabled`` is
    admin-controllable (disabling is allowed and audited).
    """

    name: str
    class_path: str
    scope: Literal["session", "run"] = "session"
    enabled: bool = True
    builtin: bool = False


def default_guard_declarations() -> list[GuardDeclaration]:
    """Field default for guardrail_guards: the five seeded baseline guards.

    Living on the model default (not only in the DB seed row) keeps the
    permission baseline present in env-only mode (no DB) and after an admin
    deletes the raw setting; an explicit DB value — including an emptied
    list — always wins over the default."""
    from courtier.agent.core.guardrails.registry import DEFAULT_GUARD_DECLARATIONS

    return [
        GuardDeclaration(
            name=d.name,
            class_path=d.class_path,
            scope=d.scope,
            enabled=d.enabled,
            builtin=d.builtin,
        )
        for d in DEFAULT_GUARD_DECLARATIONS
    ]


class ModelPoolConfig(BaseModel):
    """Two-level model pool (endpoints → models) plus the pool default.

    Stored as the ``llm_model_pool`` setting; api keys live separately in
    the secret ``llm_endpoint_keys`` map (endpoint id → key) so the pool
    body stays non-secret and editable in the admin UI.
    """

    endpoints: list[PoolEndpointConfig] = Field(default_factory=list)
    default_model_id: str = ""
    # 高级参数物化标记：True 表示所有条目的可空参数已写入全局默认值
    # （admin 保存时物化；本标记防止启动迁移重复执行覆盖后续的手工值）。
    advanced_defaults_materialized: bool = False

    @model_validator(mode="after")
    def _validate_references(self) -> "ModelPoolConfig":
        endpoint_ids = [ep.id for ep in self.endpoints]
        model_ids = [m.id for ep in self.endpoints for m in ep.models]
        duplicate_endpoints = sorted({i for i in endpoint_ids if endpoint_ids.count(i) > 1})
        if duplicate_endpoints:
            raise ValueError(f"duplicate endpoint ids: {duplicate_endpoints}")
        duplicate_models = sorted({i for i in model_ids if model_ids.count(i) > 1})
        if duplicate_models:
            raise ValueError(f"duplicate model ids: {duplicate_models}")
        if any(not i.strip() for i in endpoint_ids + model_ids):
            raise ValueError("pool ids must be non-empty")
        if self.default_model_id:
            for endpoint in self.endpoints:
                if any(m.id == self.default_model_id for m in endpoint.models):
                    if not endpoint.enabled:
                        raise ValueError(
                            f"default_model_id {self.default_model_id!r} "
                            "resolves to a disabled endpoint"
                        )
                    break
            else:
                raise ValueError(f"default_model_id {self.default_model_id!r} not found in pool")
        return self


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

    # —— 多模态附件（media_* 自动归 model 类设置）——
    media_max_images_per_message: int = Field(
        default=4, ge=0, description="每条消息允许的图片附件数量上限"
    )
    media_max_image_bytes: int = Field(
        default=20971520, ge=1, description="单个图片附件大小上限（字节，归一化前）"
    )
    media_max_audio_bytes: int = Field(
        default=26214400, ge=1, description="单个音频附件大小上限（字节）"
    )
    media_max_audio_seconds: float = Field(
        default=1800, gt=0, description="单个音频附件时长上限（秒）"
    )
    media_max_video_bytes: int = Field(
        default=52428800, ge=1, description="单个视频附件大小上限（字节）"
    )
    media_max_video_seconds: float = Field(
        default=300, gt=0, description="单个视频附件时长上限（秒）"
    )
    media_image_max_edge: int = Field(
        default=2048, ge=1, description="图片内联前归一化的长边上限（像素）"
    )
    media_image_jpeg_quality: int = Field(
        default=85, ge=1, le=100, description="图片归一化 JPEG 重编码质量"
    )
    media_image_token_estimate: int = Field(
        default=1024, ge=0, description="上下文记账中每张图片的 token 估算值"
    )
    media_audio_tokens_per_second: float = Field(
        default=40, ge=0, description="上下文记账中音频每秒 token 估算值"
    )
    media_video_tokens_per_second: float = Field(
        default=200, ge=0, description="上下文记账中视频每秒 token 估算值"
    )

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
    llm_embedding_base_url: str = Field(
        default="",
        alias="llm_embedding_base_url",
        description=(
            "Embedding API 端点 URL（环境变量: LLM_EMBEDDING_BASE_URL）。"
            "未设置 = 向量检索关闭，搜索与入库退化为纯词法；与聊天端点无关。"
        ),
    )
    llm_embedding_api_key: str = Field(
        default="",
        alias="llm_embedding_api_key",
        description=(
            "Embedding API 密钥（环境变量: LLM_EMBEDDING_API_KEY）。仅在端点需要鉴权时填写。"
        ),
    )

    # -- Tool confirmation (guards category) ---------------------------------
    tool_confirmation: list[ToolConfirmationRule] = Field(
        default_factory=list,
        alias="tool_confirmation",
        description=(
            "工具确认名单（JSON）：[{tool, message}]。名单内工具每次调用前挂起"
            "等待用户确认（仅本次 / 本会话放行 / 拒绝）；空名单 = 不需要确认。"
        ),
    )

    # -- 工具路径白名单（guards 类；工具自声明 + 管理后台覆盖） --------------
    tool_path_policies: dict[str, list[str] | bool] = Field(
        default_factory=dict,
        alias="tool_path_policies",
        description=(
            "工具路径白名单（JSON）：{工具名: [允许的路径, ...] 或 false}。"
            "列出的工具恰好只能读写这些路径（支持 ~）；false = 显式豁免；"
            "未列出的工具按代码内声明或基线。设置项优先于工具类内的代码声明。"
        ),
    )

    # -- 守卫声明（guards 类；新增守卫 = 写类 + 加一条声明，不改核心） -------
    guardrail_guards: list[GuardDeclaration] = Field(
        default_factory=default_guard_declarations,
        alias="guardrail_guards",
        description=(
            "守卫声明（JSON）：[{name, class_path, scope, enabled}]。scope="
            "session 注册进会话系统（编排器与子代理共享，须无状态）；run 每"
            "次 agent_loop 新实例（有状态守卫必须选它）；tool_call 层只允许 "
            "session。列表顺序即各 scope 内的检查顺序。builtin 条目为种子"
            "基线：身份字段服务端权威、仅 enabled 可改（可停用，审计留痕）。"
            "字段默认值即五个内置守卫；DB 值（含清空的列表）总是覆盖默认，"
            "首次启动另行播种为真实行供后台编辑。"
        ),
    )
    tools_disabled: list[str] = Field(
        default_factory=list,
        alias="tools_disabled",
        description=(
            '禁用工具名单（JSON 字符串数组，如 ["deploy"]）。名单内工具仍'
            "在模型工具表中，调用时收到模型可见的拒绝提示；空名单 = 无禁用。"
        ),
    )

    # -- Guardrail 分层模式（guards 类；构造会话 GuardrailSystem 时读取，
    #    热生效于下一次会话构建；默认值 = 历史装配行为） --------------------
    guardrail_input_layer: Literal["allow", "log", "block", "off"] = Field(
        default="block",
        alias="guardrail_input_layer",
        description=(
            "思考前（input）层模式：block=拦截生效；log=影子只记录；"
            "allow/off=跳过检查。当前该层无内置守卫。"
        ),
    )
    guardrail_output_layer: Literal["allow", "log", "block", "off"] = Field(
        default="log",
        alias="guardrail_output_layer",
        description=(
            "输出后（output）层模式：block=拦截生效；log=影子只记录不拦截；"
            "allow/off=跳过检查。当前该层无内置守卫，模式暂无效果（扩展预留）。"
        ),
    )
    guardrail_tool_layer: Literal["allow", "log", "block", "off"] = Field(
        default="block",
        alias="guardrail_tool_layer",
        description=(
            "工具批前（tool）层模式：block=拦截生效；log=影子只记录不拦截；"
            "allow/off=跳过检查。当前该层无内置守卫，模式暂无效果（扩展预留）。"
        ),
    )
    guardrail_tool_call_layer: Literal["block", "log"] = Field(
        default="block",
        alias="guardrail_tool_call_layer",
        description=(
            "逐调用（tool_call）层模式，仅限 block/log：该层承载工具禁用、"
            "路径白名单、执行确认三道权限拦截。log=影子只记录、调用照常执行"
            "（权限拦截全部失效），因此不提供放开档位。"
        ),
    )
    guardrail_post_tool_layer: Literal["allow", "log", "block", "off"] = Field(
        default="block",
        alias="guardrail_post_tool_layer",
        description=(
            "观察后（post_tool）层模式：block=强制收尾等拦截生效；"
            "log=影子只记录不拦截；allow/off=跳过检查。"
        ),
    )

    # -- Model pool (multi-model selection; the chat chain follows the pick) --
    llm_model_pool: ModelPoolConfig = Field(
        default_factory=ModelPoolConfig,
        alias="llm_model_pool",
        description=(
            "模型池（JSON）：endpoints（id/name/base_url/enabled/models）+ "
            "default_model_id。空池 = 回退 llm_base_url/llm_model 标量。"
        ),
    )
    llm_endpoint_keys: dict[str, str] = Field(
        default_factory=dict,
        alias="llm_endpoint_keys",
        description="模型池接入点的 API 密钥映射（JSON：endpoint id → key）。",
    )

    @field_validator("llm_endpoint_keys", mode="before")
    @classmethod
    def parse_llm_endpoint_keys(cls, v: Any) -> dict[str, str]:
        """Accept a dict or a JSON-object string — the secret round-trip
        (SettingsStore.save encrypts ``str(value)``) comes back as text."""
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"llm_endpoint_keys must be a valid JSON string, got: {v!r}")
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"llm_endpoint_keys must be a JSON object, got: {type(parsed).__name__}"
                )
            return parsed
        raise ValueError(
            f"llm_endpoint_keys must be a dict, JSON string, or null, got: {type(v).__name__}"
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

    @field_validator("guardrail_guards", mode="before")
    @classmethod
    def validate_guardrail_guards(cls, v: Any) -> list[dict[str, Any]]:
        """Normalize + deep-validate the guard declaration list.

        Structural parsing and class-level checks (importable, guard-shaped,
        legal layer, tool_call never run-scoped) run here so a bad
        declaration is rejected at save time instead of failing at run
        start. Builtin entries are server-authoritative: identity fields are
        forced back to the seed definition; only ``enabled`` passes through
        from the client."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"guardrail_guards must be a valid JSON string, got: {v!r}")
        if not isinstance(v, list):
            raise ValueError(f"guardrail_guards must be a list, got: {type(v).__name__}")
        from courtier.agent.core.guardrails.registry import (
            DEFAULT_GUARD_DECLARATIONS,
            GuardLoadError,
            check_guard_declaration,
            descriptor_from_raw,
        )

        seed_by_name = {d.name: d for d in DEFAULT_GUARD_DECLARATIONS}
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for raw in v:
            if isinstance(raw, BaseModel):  # defaults arrive as model instances
                raw = raw.model_dump()
            try:
                descriptor = descriptor_from_raw(raw)
            except GuardLoadError as exc:
                raise ValueError(str(exc)) from exc
            if descriptor.name in seen:
                raise ValueError(
                    f"guardrail_guards: duplicate declaration name {descriptor.name!r}"
                )
            seen.add(descriptor.name)
            seed = seed_by_name.get(descriptor.name)
            if seed is not None:
                items.append(
                    {
                        "name": seed.name,
                        "class_path": seed.class_path,
                        "scope": seed.scope,
                        "enabled": descriptor.enabled,
                        "builtin": True,
                    }
                )
            else:
                items.append(
                    {
                        "name": descriptor.name,
                        "class_path": descriptor.class_path,
                        "scope": descriptor.scope,
                        "enabled": descriptor.enabled,
                        "builtin": False,
                    }
                )
        for item in items:
            try:
                check_guard_declaration(item["class_path"], item["scope"])
            except GuardLoadError as exc:
                raise ValueError(f"guardrail_guards[{item['name']}]: {exc}") from exc
        return items

    @field_validator("tools_disabled", mode="before")
    @classmethod
    def validate_tools_disabled(cls, v: Any) -> list[str]:
        """Accept None/''/JSON string/list; keep non-empty unique names.

        No existence check against the tool registry: the deny list is
        blacklist semantics (an unknown name simply never matches) and tool
        availability varies with activated domains/plugins."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"tools_disabled must be a valid JSON string, got: {v!r}")
        if not isinstance(v, list):
            raise ValueError(f"tools_disabled must be a list, got: {type(v).__name__}")
        names: list[str] = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"tools_disabled entries must be non-empty strings, got: {item!r}")
            name = item.strip()
            if name not in names:
                names.append(name)
        return names

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

    # -- Memory tiers (MemoryManager) --
    memory_auto_inject_enabled: bool = Field(
        default=True,
        alias="memory_auto_inject_enabled",
        description="每轮用户输入后自动检索并注入相关记忆（环境变量: MEMORY_AUTO_INJECT_ENABLED）",
    )
    memory_auto_inject_total_chars: int = Field(
        default=1500,
        alias="memory_auto_inject_total_chars",
        description="自动注入 hint 的总字符上限（环境变量: MEMORY_AUTO_INJECT_TOTAL_CHARS）",
    )
    memory_auto_inject_max_chars: int = Field(
        default=400,
        alias="memory_auto_inject_max_chars",
        description="自动注入时单条记忆值的截断长度（环境变量: MEMORY_AUTO_INJECT_MAX_CHARS）",
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

    tool_timeout_seconds: float = Field(
        default=120.0,
        description="单次工具执行超时（秒），0=不限制；工具可用 execution_timeout 类属性覆盖",
    )

    # Refusal detection & same-model retry (model-behavior recovery policy).
    refusal_detection_enabled: bool = Field(
        default=True,
        description="是否对纯文本响应做拒绝（refusal）检测",
    )
    refusal_patterns: list[str] = Field(
        default_factory=lambda: [
            "我无法",
            "我不能",
            "无法协助",
            "无法帮助",
            "无法满足",
            "对不起，我不能",
            "i'm sorry, but",
            "i cannot",
            "i can't",
            "i'm not able",
        ],
        description="拒绝检测模式清单（不区分大小写的子串匹配），可按需增删",
    )
    refusal_retry_max: int = Field(
        default=1,
        ge=0,
        description="检测到拒绝后同模型重试次数上限（0=只检测不重试）",
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

    model_config = {
        "env_file": _ENV_FILE,
        "extra": "ignore",
        "env_nested_delimiter": "__",
        # snapshot composition merges by field name (model_dump keys);
        # init by field name must be accepted alongside env aliases.
        "populate_by_name": True,
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


class ConfigService:
    """Single owner of the effective Settings snapshot.

    Phase 0 (env-only): the snapshot is built eagerly at import — exactly
    the old module-level singleton semantics — and never changes, so
    get_settings(), ``app.state.settings`` and the lazy client helpers all
    read one consistent view.  Later phases (DB-backed settings) swap the
    snapshot as a whole via :meth:`replace`, bump *version*, and notify
    subscribers, which is how hot-reload groups propagate without any
    consumer holding a stale reference forever.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: "Settings | None" = None
        self._version: int = 0
        self._listeners: list[Callable[["Settings", int], None]] = []
        #: "env" until a DB snapshot composition succeeds, then "db".
        self.source: str = "env"

    def get(self) -> "Settings":
        """Return the effective snapshot, building it on first access."""
        with self._lock:
            if self._settings is None:
                self._settings = Settings()
                self._version = 1
            return self._settings

    @property
    def version(self) -> int:
        """Snapshot version; starts at 1 once built, +1 per replace()."""
        with self._lock:
            return self._version

    def replace(self, settings: "Settings") -> int:
        """Swap in a new snapshot and notify subscribers (returns version).

        Listeners run synchronously outside the lock, in subscription order;
        a failing listener is logged and does not block the others."""
        with self._lock:
            self._settings = settings
            self._version += 1
            version = self._version
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(settings, version)
            except Exception:
                logger.warning("config change listener failed", exc_info=True)
        return version

    def subscribe(self, listener: Callable[["Settings", int], None]) -> Callable[[], None]:
        """Register a change listener; returns an unsubscribe callable."""

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        with self._lock:
            self._listeners.append(listener)
        return _unsubscribe


_config_service = ConfigService()
# Eager init at import keeps the old singleton behavior: Settings
# validation runs once at import time, avoiding first-call races.
_config_service.get()


# ---------------------------------------------------------------------------
# Settings metadata — the DB-backed settings surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingMeta:
    """Presentation/persistence metadata for one Settings field.

    effect: "hot" applies from the next request; "rebuild" additionally
    recreates derived clients (ES/MinIO/plugin connections) on save;
    "restart" only takes effect after a container restart (UI labels it).
    """

    category: str
    is_secret: bool = False
    effect: str = "hot"


SETTING_CATEGORIES: dict[str, str] = {
    "model": "模型与上下文",
    "retrieval": "检索与存储",
    "plugins": "插件",
    "guards": "运行守卫与预算",
    "observability": "可观测性",
    "web": "Web 与安全",
}

#: Fields the DB must never override — bootstrap/topology concerns whose
#: values stay in the deployment layer (Tier 0).  admin_user/admin_password
#: leave with the Phase 3 setup wizard.
TIER0_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "mysql_url",
        "upload_dir",
        "cache_dir",
        "audit_log_dir",
        "deployment_env",
        "admin_user",
        "admin_password",
    }
)

_SECRET_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "llm_api_key",
        "llm_embedding_api_key",
        "llm_endpoint_keys",
        "minio_access_key",
        "minio_secret_key",
        "es_password",
        "courtier_plugin_token",
        "jwt_secret",
    }
)

_RESTART_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "cors_origins",
        "cors_allow_credentials",
        "otel_service_name",
        "otel_exporter_otlp_endpoint",
        "otel_log_level",
    }
)

_REBUILD_SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "es_hosts",
        "es_username",
        "es_password",
        "es_index_chunks",
        "es_index_results",
        "minio_endpoint",
        "minio_access_key",
        "minio_secret_key",
        "minio_secure",
        "minio_bucket_docs",
        "minio_bucket_library",
        "minio_bucket_resources",
        "minio_bucket_documents",
        "minio_bucket_plugin_io",
        "courtier_plugin_endpoints",
        "courtier_plugin_token",
    }
)


def _setting_category(field: str) -> str:
    if field.startswith(("llm_", "context_", "media_")):
        return "model"
    if field.startswith(("es_", "minio_", "search_rerank_")):
        return "retrieval"
    if field.startswith("courtier_plugin_"):
        return "plugins"
    if field.startswith(("loop_", "subagent_", "run_", "refusal_", "guardrail_")) or field in (
        "max_runs_per_user",
        "max_total_runs",
        "tool_confirmation",
        "tool_path_policies",
        "tools_disabled",
    ):
        return "guards"
    if field.startswith(("otel_", "audit_log_")) or field == "logger_level":
        return "observability"
    if field.startswith(("cors_", "jwt_")) or field == "bcrypt_rounds":
        return "web"
    return "advanced"


def build_settings_meta() -> dict[str, SettingMeta]:
    """Derive per-field metadata from the Settings model itself.

    Tier-0 fields and the nested agent_runtime block are excluded: they
    are not part of the DB-editable surface."""
    meta: dict[str, SettingMeta] = {}
    for name in Settings.model_fields:
        if name in TIER0_SETTING_FIELDS or name == "agent_runtime":
            continue
        if name in _RESTART_SETTING_FIELDS:
            effect = "restart"
        elif name in _REBUILD_SETTING_FIELDS:
            effect = "rebuild"
        else:
            effect = "hot"
        meta[name] = SettingMeta(
            category=_setting_category(name),
            is_secret=name in _SECRET_SETTING_FIELDS,
            effect=effect,
        )
    return meta


SETTINGS_META: dict[str, SettingMeta] = build_settings_meta()
