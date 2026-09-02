"""PromptEngine — Jinja2-based prompt rendering with zero hardcoded text.

Template resolution is three-tier:
1. Domain package bundles (config/prompts/{locale}/*.yaml) — domain-specific
   templates (orchestrator.*, chat.system_prompt) plus optional overrides of
   any core default.
2. Core default bundles shipped with the platform
   (courtier/prompts/defaults/{locale}/*.yaml) — full localized text for all
   domain-agnostic keys (errors, context, behavioral, tools, subagent,
   chat.welcome_message).
3. FALLBACK_TEMPLATES — minimal en-US strings below, the last resort so the
   platform still boots when a domain-owned key has no template at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, Environment, TemplateError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Core default prompt bundles shipped with the platform (full localized text
# for every domain-agnostic key).  Loaded as the merge base in
# ``from_domain_directories`` so domain packages only carry domain-specific
# templates and optional overrides.
_CORE_DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
_CORE_DEFAULTS_FALLBACK_LOCALE = "en-US"

# Reserved template keys — the complete set of named slots that domain
# packages can fill.  No template outside this set is recognized by Core.
RESERVED_TEMPLATE_KEYS: frozenset[str] = frozenset(
    {
        "orchestrator.system_prompt",
        "orchestrator.task_decomposition",
        "orchestrator.workflow_rules",
        "subagent.system_prompt",
        "subagent.tool_call_reminder",
        "chat.system_prompt",
        "chat.welcome_message",
        "behavioral.thinking_directive",
        "behavioral.rules",
        "behavioral.pre_turn_reminder",
        "behavioral.periodic_reminder",
        "errors.tool_timeout",
        "errors.tool_arg_parse",
        "errors.tool_exception",
        "errors.tool_not_found",
        "errors.tool_max_calls_exceeded",
        "errors.tool_max_consecutive_exceeded",
        "errors.tool_missing_param",
        "errors.result_unknown_error",
        "errors.artifact_store_unavailable",
        "errors.permission_denied",
        "errors.internal_error",
        "errors.model_error",
        "errors.skill_input_validation",
        "errors.skill_missing_mode",
        "errors.skill_runtime_unavailable",
        "errors.skill_spawn_failed",
        "errors.skill_failed",
        "errors.skill_inline_missing_tools",
        "errors.subagent_unknown",
        "errors.subagent_timeout",
        "errors.subagent_terminated",
        "errors.subagent_budget_exhausted",
        "errors.subagent_spawn_refused",
        "errors.subagent_failed",
        "errors.budget_max_depth",
        "errors.budget_spawn_exhausted",
        "errors.budget_spawn_cycle",
        "errors.budget_child_allocate",
        "errors.subagent_max_steps",
        "errors.subagent_missing_registry",
        "errors.guard_null_tool_results",
        "errors.guard_repeated_calls",
        "errors.guard_consecutive_exploratory",
        "errors.guard_no_artifact_progress",
        "errors.guard_tool_blocked",
        "errors.guard_call_failed",
        "errors.confirmation_denied",
        "errors.confirmation_unavailable",
        "errors.plugin_arg_path_escape",
        "errors.plugin_arg_file_missing",
        "errors.plugin_file_transfer_failed",
        "errors.plugin_bad_response_type",
        "errors.plugin_missing_success_field",
        "errors.plugin_call_timeout",
        "errors.plugin_crashed",
        "errors.memory_unavailable",
        "errors.activate_domain_not_wired",
        "errors.binder_field_unresolved",
        "errors.binder_text_below_min",
        "errors.binder_text_above_max",
        "errors.binder_items_below_min",
        "errors.binder_items_above_max",
        "errors.binder_item_text_below_min",
        "errors.artifact_ref_not_found_hint",
        "errors.artifact_missing_id",
        "errors.artifact_projection_unsatisfied",
        "errors.artifact_data_not_found",
        "errors.artifact_outline_unsupported",
        "errors.artifact_section_not_found",
        "errors.artifact_outline_missing_params",
        "errors.artifact_typed_ref_not_found",
        "errors.artifact_read_failed",
        "errors.artifact_empty_data",
        "errors.artifact_id_not_found",
        "errors.artifact_hint_persisted_ref",
        "errors.artifact_hint_list_artifacts",
        "errors.artifact_projection_resolve_failed",
        "errors.artifact_projection_exec_failed_typed",
        "errors.artifact_projection_exec_failed",
        "ui.session_title",
        "ui.step_label",
        "ui.agent_name",
        "tools.invocation_rules",
        "search.rerank.system",
        "search.rerank.user",
        "context.compact_prompt",
        "context.compact_merge_prompt",
        "context.memory_recall_hint",
    }
)

# Minimal en-US last-resort templates.  Domain-agnostic keys are normally
# covered by the core default bundles (prompts/defaults/); these strings keep
# the platform booting when a template is missing everywhere — most notably
# domain-owned keys (orchestrator.*, chat.system_prompt) with no domain
# package installed.
FALLBACK_TEMPLATES: dict[str, str] = {
    "orchestrator.system_prompt": (
        "You are {{ agent_name }}, an intelligent task orchestration Agent.\n\n"
        "Your responsibilities:\n"
        "1. Analyze user tasks\n"
        "2. Select appropriate Skills to execute\n"
        "3. Coordinate multiple SubAgents (parallel/sequential)\n"
        "4. Aggregate and return results\n\n"
        "Available Skills:\n"
        "{% for skill in available_skills %}"
        "- **{{ skill.name }}**: {{ skill.description }}\n"
        "{% endfor %}\n"
    ),
    "orchestrator.task_decomposition": (
        "Decompose the task into steps:\n"
        "1. Understand {{ task }}\n"
        "2. Identify required skills\n"
        "3. Execute each step\n"
        "4. Return results\n"
    ),
    "orchestrator.workflow_rules": (
        "# Workflow Rules\n"
        "- Execute steps sequentially unless parallel is safe.\n"
        "- Report progress after each step.\n"
    ),
    "subagent.system_prompt": (
        "You are a SubAgent executing a specific task.\n"
        "Complete the task and return your results.\n"
    ),
    "subagent.tool_call_reminder": (
        "# Tool Call Reminder\n" "Call tools when needed. Do not describe plans in text.\n"
    ),
    "chat.system_prompt": (
        "You are {{ agent_name }}, an AI assistant.\n" "Provide helpful, accurate responses.\n"
    ),
    "chat.welcome_message": ("Welcome! I am {{ agent_name }}, how can I help?\n"),
    "behavioral.thinking_directive": (
        "# Thinking Directive\n"
        "- Be concise in your reasoning.\n"
        "- State conclusions directly.\n"
    ),
    "behavioral.rules": (
        "# Behavioral Rules\n"
        "- Keep final responses concise.\n"
        "- Do not fabricate information.\n"
        "- Never expose internal identifiers (tool/skill names, $ref, artifact IDs) "
        "in final output.\n"
    ),
    "behavioral.pre_turn_reminder": (
        "# Pre-Turn Reminder\n" "- Stay on task.\n" "- Use tools when appropriate.\n"
    ),
    "behavioral.periodic_reminder": (
        "# Periodic Reminder\n" "- Review progress.\n" "- Adjust approach if needed.\n"
    ),
    "errors.tool_timeout": ("Tool '{{ tool_name }}' timed out after {{ timeout_seconds }}s.\n"),
    "errors.tool_arg_parse": (
        "Failed to parse arguments for tool '{{ tool_name }}'. "
        "Raw arguments: {{ raw_arguments }}\n"
    ),
    "errors.tool_exception": ("Tool '{{ tool_name }}' raised an exception: {{ error }}\n"),
    "errors.tool_not_found": (
        "Tool '{{ tool_name }}' is not registered and cannot be called. "
        "Available tools: {{ available_tools }}. Use one of the available "
        "tools, or answer with text directly.\n"
    ),
    "errors.tool_max_calls_exceeded": (
        "Tool '{{ tool_name }}' has been called {{ count }} times, exceeding "
        "the limit of {{ limit }}.\n"
    ),
    "errors.tool_max_consecutive_exceeded": (
        "Tool '{{ tool_name }}' has been called {{ count }} consecutive "
        "times, exceeding the limit of {{ limit }}.\n"
    ),
    "errors.tool_missing_param": (
        "Tool '{{ tool_name }}' is missing required parameter "
        "'{{ param_name }}'{% if hint %} ({{ hint }}){% endif %}.\n"
    ),
    "errors.result_unknown_error": (
        "'{{ actor_name }}' failed without returning a specific error message.\n"
    ),
    "errors.artifact_store_unavailable": ("Artifact store is unavailable; cannot {{ action }}.\n"),
    "errors.permission_denied": (
        "Permission denied: the following tools were refused: {{ denied_tools }}.\n"
    ),
    "errors.internal_error": ("Internal error: {{ message }}.\n"),
    "errors.model_error": ("Model call failed: {{ error }}.\n"),
    "errors.skill_input_validation": (
        "Skill '{{ skill_name }}' received invalid input.\n"
        "{{ error_details }}\n"
        "Fix the listed fields and call the skill again."
    ),
    "errors.skill_missing_mode": (
        "Skill '{{ skill_name }}' requires a 'mode' parameter. Use "
        'mode="subagent" (complex task) or mode="inline" (simple task).\n'
    ),
    "errors.skill_runtime_unavailable": (
        "Skill '{{ skill_name }}' cannot run: AgentRuntime is unavailable.\n"
    ),
    "errors.skill_spawn_failed": ("Failed to spawn skill '{{ skill_name }}': {{ error }}\n"),
    "errors.skill_failed": ("Skill '{{ skill_name }}' failed: {{ error }}\n"),
    "errors.skill_inline_missing_tools": (
        "Inline mode cannot execute skill '{{ skill_name }}': missing "
        "required tools {{ missing_tools }}. Use mode='subagent' to run an "
        "isolated sub-agent, or fetch data via list_artifacts / get_artifact "
        "from the artifact store.\n"
    ),
    "errors.subagent_unknown": ("Unknown agent or skill: {{ agent_name }}\n"),
    "errors.subagent_timeout": ("Sub-agent timed out after {{ timeout_seconds }}s\n"),
    "errors.subagent_terminated": ("Sub-agent was terminated\n"),
    "errors.subagent_budget_exhausted": (
        "Cumulative runtime budget exhausted ({{ used_seconds }}s / " "{{ limit_seconds }}s)\n"
    ),
    "errors.subagent_spawn_refused": ("Cannot spawn sub-agent {{ agent_name }}: {{ reason }}\n"),
    "errors.budget_max_depth": ("maximum sub-agent nesting depth reached ({{ max_depth }})"),
    "errors.budget_spawn_exhausted": ("total spawn budget exhausted"),
    "errors.budget_spawn_cycle": (
        "spawn cycle detected: {{ agent_name }} (the same agent cannot spawn "
        "recursively along one chain)"
    ),
    "errors.budget_child_allocate": ("Cannot allocate child budget: {{ reason }}"),
    "errors.subagent_max_steps": (
        "Sub-agent hit the max-steps limit ({{ max_steps }} steps) without completing the task"
    ),
    "errors.subagent_missing_registry": (
        "Sub-agent has no tool registry and cannot execute tool calls"
    ),
    "errors.guard_null_tool_results": ("{{ count }} consecutive tool calls returned empty results"),
    "errors.guard_repeated_calls": (
        "{{ count }} repeated calls to tool {{ tool_name }} with identical arguments"
    ),
    "errors.guard_consecutive_exploratory": (
        "{{ count }} consecutive exploratory tool calls (list_artifacts/get_artifact) "
        "without business output"
    ),
    "errors.guard_no_artifact_progress": ("{{ turns }} turns without new business artifacts"),
    "errors.guard_tool_blocked": ("Tool {{ tool_name }} is blocked by policy: {{ cause }}"),
    "errors.guard_call_failed": (
        "Call guard {{ guard_name }} failed — the call was denied as a precaution"
    ),
    "errors.confirmation_denied": (
        "The user rejected the confirmation for tool {{ tool_name }}; the call was not "
        "executed. Adjust your approach or answer the user directly. (Note: {{ message }})"
    ),
    "errors.confirmation_unavailable": (
        "Tool {{ tool_name }} requires user confirmation, but no confirmation channel is "
        "available here — the call was not executed."
    ),
    "errors.subagent_failed": ("Sub-agent execution failed: {{ error }}\n"),
    "errors.plugin_arg_path_escape": (
        "Argument {{ param_name }} escapes the upload directory and was " "rejected: {{ value }}\n"
    ),
    "errors.plugin_arg_file_missing": (
        "File referenced by argument {{ param_name }} does not exist: {{ value }}\n"
    ),
    "errors.plugin_file_transfer_failed": (
        "Failed to transfer file to object storage: {{ error }}\n"
    ),
    "errors.plugin_bad_response_type": (
        "Plugin {{ plugin_name }} tool {{ tool_name }} returned an unexpected "
        "response type: {{ response_type }}\n"
    ),
    "errors.plugin_missing_success_field": (
        "Plugin {{ plugin_name }} tool {{ tool_name }} response is missing " "the 'success' field\n"
    ),
    "errors.plugin_call_timeout": (
        "Plugin '{{ plugin_name }}' call timed out ({{ timeout }}s); retry or " "split the task\n"
    ),
    "errors.plugin_crashed": ("Plugin '{{ plugin_name }}' crashed or disconnected\n"),
    "errors.memory_unavailable": (
        "Memory is unavailable (the current context manager does not support " "the memory tier).\n"
    ),
    "errors.activate_domain_not_wired": (
        "Activator not wired: activate_domain requires a DomainActivator "
        "bound at orchestrator build time\n"
    ),
    "errors.binder_field_unresolved": (
        "Could not resolve required field {{ field_name }} (type {{ artifact_type }})"
    ),
    "errors.binder_text_below_min": (
        "Field {{ field_name }}: text length {{ length }} is below minimum "
        "{{ min_chars }} characters"
    ),
    "errors.binder_text_above_max": (
        "Field {{ field_name }}: text length {{ length }} exceeds maximum "
        "{{ max_chars }} characters"
    ),
    "errors.binder_items_below_min": (
        "Field {{ field_name }}: {{ length }} items is below minimum {{ min_items }}"
    ),
    "errors.binder_items_above_max": (
        "Field {{ field_name }}: {{ length }} items exceeds maximum {{ max_items }}"
    ),
    "errors.binder_item_text_below_min": (
        "Field {{ field_name }}: item {{ index }} has {{ length }} chars, "
        "below minimum {{ item_min_chars }}"
    ),
    "errors.artifact_ref_not_found_hint": (
        "This reference does not exist or is outside the current task's "
        "visibility. Use a result_id actually returned by a tool in the "
        "current task chain, or call list_artifacts first to see available "
        "artifacts."
    ),
    "errors.artifact_missing_id": (
        "The 'id' parameter is required. Use the result_id field from tool "
        "output ($ref:...:N format), or the artifact_id field value returned "
        "by list_artifacts.\n"
    ),
    "errors.artifact_projection_unsatisfied": (
        "Artifact type {{ artifact_type }} cannot be projected to satisfy the "
        "request (materialize_as={{ materialize_as }}, constraints="
        "{{ constraints }}). Use a projectable target type with the "
        "artifact_type parameter (see projectable_to_types in list_artifacts "
        "output), or omit materialize_as/source_scope and read the raw data "
        "directly.\n"
    ),
    "errors.artifact_data_not_found": (
        "No artifact data found for id={{ id }}. The id parameter accepts: a "
        "result_id from tool output ($ref:...:N format), or an artifact_id "
        "field from list_artifacts output; add artifact_type when a type "
        "conversion is needed (see projectable_to_types).\n"
    ),
    "errors.artifact_outline_unsupported": (
        "Result {{ id }} does not support outline/section reading: only text "
        "results (Markdown/plain text) and parse_layout output qualify. "
        "Read the raw data via get_artifact directly, or add "
        "materialize_as=string to extract the body text.\n"
    ),
    "errors.artifact_section_not_found": (
        "Section '{{ section }}' not found. Available sections (index + "
        "title): {{ candidates }}{% if truncated %}…{% endif %}"
    ),
    "errors.artifact_outline_missing_params": (
        "Structured reading requires the outline or section parameter\n"
    ),
    "errors.artifact_typed_ref_not_found": (
        "No typed artifact found for reference {{ id }}; cannot run the "
        "requested projection. Omit materialize_as/source_scope and read the "
        "raw data directly, or call list_artifacts first to see available "
        "artifacts.\n"
    ),
    "errors.artifact_read_failed": ("Failed to read the persisted result: {{ error }}\n"),
    "errors.artifact_empty_data": ("Data of result {{ id }} is empty\n"),
    "errors.artifact_id_not_found": (
        "Artifact not found for artifact_id={{ artifact_id }}. {{ hint }}\n"
    ),
    "errors.artifact_hint_persisted_ref": (
        "(Hint: {{ artifact_id }} is a persisted reference — pass it directly "
        "as id to read the raw data; no need to call list_artifacts first.)"
    ),
    "errors.artifact_hint_list_artifacts": (
        "Call list_artifacts first to see available artifacts and their " "artifact_id."
    ),
    "errors.artifact_projection_resolve_failed": (
        "Cannot obtain the requested artifact: {{ messages }}{% if "
        "suggested_tools %}. Consider calling these upstream tools first: "
        "{{ suggested_tools }}{% endif %}"
    ),
    "errors.artifact_projection_exec_failed_typed": (
        "Projection failed: {{ error }}. Materializable artifact types: "
        "{{ available_types }}. If the source artifact type (e.g. "
        "docaudit.parsed_layout) is not directly materializable, request a "
        "projection target type (e.g. core.plain_text or "
        "docaudit.paragraph_list); the system runs the type-projection chain "
        "automatically.\n"
    ),
    "errors.artifact_projection_exec_failed": ("Projection failed: {{ error }}\n"),
    "ui.session_title": "{{ title }}",
    "ui.step_label": "Step {{ index }}: {{ label }}",
    "ui.agent_name": "{{ agent_name }}",
    "tools.invocation_rules": (
        "# Tool Invocation Rules\n"
        "1. Minimize tool calls.\n"
        "2. Call tools directly, don't describe plans in text.\n"
    ),
    "search.rerank.system": ("You are a search-result reranker. Output only a JSON array.\n"),
    "search.rerank.user": (
        "Rank the candidate chunks for the query by relevance and output the "
        "candidate numbers as a JSON array (most relevant first, numbering "
        "from 1, including every number).\n"
        "Query: {{ query }}\n"
        "Candidate chunks:\n"
        "{{ candidates }}\n"
        "Output only the JSON array, e.g. [3, 1, 2].\n"
    ),
    "context.compact_prompt": (
        "You are a context compaction assistant. Compress the conversation "
        "history below into a compact summary. You MUST preserve:\n"
        "1. Current task goal\n"
        "2. Completed key operations\n"
        "3. Files involved\n"
        "4. Key decisions and constraints\n"
        "5. Next concrete action\n"
        "6. All available $ref IDs and file paths\n\n"
        "Conversation history:\n{history}\n---\n"
        "Compress the history above. Output only the summary."
    ),
    "context.compact_merge_prompt": (
        "You are a context compaction assistant. Merge the NEW SEGMENT into "
        "the EXISTING SUMMARY and output the updated full summary. Preserve "
        "the same six information categories (including all $ref IDs and "
        "file paths).\n\n"
        "EXISTING SUMMARY:\n{previous_summary}\n\n"
        "NEW SEGMENT:\n{new_segment}\n---\n"
        "Merge and output the updated summary."
    ),
    "context.memory_recall_hint": (
        "[Recalled memory] Below are your memory workspace and current indexes:"
        "\n{memories}\nMemories are plain files in the memory workspace: "
        "use the read / write / edit tools; after writing or changing a "
        "memory, update its MEMORY.md index."
    ),
}


class PromptBundle(BaseModel):
    """A collection of named Jinja2 templates for a single locale.

    Loaded from config/prompts/{locale}/*.yaml in each domain package.
    Multiple bundles merge: later bundles override same-key templates.
    """

    locale: str = Field(description="Locale tag, e.g. 'zh-CN' or 'en-US'")
    templates: dict[str, str] = Field(
        default_factory=dict,
        description="Template key → Jinja2 template string",
    )

    @classmethod
    def from_directory(cls, path: Path, locale: str) -> "PromptBundle":
        """Load all YAML files in a locale directory into a PromptBundle.

        Each .yaml file's top-level keys become template keys.  Nested
        keys are flattened with dot-notation (e.g. orchestrator.system_prompt).
        """
        templates: dict[str, str] = {}
        if not path.is_dir():
            logger.warning("Prompt directory not found: %s", path)
            return cls(locale=locale, templates=templates)

        for yaml_file in sorted(path.glob("*.yaml")):
            try:
                raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                logger.error("Failed to parse %s: %s", yaml_file, exc)
                continue
            if isinstance(raw, dict):
                for key, value in _flatten_yaml_keys(raw):
                    if isinstance(value, str):
                        templates[key] = value
                        if key not in RESERVED_TEMPLATE_KEYS:
                            logger.warning(
                                "Template key '%s' is not in RESERVED_TEMPLATE_KEYS — "
                                "it will be ignored by the engine until registered",
                                key,
                            )

        return cls(locale=locale, templates=templates)

    def merge(self, other: "PromptBundle") -> "PromptBundle":
        """Return a new PromptBundle with other's templates layered on top."""
        merged = dict(self.templates)
        merged.update(other.templates)
        return PromptBundle(locale=self.locale, templates=merged)


def _flatten_yaml_keys(d: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested YAML dict into dot-notation key-value pairs."""
    result: list[tuple[str, str]] = []
    for key, value in d.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            result.append((full_key, value))
        elif isinstance(value, dict):
            result.extend(_flatten_yaml_keys(value, full_key))
        elif isinstance(value, list):
            # Join list items as newline-separated string
            joined = "\n".join(str(item) for item in value if isinstance(item, str))
            result.append((full_key, joined))
    return result


class PromptEngine:
    """Zero-hardcoded-text prompt renderer.

    Renders named templates from a PromptBundle using Jinja2.  Falls back
    to FALLBACK_TEMPLATES when a template key is not found in the bundle.
    """

    def __init__(self, bundle: PromptBundle | None = None) -> None:
        self._bundle = bundle or PromptBundle(locale="en-US")
        self._env = Environment(
            loader=BaseLoader(),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    @property
    def locale(self) -> str:
        return self._bundle.locale

    def render(self, template_name: str, **variables: Any) -> str:
        """Render a named template.

        Args:
            template_name: Dot-notation key (e.g. 'orchestrator.system_prompt').
            **variables: Jinja2 template variables.

        Returns:
            Rendered string.  Returns "" if the template is not found.
        """
        template_str = self._bundle.templates.get(template_name)
        if template_str is None:
            template_str = FALLBACK_TEMPLATES.get(template_name, "")
            if not template_str:
                logger.warning("Template '%s' not found in bundle or fallbacks", template_name)
                return ""
        try:
            template = self._env.from_string(template_str)
            return template.render(**variables)
        except TemplateError as exc:
            logger.error("Failed to render template '%s': %s", template_name, exc)
            return template_str  # Return raw template so errors are visible

    @classmethod
    def from_domain_directories(
        cls,
        domain_paths: list[Path],
        locale: str = "en-US",
    ) -> "PromptEngine":
        """Load and merge PromptBundles from core defaults + domain packages.

        The merge base is the core default bundle for *locale* (en-US when
        the requested locale has no core defaults).  Each domain then
        contributes config/prompts/{locale}/*.yaml on top; later domains
        override earlier ones.  A domain falls back to its first declared
        locale when the requested one is unavailable.  Keys left undefined
        everywhere resolve to FALLBACK_TEMPLATES at render time.
        """
        merged = _load_core_defaults(locale)
        for domain_path in domain_paths:
            prompts_dir = domain_path / "config" / "prompts" / locale
            used_locale = locale
            if not prompts_dir.is_dir():
                # Try the domain's first declared locale
                domain_yaml = domain_path / "config" / "domain.yaml"
                first_locale = _first_domain_locale(domain_yaml)
                if first_locale:
                    prompts_dir = domain_path / "config" / "prompts" / first_locale
                    used_locale = first_locale
                if not prompts_dir.is_dir():
                    logger.warning(
                        "No prompts found for domain %s (tried %s)",
                        domain_path.name,
                        locale,
                    )
                    continue
            bundle = PromptBundle.from_directory(prompts_dir, used_locale)
            merged = merged.merge(bundle)

        if not merged.templates:
            logger.warning(
                "from_domain_directories: no templates loaded from any domain "
                "or core defaults (paths: %s, locale: %s). "
                "Engine will use FALLBACK_TEMPLATES.",
                [str(p) for p in domain_paths],
                locale,
            )
        # Report the requested locale regardless of fallback sources,
        # matching the historical behavior of this factory.
        return cls(bundle=PromptBundle(locale=locale, templates=merged.templates))


def _load_core_defaults(locale: str) -> PromptBundle:
    """Load the core default bundle for *locale* (en-US fallback)."""
    defaults_dir = _CORE_DEFAULTS_DIR / locale
    used_locale = locale
    if not defaults_dir.is_dir():
        logger.warning(
            "No core default prompts for locale '%s'; falling back to '%s'",
            locale,
            _CORE_DEFAULTS_FALLBACK_LOCALE,
        )
        defaults_dir = _CORE_DEFAULTS_DIR / _CORE_DEFAULTS_FALLBACK_LOCALE
        used_locale = _CORE_DEFAULTS_FALLBACK_LOCALE
    return PromptBundle.from_directory(defaults_dir, used_locale)


def _first_domain_locale(domain_yaml_path: Path) -> str | None:
    """Read the first locale from a domain.yaml file."""
    if not domain_yaml_path.is_file():
        return None
    try:
        raw = yaml.safe_load(domain_yaml_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            locales = raw.get("locales", [])
            if locales:
                return str(locales[0])
    except (yaml.YAMLError, OSError):
        pass
    return None
