from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .config import RetryPolicy, SkillConfig, SkillMode

logger = logging.getLogger(__name__)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Split YAML frontmatter from markdown body."""
    if not raw.startswith("---\n"):
        return None, raw

    parts = raw.split("\n---\n", 1)
    if len(parts) != 2:
        return None, raw

    try:
        text = parts[0]
        # Strip the leading "---\n" prefix only, not arbitrary dashes
        if text.startswith("---\n"):
            text = text[4:]
        elif text.startswith("---\r\n"):
            text = text[5:]
        meta = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None, raw

    if not isinstance(meta, dict):
        return None, raw

    return meta, parts[1].strip()


class SkillRegistry:
    """启动时扫描 skills/ 目录并维护预编译的 Skill 配置。"""

    def __init__(self, skills_dir: str | Path) -> None:
        self._skills_dir = Path(skills_dir)
        self._configs: dict[str, SkillConfig] = {}
        self._errors: list[str] = []

    def scan(self) -> None:
        """扫描 skills/ 目录，解析所有 .md 文件。"""
        self._configs.clear()
        self._errors.clear()

        if not self._skills_dir.exists():
            logger.info("Skills directory not found: %s", self._skills_dir)
            return

        for path in sorted(self._skills_dir.glob("*.md")):
            self._scan_one(path)

    def _scan_one(self, path: Path) -> None:
        name = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # The admin UI rewrites skill files in place — a read racing a
            # rewrite (truncated/deleted file) must skip the file, not fail
            # the whole scan and with it every agent build.
            logger.warning("Skill file unreadable during scan: %s", path, exc_info=True)
            self._errors.append(f"Skill '{name}' unreadable during scan: {path}")
            return
        meta, body = _split_frontmatter(raw)

        if meta is None:
            self._errors.append(f"Skill '{name}' missing valid frontmatter: {path}")
            return

        try:
            config = self._build_config(name, path, meta, body)
        except Exception as exc:
            logger.exception("Skill '%s' build failed", name)
            self._errors.append(f"Skill '{name}' build failed: {exc}")
            return

        if config.name in self._configs:
            self._errors.append(f"Duplicate skill name '{config.name}' from {path}; overwriting")

        self._configs[config.name] = config

    def _build_config(self, stem: str, path: Path, meta: dict[str, Any], body: str) -> SkillConfig:
        name = meta.get("name", stem)
        description = meta.get("description", body.split("\n")[0].lstrip("#").strip())
        tools = tuple(meta.get("tools") or [])
        skills = tuple(meta.get("skills") or [])
        tags = tuple(meta.get("tags") or [])
        enabled = bool(meta.get("enabled", True))
        output_artifact_type = meta.get("output_artifact_type")
        input_model = self._resolve_input_model(meta.get("input_model"))
        if input_model is not None:
            from ..agents.subagent.base import data_field_names

            builtin_names = {"task", "mode", "file_path", "ref_ids"}
            conflicts = sorted(data_field_names(input_model) & builtin_names)
            if conflicts:
                raise ValueError(
                    f"input_model {input_model.__name__} fields {conflicts} "
                    f"conflict with built-in SkillTool parameters "
                    f"{sorted(builtin_names)}"
                )
            input_model.model_json_schema()  # fail fast on un-schema-able models
        default_mode = meta.get("default_mode", "")
        if default_mode not in ("subagent", "inline", ""):
            default_mode = ""

        # 协议字段
        type_ = meta.get("type", "skill")
        version = str(meta.get("version", "1.0"))
        mode = meta.get("mode", "auto")
        if isinstance(mode, str):
            try:
                mode = SkillMode(mode)
            except ValueError:
                self._errors.append(f"Skill '{name}' has invalid mode {mode!r}; using 'auto'")
                mode = SkillMode.AUTO
        output_schema = meta.get("output_schema")
        timeout_seconds = int(meta.get("timeout_seconds", 600))
        retry_policy = meta.get("retry_policy", "none")
        if isinstance(retry_policy, str):
            try:
                retry_policy = RetryPolicy(retry_policy)
            except ValueError:
                self._errors.append(
                    f"Skill '{name}' has invalid retry_policy {retry_policy!r}; using 'none'"
                )
                retry_policy = RetryPolicy.NONE

        # 验证
        if type_ != "skill":
            self._errors.append(f"Skill '{name}' has type={type_!r}, expected 'skill'")
        if "version" not in meta:
            self._errors.append(f"Skill '{name}' missing 'version' in frontmatter")

        return SkillConfig(
            name=name,
            description=description,
            source_path=path,
            system_prompt=body,
            tools=tools,
            skills=skills,
            input_model=input_model,
            output_artifact_type=output_artifact_type,
            tags=tags,
            enabled=enabled,
            default_mode=default_mode,
            display_name=meta.get("display_name", ""),
            raw_frontmatter=meta,
            type=type_,
            version=version,
            mode=mode,
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
        )

    @staticmethod
    def _resolve_input_model(import_path: str | None) -> type[BaseModel] | None:
        """Resolve a dotted import path to a BaseModel subclass.

        Args:
            import_path: Dotted path like 'my_module.MyModel'.

        Returns:
            The resolved BaseModel subclass, or None if import_path is empty.

        Raises:
            TypeError: If the resolved object is not a BaseModel subclass.
            ValueError: If the import path format is invalid.
            ModuleNotFoundError: If the module cannot be imported.
            AttributeError: If the class does not exist in the module.
        """
        if not import_path:
            return None

        if "." not in import_path:
            raise ValueError(f"Invalid input_model path: {import_path!r}")

        module_path, class_name = import_path.rsplit(".", 1)
        if not module_path or not class_name:
            raise ValueError(f"Invalid input_model path: {import_path!r}")

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if not isinstance(cls, type) or not issubclass(cls, BaseModel):
            raise TypeError(f"{import_path!r} is not a BaseModel subclass")
        return cls

    def get(self, name: str) -> SkillConfig | None:
        return self._configs.get(name)

    def list_all(self) -> list[SkillConfig]:
        """Return all scanned skills, including disabled ones."""
        return list(self._configs.values())

    def list_enabled(self) -> list[SkillConfig]:
        return [c for c in self._configs.values() if c.enabled]

    def build_catalog(self) -> str:
        lines = []
        for config in sorted(self.list_enabled(), key=lambda c: c.name):
            lines.append(f"- **{config.name}**: {config.description}")
        return "\n".join(lines)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def has_errors(self) -> bool:
        return bool(self._errors)
