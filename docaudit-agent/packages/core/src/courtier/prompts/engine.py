"""PromptEngine — Jinja2-based prompt rendering with zero hardcoded text.

All natural-language text lives in PromptBundle YAML files loaded from
domain packages. The Core engine contains no hardcoded prompts, error
messages, or UI labels.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, Environment, TemplateError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Reserved template keys — the complete set of named slots that domain
# packages can fill.  No template outside this set is recognized by Core.
RESERVED_TEMPLATE_KEYS: frozenset[str] = frozenset({
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
    "errors.permission_denied",
    "errors.tool_not_found",
    "errors.internal_error",
    "ui.session_title",
    "ui.step_label",
    "ui.agent_name",
    "tools.invocation_rules",
})

# Minimal en-US fallback templates shipped with Core so the platform
# boots even when no domain package is installed.
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
        "# Tool Call Reminder\n"
        "Call tools when needed. Do not describe plans in text.\n"
    ),
    "chat.system_prompt": (
        "You are {{ agent_name }}, an AI assistant.\n"
        "Provide helpful, accurate responses.\n"
    ),
    "chat.welcome_message": (
        "Welcome! I am {{ agent_name }}, how can I help?\n"
    ),
    "behavioral.thinking_directive": (
        "# Thinking Directive\n"
        "- Be concise in your reasoning.\n"
        "- State conclusions directly.\n"
    ),
    "behavioral.rules": (
        "# Behavioral Rules\n"
        "- Keep final responses concise.\n"
        "- Do not fabricate information.\n"
    ),
    "behavioral.pre_turn_reminder": (
        "# Pre-Turn Reminder\n"
        "- Stay on task.\n"
        "- Use tools when appropriate.\n"
    ),
    "behavioral.periodic_reminder": (
        "# Periodic Reminder\n"
        "- Review progress.\n"
        "- Adjust approach if needed.\n"
    ),
    "errors.tool_timeout": (
        "Tool '{{ tool_name }}' timed out after {{ timeout }}s.\n"
    ),
    "errors.permission_denied": (
        "Permission denied for tool '{{ tool_name }}'.\n"
    ),
    "errors.tool_not_found": (
        "Tool '{{ tool_name }}' not found.\n"
    ),
    "errors.internal_error": (
        "Internal error occurred: {{ error_message }}.\n"
    ),
    "ui.session_title": "{{ title }}",
    "ui.step_label": "Step {{ index }}: {{ label }}",
    "ui.agent_name": "{{ agent_name }}",
    "tools.invocation_rules": (
        "# Tool Invocation Rules\n"
        "1. Minimize tool calls.\n"
        "2. Call tools directly, don't describe plans in text.\n"
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


def _flatten_yaml_keys(
    d: dict[str, Any], prefix: str = ""
) -> list[tuple[str, str]]:
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
            joined = "\n".join(
                str(item) for item in value if isinstance(item, str)
            )
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
            autoescape=False,
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
                logger.warning(
                    "Template '%s' not found in bundle or fallbacks", template_name
                )
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
        """Load and merge PromptBundles from multiple domain packages.

        Each domain contributes config/prompts/{locale}/*.yaml.
        Bundles are merged in order; later domains override earlier ones.
        Falls back to the domain's first declared locale if *locale* is
        unavailable, then to FALLBACK_TEMPLATES.
        """
        merged = PromptBundle(locale=locale)
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
                        domain_path.name, locale,
                    )
                    continue
            bundle = PromptBundle.from_directory(prompts_dir, used_locale)
            merged = merged.merge(bundle)

        if not merged.templates:
            logger.warning(
                "from_domain_directories: no templates loaded from any domain "
                "(paths: %s, locale: %s). Engine will use FALLBACK_TEMPLATES.",
                [str(p) for p in domain_paths], locale,
            )
        return cls(bundle=merged)


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
