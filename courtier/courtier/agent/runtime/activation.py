"""DomainActivator — per-orchestrator domain self-activation (visibility gating).

Domain gating is a visibility filter: plugin subprocesses start up regardless
(they live in the app-wide shared registry), but an orchestrator agent only
sees the shared plugin set plus the tools of domains it has explicitly
activated through the ``activate_domain`` meta-tool.  Activation is additive
and idempotent: it registers the domain's skill tools on the agent's private
registry and overlays the domain's ``orchestrator.workflow_rules`` onto the
prompt pipeline.  The active set lives on the activator so it can be
persisted per-session and replayed when the agent is rebuilt per request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from courtier.agent.skills.registry import SkillRegistry
from courtier.prompts.engine import PromptEngine

if TYPE_CHECKING:
    from pathlib import Path

    from courtier.agent.agents.base import Agent
    from courtier.agent.runtime.runtime import AgentRuntime
    from courtier.config import CourtierConfig, DomainPackage

logger = logging.getLogger(__name__)

# Process-level cache of per-domain skill catalogs, keyed by skills dir path.
# Invalidated by the directory mtime so skills created/edited through the
# admin UI show up in the activate_domain catalog on the next request without
# re-scanning on every agent build.
_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}


def build_domain_catalog(courtier_config: "CourtierConfig") -> list[dict[str, Any]]:
    """Build the activate_domain catalog: every domain + its live skill list.

    Each entry: ``{"name", "description", "skills": [{"name", "description"}]}``.
    The skill list reflects the current skills/ directory (mtime-cached), so
    a skill created in the admin UI is visible to the model on the next
    request — no restart, no domain.yaml edit required.
    """
    catalog: list[dict[str, Any]] = []
    for pkg in courtier_config.domains:
        catalog.append(
            {
                "name": pkg.name,
                "description": getattr(pkg.config, "description", ""),
                "skills": _cached_skill_list(pkg.skills_path),
            }
        )
    return catalog


def _skills_dir_signature(skills_path: "Path") -> float:
    """Freshness signature for a skills dir: max mtime of dir + its *.md files.

    The admin update path rewrites skill files in place (write_text on an
    existing file), which bumps only the file's mtime, never the directory's.
    Keying the cache on the directory mtime alone therefore kept serving a
    stale catalog after any in-place edit or enabled toggle.
    """
    try:
        latest = skills_path.stat().st_mtime
    except OSError:
        return 0.0
    for md in skills_path.glob("*.md"):
        try:
            latest = max(latest, md.stat().st_mtime)
        except OSError:
            continue
    return latest


def _cached_skill_list(skills_path: "Path") -> list[dict[str, str]]:
    """Return [{name, description}] of enabled skills, freshness-cached."""
    mtime = _skills_dir_signature(skills_path)
    if mtime == 0.0:
        return []
    key = str(skills_path)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    registry = SkillRegistry(skills_path)
    registry.scan()
    if registry.has_errors:
        logger.warning("Skill catalog scan errors for %s: %s", skills_path, registry.errors)
    skills = [{"name": s.name, "description": s.description} for s in registry.list_enabled()]
    _CATALOG_CACHE[key] = (mtime, skills)
    return skills


@dataclass
class ActivationResult:
    """Outcome of a domain activation."""

    domain: str
    success: bool = True
    already_active: bool = False
    new_tools: list[str] = field(default_factory=list)
    new_skills: list[str] = field(default_factory=list)
    message: str = ""


class DomainActivator:
    """Activates domain packages on an orchestrator agent at runtime.

    Parameters
    ----------
    tool_registry : ToolRegistry
        App-wide shared registry (all plugin proxies, before gating).
    courtier_config : CourtierConfig
        Loaded domain packages (names/descriptions/skills paths/prompt bundles).
    agent_runtime : AgentRuntime
        Runtime that spawns skill sub-agents (holds the full tool registry).
    plugin_system : PluginSystem
        Supplies the plugin → domain mapping via ``plugin_domain``.
    prompt_engine : PromptEngine
        Merged engine, used as the fallback renderer for workflow rules.
    shared_plugin_names : set[str]
        Plugin names under ``plugins/shared/`` — always visible.
    agent : Agent | None
        Orchestrator agent; attach() must be called before activate().
    """

    def __init__(
        self,
        *,
        tool_registry: Any,
        courtier_config: "CourtierConfig",
        agent_runtime: "AgentRuntime",
        plugin_system: Any,
        prompt_engine: PromptEngine | None,
        shared_plugin_names: set[str],
        agent: "Agent | None" = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._courtier_config = courtier_config
        self._agent_runtime = agent_runtime
        self._plugin_system = plugin_system
        self._prompt_engine = prompt_engine
        self._shared_plugin_names = set(shared_plugin_names)
        self._agent = agent
        # Domains activated in this session (additive, never removed).
        self.active_domains: set[str] = set()
        # Per-domain SkillRegistries, scanned once and cached.
        self._skill_registries: dict[str, SkillRegistry] = {}

    # -- wiring --------------------------------------------------------------

    def attach(self, agent: "Agent") -> None:
        """Bind the orchestrator agent (needed to register skills and rules)."""
        self._agent = agent

    # -- visibility rule -----------------------------------------------------

    def visible(self, tool: Any) -> bool:
        """Visibility rule for the agent's ``tool_filter``.

        Tools without a plugin client (builtins, SkillTools) always pass;
        plugin tools pass when their plugin is in the shared set or its
        domain is active.  Reads ``active_domains`` live, so an activation
        inside one turn becomes visible on the next run() tool sync.
        """
        client = getattr(tool, "_client", None)
        plugin_name = getattr(client, "plugin_name", None)
        if plugin_name is None:
            return True
        if plugin_name in self._shared_plugin_names:
            return True
        return self.plugin_domain(plugin_name) in self.active_domains

    def plugin_domain(self, plugin_name: str) -> str | None:
        """Map a plugin name to its domain (None = shared plugin)."""
        if self._plugin_system is None:
            return None
        try:
            domain: str | None = self._plugin_system.plugin_domain(plugin_name)
            return domain
        except Exception:
            logger.warning("plugin_domain(%s) failed", plugin_name, exc_info=True)
            return None

    # -- activation ----------------------------------------------------------

    async def activate(self, domain: str) -> ActivationResult:
        """Activate *domain* on the attached agent (idempotent, additive).

        Validates the domain, scans its skills, registers them as runtime
        configs + SkillTools on the agent, overlays the domain's
        ``orchestrator.workflow_rules`` onto the prompt pipeline, and marks
        the domain active.  Plugin tools of the domain become visible via
        ``visible()`` on the next run() tool sync.
        """
        if self._agent is None:
            raise RuntimeError("DomainActivator not attached to an agent")

        pkg = self._domain_package(domain)
        if pkg is None:
            return ActivationResult(
                domain=domain,
                success=False,
                message=f"未知领域: {domain}。可用领域见 activate_domain 工具描述。",
            )

        if domain in self.active_domains:
            return ActivationResult(
                domain=domain,
                already_active=True,
                message=f"领域 {domain} 已激活，无需重复操作。",
            )

        registry = self._skill_registry(domain, pkg)

        # Register skill configs on the runtime (idempotent), then build
        # SkillTools on the agent's private registry so the orchestrator can
        # dispatch them as sub-agents.
        self._agent_runtime.register_skills(registry)
        new_skills: list[str] = []
        for skill in registry.list_enabled():
            from courtier.agent.tools.builtin.skill import SkillTool

            tool = SkillTool(
                skill=skill,
                runtime=self._agent_runtime,
                output_artifact_type=skill.output_artifact_type,
                prompt_engine=self._prompt_engine,
            )
            # Bind the current run's sub-agent streaming callback and root
            # budget handle: activation happens mid-run, AFTER the run()-start
            # callback sweep, so these tools would otherwise stream no
            # sub-agent events to the frontend and spawn without a root
            # budget anchor.
            tool.set_callbacks(
                on_subagent_event=getattr(self._agent, "_subagent_event_callback", None)
            )
            tool.set_parent_handle(getattr(self._agent, "_subagent_root_handle", None))
            self._agent.tool_registry.register(tool)
            new_skills.append(skill.name)

        # Overlay the domain's activation payload (workflow rules) onto the
        # rules section.  Rendered from the domain's own bundle so a missing
        # key falls back to core defaults / FALLBACK_TEMPLATES.
        rules_engine = PromptEngine(pkg.prompt_bundle)
        overlay = rules_engine.render("orchestrator.workflow_rules")
        if overlay:
            self._agent._prompt_pipeline.set_rules(overlay)
            # The run's system message is materialized once per run(); flag
            # the mutation so the loop refreshes messages[0] on the next
            # turn — otherwise mid-run activations stay invisible until the
            # next user message.
            self._agent.mark_system_prompt_dirty()

        self.active_domains.add(domain)
        # Surface the domain's plugin proxies on the agent immediately — the
        # per-run sync only picks up newly visible tools at the START of the
        # next run, so same-turn use after activation would otherwise fail.
        self._inject_domain_tools(domain)
        # Report callable tool names (plugin names like "parse" differ from
        # their tool names like "parse_document" — the model calls the
        # latter).
        new_tools = self._domain_plugin_tool_names(domain)
        logger.info(
            "Domain '%s' activated: tools=%s skills=%s",
            domain,
            new_tools,
            new_skills,
        )
        return ActivationResult(
            domain=domain,
            new_tools=new_tools,
            new_skills=new_skills,
            message=(
                f"领域 {domain} 已激活。"
                + (
                    f"新增技能（任务级工作流，调用即启动子代理执行）: {', '.join(new_skills)}。"
                    if new_skills
                    else ""
                )
                + (
                    f"新增工具（原子能力，可直接调用）: {', '.join(new_tools)}。"
                    if new_tools
                    else ""
                )
            ),
        )

    # -- helpers -------------------------------------------------------------

    def _domain_package(self, domain: str) -> "DomainPackage | None":
        for pkg in self._courtier_config.domains:
            if pkg.name == domain:
                return pkg
        return None

    def _skill_registry(self, domain: str, pkg: "DomainPackage") -> SkillRegistry:
        """Return the domain's SkillRegistry, scanning it once and caching."""
        cached = self._skill_registries.get(domain)
        if cached is not None:
            return cached
        registry = SkillRegistry(pkg.skills_path)
        registry.scan()
        if registry.has_errors:
            logger.warning("Domain '%s' skill registry errors: %s", domain, registry.errors)
        self._skill_registries[domain] = registry
        return registry

    def _inject_domain_tools(self, domain: str) -> None:
        """Register the domain's plugin proxies on the agent's registry now.

        Idempotent: tools already present are left untouched (hot-replacement
        stays owned by the run() sync loop).
        """
        if self._agent is None:  # attach() not called yet — nothing to inject
            return
        existing = {t.name for t in self._agent.tool_registry.list_tools()}
        for tool in self._tool_registry.list_tools():
            plugin_name = getattr(getattr(tool, "_client", None), "plugin_name", None)
            if plugin_name is None or tool.name in existing:
                continue
            if self.plugin_domain(plugin_name) == domain:
                self._agent.tool_registry.register(tool)

    def _domain_plugin_tool_names(self, domain: str) -> list[str]:
        """Callable tool names provided by *domain*'s plugins (from the
        shared registry, regardless of current agent visibility)."""
        return sorted(
            tool.name
            for tool in self._tool_registry.list_tools()
            if (pn := getattr(getattr(tool, "_client", None), "plugin_name", None))
            is not None
            and self.plugin_domain(pn) == domain
        )
