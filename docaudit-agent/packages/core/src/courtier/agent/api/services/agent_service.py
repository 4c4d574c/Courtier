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
    """Create an OpenAIModelClient from Settings."""
    from courtier.agent.core.model import OpenAIModelClient

    return OpenAIModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens if settings.llm_max_tokens > 0 else None,
        extra_body=settings.llm_extra_body,
        frequency_penalty=settings.llm_frequency_penalty,
        presence_penalty=settings.llm_presence_penalty,
    )


async def build_audit_agent(
    settings: Any,
    plugin_system: Any = None,
    tool_registry: Any = None,
    cache_store: Any = None,
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
        try:
            courtier_cfg = CourtierConfig.from_env()
            courtier_cfg.discover()
            resolved_skills_dir = str(courtier_cfg.domains[0].skills_path)
        except Exception:
            resolved_skills_dir = "packages/domains/docaudit/skills"

    skill_registry = SkillRegistry(resolved_skills_dir)
    skill_registry.scan()
    if skill_registry.has_errors:
        logger.warning("Skill registry errors: %s", skill_registry.errors)

    # Build unified ArtifactStore (which now subsumes CacheStore).
    store = _build_artifact_store(settings, artifact_store or cache_store)

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
    """Build or augment an ArtifactStore (which now subsumes CacheStore).

    If *existing_store* is already an ArtifactStore, attach an optional ES
    primary backend and return it.  If it is a legacy CacheStore, wrap it.
    Otherwise create a new ArtifactStore from *settings.cache_dir*.
    """
    from courtier.agent.artifacts.store import ArtifactStore
    from courtier.agent.core.cache_store import CacheStore as _LegacyCacheStore

    if isinstance(existing_store, ArtifactStore):
        store = existing_store
    elif isinstance(existing_store, _LegacyCacheStore):
        # Legacy CacheStore — wrap it in an ArtifactStore by replacing its
        # internal backend.  This preserves any existing ref_map / ref_counters.
        store = ArtifactStore(cache_dir=str(settings.cache_dir))
        store._backend = existing_store
    elif existing_store is not None:
        store = existing_store  # Duck-typed — assume it has persist/read/etc.
    else:
        store = ArtifactStore(cache_dir=str(settings.cache_dir))

    if getattr(settings, "es_hosts", None):
        try:
            from ...runtime.es_backend import ElasticsearchResultBackend

            es_primary = ElasticsearchResultBackend(
                index_name=getattr(settings, "es_index_results", "agent_results"),
            )
            store._backend._primary_backend = es_primary
        except Exception as exc:
            logger.warning(
                "Failed to create Elasticsearch backend for ArtifactStore: %s", exc,
            )

    return store

async def build_chat_agent(
    settings: Any,
    cache_store: Any = None,
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
        artifact_store=artifact_store or cache_store,
    )
    return agent, context_manager, model.model_name
