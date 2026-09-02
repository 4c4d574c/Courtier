"""Agent construction — build model client, audit agent, and chat agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from courtier.prompts.engine import PromptEngine

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Tools that read the resource library and must be scope-filtered per caller.
_SCOPE_SENSITIVE_TOOLS = ("search_documents", "read_chunks")


class UnknownModelError(LookupError):
    """A run requested a model id that the pool cannot resolve (unknown id
    or its endpoint is disabled) — the run route turns this into a 400."""


@dataclass(frozen=True)
class ModelProfile:
    """A resolved model-pool selection for one run.

    Carries everything needed to build a model client plus the context
    window override; ``api_key`` lives in memory only (never persisted or
    logged).  ``None`` profiles (empty pool) mean the scalar ``llm_*``
    settings drive the client."""

    model_id: str
    name: str
    base_url: str
    api_key: str
    model: str
    context_window_tokens: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    # Per-entry advanced overrides; None = the scalar llm_* default applies.
    timeout_seconds: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    extra_body: dict[str, Any] | None = None


def resolve_model_profile(
    settings: Any,
    *,
    model_id: str = "",
    session_last_model_id: str = "",
) -> ModelProfile | None:
    """Resolve the model pool pick for a run; ``None`` = scalar fallback.

    Precedence: explicit *model_id* (must resolve, else
    :class:`UnknownModelError`) → *session_last_model_id* (falls through to
    the pool default when the entry is gone/disabled) → pool default →
    ``None`` when the pool is empty or unresolvable (scalar ``llm_*``).
    """
    pool = getattr(settings, "llm_model_pool", None)
    endpoints = getattr(pool, "endpoints", None) or []
    if model_id and not endpoints:
        # An explicit pick must be validated even when the pool is empty —
        # silently ignoring it would run with a different model than asked.
        raise UnknownModelError(model_id)
    if not endpoints:
        return None

    lookup = {m.id: (ep, m) for ep in endpoints if ep.enabled for m in ep.models}
    picked: tuple[Any, Any] | None = None
    if model_id:
        picked = lookup.get(model_id)
        if picked is None:
            raise UnknownModelError(model_id)
    if picked is None and session_last_model_id:
        picked = lookup.get(session_last_model_id)
    if picked is None:
        picked = lookup.get(pool.default_model_id)
    if picked is None:
        logger.warning(
            "model pool configured but no enabled/default model resolves; "
            "falling back to scalar llm_* settings"
        )
        return None

    endpoint, entry = picked
    return ModelProfile(
        model_id=entry.id,
        name=entry.name,
        base_url=endpoint.base_url,
        api_key=getattr(settings, "llm_endpoint_keys", {}).get(endpoint.id, ""),
        model=entry.model,
        context_window_tokens=entry.context_window_tokens,
        max_tokens=entry.max_tokens,
        temperature=entry.temperature,
        timeout_seconds=getattr(entry, "timeout_seconds", None),
        frequency_penalty=getattr(entry, "frequency_penalty", None),
        presence_penalty=getattr(entry, "presence_penalty", None),
        extra_body=getattr(entry, "extra_body", None),
    )


def apply_owner_scope(agent: Any, owner_id: int | None) -> None:
    """Wrap scope-sensitive tools on *agent*'s private registry so resource
    library queries are filtered to the caller (public + own personal).

    The injected ``_owner_scope`` kwarg is host-side: it is not part of the
    tool's parameters schema and always wins over model-supplied arguments.
    Anonymous callers (owner_id=None) are scoped to public-only content.

    Any pre-existing ScopedTool chain is unwrapped first so repeated
    application replaces the injection instead of nesting wrappers (nested
    ScopedTools resolve innermost-wins, which would silently pin the first
    owner across sessions).
    """
    from ...tools.scoped import ScopedTool

    registry = getattr(agent, "tool_registry", None)
    if registry is None:
        return
    for name in _SCOPE_SENSITIVE_TOOLS:
        try:
            inner = registry.get(name)
        except KeyError:
            continue
        if isinstance(inner, ScopedTool):
            inner = inner.unwrapped
        registry.register(ScopedTool(inner, {"_owner_scope": owner_id}), force=True)


def _find_project_root() -> Path | None:
    """Walk upward from this file to locate the project root via a marker file."""
    start = Path(__file__).resolve().parent
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _load_courtier_md() -> str | None:
    """Load the project-level COURTIER.md from the repo root."""
    root = _find_project_root()
    if root is None:
        logger.warning("Cannot locate project root; COURTIER.md not loaded")
        return None
    courtier_md_path = root / "COURTIER.md"
    try:
        if courtier_md_path.exists():
            return courtier_md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to load COURTIER.md: %s", exc)
    return None


def build_model_client(settings: Any, profile: ModelProfile | None = None) -> Any:
    """Create the production model client from Settings.

    Without *profile*: builds the scalar ``llm_*`` client, wrapped in a
    ``ModelRouter`` when ``settings.agent_runtime.model`` configures
    fallback backends (same-endpoint clones).  With *profile* (a resolved
    pool entry): targets that endpoint/model — every scalar LLM parameter
    (temperature, max_tokens, timeout, penalties, extra_body) is overridden
    by the entry's explicit value when set, the scalar default otherwise;
    fallback routing is not applied (pool selection and fallback chains
    are separate mechanisms for now).
    """
    from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
    from courtier.agent.core.model import BackendModelClient

    def _backend(
        base_url: str,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
        extra_body: dict[str, Any] | None,
        frequency_penalty: float,
        presence_penalty: float,
    ) -> OpenAIModelBackend:
        return OpenAIModelBackend(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens if max_tokens > 0 else None,
            extra_body=extra_body,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
        )

    if profile is not None:
        temperature = (
            settings.llm_temperature if profile.temperature is None else profile.temperature
        )
        max_tokens = settings.llm_max_tokens if profile.max_tokens is None else profile.max_tokens
        return BackendModelClient(
            backend=_backend(
                profile.base_url,
                profile.api_key,
                profile.model,
                temperature,
                max_tokens,
                timeout=(
                    settings.llm_timeout
                    if profile.timeout_seconds is None
                    else profile.timeout_seconds
                ),
                extra_body=(
                    settings.llm_extra_body if profile.extra_body is None else profile.extra_body
                ),
                frequency_penalty=(
                    settings.llm_frequency_penalty
                    if profile.frequency_penalty is None
                    else profile.frequency_penalty
                ),
                presence_penalty=(
                    settings.llm_presence_penalty
                    if profile.presence_penalty is None
                    else profile.presence_penalty
                ),
            ),
            model=profile.model,
            temperature=temperature,
        )

    backend: Any = _backend(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_temperature,
        settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        extra_body=settings.llm_extra_body,
        frequency_penalty=settings.llm_frequency_penalty,
        presence_penalty=settings.llm_presence_penalty,
    )

    routing = getattr(getattr(settings, "agent_runtime", None), "model", None)
    fallback_names = list(getattr(routing, "fallback_backends", None) or [])
    if routing is not None and fallback_names:
        from courtier.agent.core.backends.router import ModelRouter, RoutingStrategy

        # Fallback entries share the scalar OpenAI-compatible endpoint; each
        # name selects the fallback model.  Pool profiles bypass routing
        # entirely — per-profile fallback chains are future work.
        backends = [backend] + [
            _backend(
                settings.llm_base_url,
                settings.llm_api_key,
                name,
                settings.llm_temperature,
                settings.llm_max_tokens,
                timeout=settings.llm_timeout,
                extra_body=settings.llm_extra_body,
                frequency_penalty=settings.llm_frequency_penalty,
                presence_penalty=settings.llm_presence_penalty,
            )
            for name in fallback_names
        ]
        backend = ModelRouter(
            backends,
            RoutingStrategy(
                name=routing.strategy,
                cost_threshold_chars=routing.cost_threshold_chars,
                ab_split=routing.ab_split,
            ),
        )

    return BackendModelClient(
        backend=backend,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )


async def build_agent(
    settings: Any,
    plugin_system: Any = None,
    tool_registry: "ToolRegistry | None" = None,
    courtier_config: Any = None,
    prompt_engine: PromptEngine | None = None,
    owner_id: int | None = None,
    session_id: str = "",
    shared_plugin_names: set[str] | None = None,
    active_domains: tuple[str, ...] = (),
    approved_tools: set[str] | None = None,
    model_profile: ModelProfile | None = None,
) -> tuple[Any, Any, str]:
    """Create the unified domain-gated orchestrator agent + MemoryManager.

    One builder for every session shape: chat-only, uploaded document, and
    multi-turn continuation.  The orchestrator starts with only the shared
    plugin tools visible (domain gating); domains self-activate at runtime
    via the ``activate_domain`` meta-tool, and the previously persisted
    ``active_domains`` set is silently replayed on per-request rebuilds —
    activation state must not be derived from history (compaction can drop
    the evidence).

    ``model_profile`` pins this run to a model-pool selection: the same
    client instance is shared by the orchestrator, subagents, and memory
    manager (the "chat chain follows the pick" invariant), and the
    profile's context window overrides the global one.

    Returns (agent, context_manager, model_name).
    """
    from courtier.config import CourtierConfig

    from ...agents.orch import OrchestratorAgent
    from ...core.capability import CapabilityRegistry
    from ...core.guardrails import (
        ConfirmationGuard,
        GuardrailSystem,
        PathPolicyGuard,
        ToolDisabledGuard,
    )
    from ...core.memory_manager import MemoryManager
    from ...runtime import AgentRuntime
    from ...runtime.activation import DomainActivator, build_domain_catalog
    from ...runtime.budget import AgentRuntimeBudget
    from ...tools.builtin.activate_domain import ActivateDomainTool

    model = build_model_client(settings, model_profile)

    courtier_md_content = _load_courtier_md()

    if courtier_config is None:
        courtier_config = CourtierConfig.from_env()
        courtier_config.discover()

    # Per-session shallow clone: each agent gets its own registry view so
    # owner-scope wrapping, SkillTool callback re-registration, and per-run
    # counters never leak across concurrent sessions sharing the app-wide
    # registry (tool instances are shared by reference; see
    # ToolRegistry.clone).  Admin/management endpoints keep reading the base
    # registry, which retains the raw plugin proxies.
    session_registry = tool_registry.clone() if tool_registry is not None else None

    # Build unified ArtifactStore (which now subsumes CacheStore).
    # Session-scoped: disk cache subdir and ES documents are keyed by
    # session_id, so ref numbering starts at 1 per session and sessions
    # never collide or see each other's persisted results.
    store = _build_artifact_store(settings, None, session_id=session_id)

    budget = AgentRuntimeBudget(
        max_runtime_seconds=settings.subagent_max_runtime_seconds,
        max_cumulative_runtime_seconds=settings.subagent_max_cumulative_runtime_seconds,
        max_turns=settings.subagent_max_turns,
        max_depth=settings.subagent_max_depth,
        remaining_total_spawns=settings.subagent_max_total_spawns,
    )
    # Full runtime over the session registry; skills are registered
    # per-domain by the activator (no upfront skill_registry).
    memory_home = Path(settings.cache_dir).parent / ".agent_memory"
    session_workspace = (
        Path(settings.cache_dir).parent / ".agent_sessions" / (session_id or "default")
    )
    # Session guardrail system: stateless permission guards shared by the
    # orchestrator and every spawned sub-agent (loop guards stay per-run —
    # agent_loop registers its own fresh instances). The capability registry
    # is shared too, so a meta["permission"] declaration steers the path
    # policy the same way for every agent in the session.
    capability_registry = CapabilityRegistry(tool_registry=session_registry)
    session_guardrails = GuardrailSystem(tool_mode="block", tool_call_mode="block")
    session_guardrails.register(ToolDisabledGuard())
    session_guardrails.register(
        PathPolicyGuard(
            allowed_roots=[memory_home, session_workspace],
            capability_registry=capability_registry,
        )
    )
    if getattr(settings, "tool_confirmation", None):
        session_guardrails.register(
            ConfirmationGuard(
                rules=settings.tool_confirmation,
                approved_tools=approved_tools,
            )
        )

    agent_runtime = AgentRuntime(
        tool_registry=session_registry,
        model=model,
        skill_registry=None,
        artifact_store=store,
        default_budget=budget,
        cache_dir=settings.cache_dir,
        prompt_engine=prompt_engine,
        guardrail_system=session_guardrails,
        capability_registry=capability_registry,
    )

    # Live catalog: domain descriptions + current skill list (mtime-cached),
    # so skills created in the admin UI are visible to the model immediately.
    domain_catalog = build_domain_catalog(courtier_config)
    activate_tool = ActivateDomainTool(domain_catalog)

    activator = DomainActivator(
        tool_registry=session_registry,
        courtier_config=courtier_config,
        agent_runtime=agent_runtime,
        plugin_system=plugin_system,
        prompt_engine=prompt_engine,
        shared_plugin_names=shared_plugin_names or set(),
    )

    agent = OrchestratorAgent(
        model=model,
        plugin_system=plugin_system,
        tool_registry=session_registry,
        guardrail_system=session_guardrails,
        skill_registry=None,
        agent_runtime=agent_runtime,
        courtier_md_content=courtier_md_content,
        prompt_engine=prompt_engine,
        agent_name="Courtier",
        # No first_required_tool: parsing choice is left to the LLM guided
        # by the activation payload (format audit → parse_layout,
        # content tasks → convert_document).
        extra_tools=[activate_tool],
        tool_filter=activator.visible,
    )
    activator.attach(agent)
    activate_tool.set_activator(activator)
    # Expose the activator on the agent so the stream runner can persist
    # the session's active-domain set after every run.
    agent._domain_activator = activator
    # Replay the persisted activation set (idempotent, silent).
    for domain in active_domains or ():
        await activator.activate(domain)

    context_manager = MemoryManager(
        model=model,
        cache_dir=settings.cache_dir,
        session_id=session_id or "default",
        artifact_store=store,
        **_context_budget_kwargs(settings, model_profile),
        **_compact_prompt_kwargs(prompt_engine),
        memory_auto_inject_enabled=bool(getattr(settings, "memory_auto_inject_enabled", True)),
        memory_auto_inject_max_chars=int(getattr(settings, "memory_auto_inject_max_chars", 400)),
        memory_auto_inject_total_chars=int(
            getattr(settings, "memory_auto_inject_total_chars", 1500)
        ),
        memory_recall_hint_template=_memory_recall_hint_template(prompt_engine),
    )
    # Seed the replayed activation set and keep future activations (the
    # activate_domain tool) feeding the recall injection.
    for domain in active_domains or ():
        context_manager.note_domain_active(domain)
    activator.on_domain_activated = context_manager.note_domain_active
    if plugin_system is not None:
        # Inject plugin-declared tool-usage guidance (plugin.yaml
        # system_prompt) into the system prompt via the lazy provider.
        agent.set_plugin_prompts_provider(plugin_system.get_system_prompts)
    apply_owner_scope(agent, owner_id)
    # The run's display name: the pool entry's human name when a profile
    # was selected, the scalar model name otherwise.
    model_name = model_profile.name if model_profile is not None else model.model_name
    return agent, context_manager, model_name


class _ContextBudgetKwargs(TypedDict):
    """Keyword arguments for ContextManager/MemoryManager budget wiring."""

    max_context_tokens: int
    micro_compact_tokens: int
    compact_target_tokens: int
    recent_tool_results_tokens: int
    preview_max_chars: int


class _CompactPromptKwargs(TypedDict, total=False):
    """Keyword arguments carrying the compaction prompt templates.

    Keys are absent entirely when no PromptEngine is available (bare test
    environments) — ContextManager then uses its protocol fallbacks.
    """

    compact_prompt_template: str | None
    compact_merge_prompt_template: str | None


def _context_budget_kwargs(
    settings: Any, profile: ModelProfile | None = None
) -> _ContextBudgetKwargs:
    """Derive ContextManager token budgets from settings.

    Budgets are ratios of the deployed model's context window: full
    compaction triggers at ``budget_ratio``, micro-compaction gates at
    ``micro_compact_ratio``, and compaction aims for ``target_ratio``.
    A pool *profile*'s ``context_window_tokens`` overrides the global
    window — models in the pool can differ by orders of magnitude."""
    window = int(getattr(settings, "llm_context_window_tokens", 32768))
    if profile is not None and profile.context_window_tokens:
        window = int(profile.context_window_tokens)
    return {
        "max_context_tokens": int(window * getattr(settings, "context_budget_ratio", 0.75)),
        "micro_compact_tokens": int(
            window * getattr(settings, "context_micro_compact_ratio", 0.60)
        ),
        "compact_target_tokens": int(
            window * getattr(settings, "context_compact_target_ratio", 0.50)
        ),
        "recent_tool_results_tokens": int(
            getattr(settings, "context_recent_tool_results_tokens", 4000)
        ),
        "preview_max_chars": int(getattr(settings, "context_preview_max_chars", 1000)),
    }


def _compact_prompt_kwargs(prompt_engine: PromptEngine | None) -> _CompactPromptKwargs:
    """Render the compaction prompt templates from the domain PromptBundle.

    The templates keep ``{history}`` / ``{previous_summary}`` /
    ``{new_segment}`` placeholders (single braces — not Jinja syntax) which
    ContextManager substitutes at compaction time.
    """
    if prompt_engine is None:
        return {}
    return {
        "compact_prompt_template": prompt_engine.render("context.compact_prompt") or None,
        "compact_merge_prompt_template": prompt_engine.render("context.compact_merge_prompt")
        or None,
    }


def _memory_recall_hint_template(prompt_engine: PromptEngine | None) -> str | None:
    """Render the memory-recall hint template from the PromptBundle.

    The template keeps a ``{memories}`` placeholder (single braces — not
    Jinja syntax) which MemoryManager substitutes at injection time.
    """
    if prompt_engine is None:
        return None
    return prompt_engine.render("context.memory_recall_hint") or None


def _build_artifact_store(settings: Any, existing_store: Any, session_id: str = "") -> Any:
    """Build an ArtifactStore (which now subsumes CacheStore).

    If *existing_store* is provided, return it unchanged.  Otherwise create a
    new ArtifactStore from *settings.cache_dir*, injecting an
    ElasticsearchResultBackend as the primary backend via the public
    constructor when ``settings.es_hosts`` is configured.  *session_id*
    scopes the store's disk cache directory and ES document namespace.
    """
    from courtier.agent.artifacts.store import ArtifactStore

    if existing_store is not None:
        # Duck-typed — assume it has persist/read/etc.
        return existing_store

    primary_backend = None
    if getattr(settings, "es_hosts", None):
        try:
            from ...runtime.es_backend import ElasticsearchResultBackend

            primary_backend = ElasticsearchResultBackend(
                index_name=settings.es_index_results,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create Elasticsearch backend for ArtifactStore: %s",
                exc,
            )

    return ArtifactStore(
        cache_dir=str(settings.cache_dir),
        preview_max_chars=int(getattr(settings, "context_preview_max_chars", 1000)),
        primary_backend=primary_backend,
        session_id=session_id,
    )
