"""Agent construction — build model client, audit agent, and chat agent."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from courtier.prompts.engine import PromptEngine

logger = logging.getLogger(__name__)


def _find_project_root() -> Path | None:
    """Walk upward from this file to locate the project root via a marker file."""
    start = Path(__file__).resolve().parent
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _load_drudge_md() -> str | None:
    """Load the project-level DRUDGE.md from the repo root."""
    root = _find_project_root()
    if root is None:
        logger.warning("Cannot locate project root; DRUDGE.md not loaded")
        return None
    drudge_md_path = root / "DRUDGE.md"
    try:
        if drudge_md_path.exists():
            return drudge_md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to load DRUDGE.md: %s", exc)
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
) -> tuple[Any, Any, str]:
    """Create OrchestratorAgent and ContextManager for document audit use."""
    from ...agents.orch import OrchestratorAgent
    from ...core.context_manager import ContextManager
    from ...runtime import AgentRuntime
    from ...runtime.budget import AgentRuntimeBudget
    from ...skills import SkillRegistry

    model = build_model_client(settings)

    drudge_md_content = _load_drudge_md()

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
    )

    agent = OrchestratorAgent(
        model=model,
        plugin_system=plugin_system,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        agent_runtime=agent_runtime,
        drudge_md_content=drudge_md_content,
        prompt_engine=prompt_engine,
        agent_name="Courtier",
    )
    context_manager = ContextManager(
        model=model,
        cache_dir=settings.cache_dir,
        artifact_store=store,
    )
    return agent, context_manager, model.model_name


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
                "Failed to create Elasticsearch backend for ArtifactStore: %s", exc,
            )

    return ArtifactStore(
        cache_dir=str(settings.cache_dir),
        primary_backend=primary_backend,
    )

async def build_chat_agent(
    settings: Any,
    artifact_store: Any = None,
    prompt_engine: PromptEngine | None = None,
) -> tuple[Any, Any, str]:
    """Create a simple chat Agent for text-only conversations (no file audit)."""
    from ...agents.base import Agent
    from ...core.context_manager import ContextManager

    model = build_model_client(settings)

    if prompt_engine is not None:
        chat_prompt = prompt_engine.render(
            "chat.system_prompt", agent_name="Courtier"
        )
        agent_name_val = "Courtier Assistant"
    else:
        chat_prompt = (
            "You are Courtier, an AI assistant. "
            "Provide helpful, accurate responses."
        )
        agent_name_val = "Courtier Assistant"

    agent = Agent(
        name=agent_name_val,
        role=chat_prompt,
        tools=[],
        model=model,
        prompt_engine=prompt_engine,
        agent_name=agent_name_val,
    )
    context_manager = ContextManager(
        model=model,
        cache_dir=settings.cache_dir,
        artifact_store=artifact_store,
    )
    return agent, context_manager, model.model_name
