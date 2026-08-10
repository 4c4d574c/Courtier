"""Domain package administration — scaffold new domains and toggle visibility.

A domain package is enabled simply by existing on disk (when no explicit
``COURTIER_DOMAIN_PACKAGES`` allowlist is set); disabling moves its name
into ``domains/.disabled`` (see ``courtier.domain.disabled``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from courtier.domain.disabled import (
    read_disabled_domains,
    write_disabled_domains,
)
from courtier.domain.loader import DomainLoader

from .skill_admin_service import list_skills

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _domains_dir(request_repo_root: Path) -> Path:
    return Path(request_repo_root) / "domains"


def list_domains_with_state(repo_root: Path, *, scan_skills: bool = True) -> list[dict[str, Any]]:
    """List every on-disk domain package with its enabled state.

    Disabled packages (in domains/.disabled) are included with
    ``enabled=False`` and no skills scanned.
    """
    domains_dir = _domains_dir(repo_root)
    disabled = read_disabled_domains(domains_dir)
    domains: list[dict[str, Any]] = []
    if not domains_dir.is_dir():
        return domains
    for entry in sorted(domains_dir.iterdir()):
        if not entry.is_dir() or not (entry / "config" / "domain.yaml").is_file():
            continue
        config = DomainLoader.load(entry)
        enabled = entry.name not in disabled
        item: dict[str, Any] = {
            "name": entry.name,
            "title": config.title if config else entry.name,
            "description": config.description if config else "",
            "enabled": enabled,
            "skillsPath": str(entry / "skills"),
            "items": [],
            "errors": [],
        }
        if enabled and scan_skills and (entry / "skills").is_dir():
            result = list_skills(str(entry / "skills"))
            item["items"] = result["items"]
            item["errors"] = result["errors"]
        domains.append(item)
    return domains


def scaffold_domain(
    repo_root: Path,
    *,
    name: str,
    title: str = "",
    description: str = "",
    locale: str = "zh-CN",
) -> dict[str, Any]:
    """Create a minimal valid domain package on disk.

    Layout: config/domain.yaml + config/prompts/<locale>/ placeholder bundle
    + empty skills/ directory.  The empty skills dir shows up as a
    validate_domain issue until the first skill is created.
    """
    if not _NAME_RE.match(name):
        raise HTTPException(400, "域名必须以小写字母开头，仅含小写字母/数字/下划线（≤64 字符）")
    domain_path = _domains_dir(repo_root) / name
    if domain_path.exists():
        raise HTTPException(409, f"domain package {name} 已存在")

    (domain_path / "config" / "prompts" / locale).mkdir(parents=True)
    (domain_path / "skills").mkdir()

    domain_yaml = {
        "name": name,
        "title": title or name,
        "description": description,
        "locales": [locale],
        "requires_plugins": [],
        "requires_services": [],
    }
    (domain_path / "config" / "domain.yaml").write_text(
        yaml.safe_dump(domain_yaml, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # Minimal placeholder prompt bundle so locale validation passes
    # (no keys — unknown keys would trigger template-engine warnings).
    (domain_path / "config" / "prompts" / locale / "prompts.yaml").write_text(
        "# Prompt bundle for the domain package.\n{}\n",
        encoding="utf-8",
    )

    issues = DomainLoader.validate_domain(domain_path)
    return {"name": name, "title": title or name, "issues": issues}


def set_domain_enabled(repo_root: Path, name: str, enabled: bool) -> None:
    """Enable/disable a domain package via the domains/.disabled list."""
    domains_dir = _domains_dir(repo_root)
    if not (domains_dir / name / "config" / "domain.yaml").is_file():
        raise HTTPException(404, f"domain package 不存在: {name}")
    disabled = read_disabled_domains(domains_dir)
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    write_disabled_domains(domains_dir, disabled)
