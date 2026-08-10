"""Agent construction — build model client, audit agent, and chat agent."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from courtier.prompts.engine import PromptEngine

logger = logging.getLogger(__name__)

# Tools that read the resource library and must be scope-filtered per caller.
_SCOPE_SENSITIVE_TOOLS = ("search_documents",)


def apply_owner_scope(agent: Any, owner_id: int | None) -> None:
    """Wrap scope-sensitive tools on *agent*'s private registry so resource
    library queries are filtered to the caller (public + own personal).

    The injected ``_owner_scope`` kwarg is host-side: it is not part of the
    tool's parameters schema and always wins over model-supplied arguments.
    Anonymous callers (owner_id=None) are scoped to public-only content.
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


def build_model_client(settings: Any) -> Any:
    """Create the production model client from Settings.

    Builds an ``OpenAIModelBackend`` and exposes it through the ``ModelClient``
    interface via ``BackendModelClient``.  When ``settings.agent_runtime.model``
    configures fallback backends, the backends are wrapped in a ``ModelRouter``
    with the configured routing strategy.
    """
    from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
    from courtier.agent.core.model import BackendModelClient

    def _openai_backend(model: str) -> OpenAIModelBackend:
        return OpenAIModelBackend(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens if settings.llm_max_tokens > 0 else None,
            extra_body=settings.llm_extra_body,
            frequency_penalty=settings.llm_frequency_penalty,
            presence_penalty=settings.llm_presence_penalty,
        )

    backend: Any = _openai_backend(settings.llm_model)

    routing = getattr(getattr(settings, "agent_runtime", None), "model", None)
    fallback_names = list(getattr(routing, "fallback_backends", None) or [])
    if routing is not None and fallback_names:
        from courtier.agent.core.backends.router import ModelRouter, RoutingStrategy

        # Fallback entries share the primary OpenAI-compatible endpoint; each
        # name selects the fallback model.  Per-backend endpoint settings do
        # not exist yet — add them to Settings when that need arises.
        backends = [backend] + [_openai_backend(name) for name in fallback_names]
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


async def build_audit_agent(
    settings: Any,
    plugin_system: Any = None,
    tool_registry: Any = None,
    artifact_store: Any = None,
    skills_dir: str | None = None,
    prompt_engine: PromptEngine | None = None,
    owner_id: int | None = None,
    session_id: str = "",
) -> tuple[Any, Any, str]:
    """Create OrchestratorAgent and MemoryManager for document audit use."""
    from ...agents.orch import OrchestratorAgent
    from ...core.memory_manager import MemoryManager
    from ...runtime import AgentRuntime
    from ...runtime.budget import AgentRuntimeBudget
    from ...skills import SkillRegistry

    model = build_model_client(settings)

    courtier_md_content = _load_courtier_md()

    # Resolve skills_dir from CourtierConfig's first domain, falling back to
    # the default path relative to repo root.
    from courtier.config import CourtierConfig

    if skills_dir:
        resolved_skills_dir = skills_dir
    else:
        courtier_cfg = CourtierConfig.from_env()
        courtier_cfg.discover()
        if not courtier_cfg.domains:
            raise ValueError(
                "No domain packages configured. Set COURTIER_DOMAIN_PACKAGES "
                "or pass an explicit skills_dir to build_audit_agent()."
            )
        resolved_skills_dir = str(courtier_cfg.domains[0].skills_path)

    skill_registry = SkillRegistry(resolved_skills_dir)
    skill_registry.scan()
    if skill_registry.has_errors:
        logger.warning("Skill registry errors: %s", skill_registry.errors)

    # Build unified ArtifactStore (which now subsumes CacheStore).
    store = _build_artifact_store(settings, artifact_store)

    budget = AgentRuntimeBudget(
        max_runtime_seconds=settings.subagent_max_runtime_seconds,
        max_cumulative_runtime_seconds=settings.subagent_max_cumulative_runtime_seconds,
        max_turns=settings.subagent_max_turns,
        max_depth=settings.subagent_max_depth,
        remaining_total_spawns=settings.subagent_max_total_spawns,
    )
    agent_runtime = AgentRuntime(
        tool_registry=tool_registry,
        model=model,
        skill_registry=skill_registry,
        artifact_store=store,
        default_budget=budget,
        cache_dir=settings.cache_dir,
    )

    agent = OrchestratorAgent(
        model=model,
        plugin_system=plugin_system,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        agent_runtime=agent_runtime,
        courtier_md_content=courtier_md_content,
        prompt_engine=prompt_engine,
        agent_name="Courtier",
        # An uploaded document must be parsed before anything else runs.
        first_required_tool="parse_document",
    )
    context_manager = MemoryManager(
        model=model,
        cache_dir=settings.cache_dir,
        session_id=session_id or "default",
        artifact_store=store,
        **_context_budget_kwargs(settings),
        **_compact_prompt_kwargs(prompt_engine),
    )
    if plugin_system is not None:
        # Inject plugin-declared tool-usage guidance (plugin.yaml
        # system_prompt) into the system prompt via the lazy provider.
        agent.set_plugin_prompts_provider(plugin_system.get_system_prompts)
    apply_owner_scope(agent, owner_id)
    return agent, context_manager, model.model_name


def _context_budget_kwargs(settings: Any) -> dict[str, int]:
    """Derive ContextManager token budgets from settings.

    Budgets are ratios of the deployed model's context window:
    full compaction triggers at ``budget_ratio``, micro-compaction gates at
    ``micro_compact_ratio``, and compaction aims for ``target_ratio``.
    """
    window = int(getattr(settings, "llm_context_window_tokens", 32768))
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


def _compact_prompt_kwargs(prompt_engine: PromptEngine | None) -> dict[str, str | None]:
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


def _build_artifact_store(settings: Any, existing_store: Any) -> Any:
    """Build an ArtifactStore (which now subsumes CacheStore).

    If *existing_store* is provided, return it unchanged.  Otherwise create a
    new ArtifactStore from *settings.cache_dir*, injecting an
    ElasticsearchResultBackend as the primary backend via the public
    constructor when ``settings.es_hosts`` is configured.
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
                index_name=getattr(settings, "es_index_results", "agent_results"),
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
    )


async def build_chat_agent(
    settings: Any,
    artifact_store: Any = None,
    prompt_engine: PromptEngine | None = None,
    tool_registry: Any = None,
    shared_plugin_names: set[str] | None = None,
    owner_id: int | None = None,
    session_id: str = "",
    plugin_system: Any = None,
) -> tuple[Any, Any, str]:
    """Create a chat Agent for conversations without an uploaded audit file.

    When *tool_registry* and *shared_plugin_names* are provided, tools from
    ``plugins/shared/`` (parse, search, annotate, template) are registered so
    the chat agent can search the resource library, etc.  Domain audit
    plugins remain audit-mode only.
    """
    from ...agents.base import Agent
    from ...core.memory_manager import MemoryManager

    model = build_model_client(settings)

    if prompt_engine is not None:
        chat_prompt = prompt_engine.render("chat.system_prompt", agent_name="Courtier")
        agent_name_val = "Courtier Assistant"
    else:
        chat_prompt = "You are Courtier, an AI assistant. " "Provide helpful, accurate responses."
        agent_name_val = "Courtier Assistant"

    tools: list[Any] = []
    if tool_registry is not None and shared_plugin_names:
        for tool in tool_registry.list_tools():
            # Plugin tools are ProxyTool instances whose JSON-RPC client
            # carries the originating plugin's name.
            plugin_name = getattr(getattr(tool, "_client", None), "plugin_name", None)
            if plugin_name in shared_plugin_names:
                tools.append(tool)

    agent = Agent(
        name=agent_name_val,
        role=chat_prompt,
        tools=tools,
        model=model,
        prompt_engine=prompt_engine,
        agent_name=agent_name_val,
    )
    context_manager = MemoryManager(
        model=model,
        cache_dir=settings.cache_dir,
        session_id=session_id or "default",
        artifact_store=artifact_store,
        **_context_budget_kwargs(settings),
        **_compact_prompt_kwargs(prompt_engine),
    )
    # Give the chat agent's private registry the same ResultSummarizer/persist
    # pipeline audit mode gets via AgentRuntime (runtime.py __post_init__),
    # so large tool results are persisted + summarised with a $ref instead of
    # being dumped verbatim into the LLM context.  The summarizer persists
    # into the same store the context manager uses, keeping $refs resolvable.
    from ...runtime.summarizer import ResultSummarizer

    agent.tool_registry.configure_result_handling(
        result_store=None,
        summarizer=ResultSummarizer(artifact_store=context_manager._cache),
    )
    if plugin_system is not None:
        # Shared plugins' declared tool-usage guidance (plugin.yaml
        # system_prompt) joins the system prompt via the lazy provider.
        agent.set_plugin_prompts_provider(plugin_system.get_system_prompts)
    apply_owner_scope(agent, owner_id)
    return agent, context_manager, model.model_name
