"""Per-domain enable/disable state, persisted in ``domains/.disabled``.

A plain-text allowlist of disabled domain package names (one per line).
This is deliberately NOT the ``.env`` file: it carries no secrets and is
safe for the admin API to read and write.  It only applies when
``COURTIER_DOMAIN_PACKAGES`` is not explicitly set — an explicit allowlist
always wins (see CourtierConfig.discover).
"""

from __future__ import annotations

from pathlib import Path

DISABLED_FILE = ".disabled"


def read_disabled_domains(domains_dir: Path) -> set[str]:
    """Return the set of disabled domain names (empty when no file)."""
    path = domains_dir / DISABLED_FILE
    if not path.is_file():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            names.add(name)
    return names


def write_disabled_domains(domains_dir: Path, names: set[str]) -> None:
    """Persist the disabled-name set (sorted, one per line)."""
    path = domains_dir / DISABLED_FILE
    content = "".join(f"{name}\n" for name in sorted(names))
    path.write_text(content, encoding="utf-8")
