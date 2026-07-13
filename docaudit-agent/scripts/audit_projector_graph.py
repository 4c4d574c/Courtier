#!/usr/bin/env python3
"""Audit the projector graph for common issues.

Usage::

    uv run python scripts/audit_projector_graph.py
    python scripts/audit_projector_graph.py

Reports unproduced types, unconsumed types, unused projectors, duplicate edges,
local projectors in production paths, and debug artifacts touching business paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow execution from project root without installing the package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("packages/core/src", "packages/domains/docaudit", "src"):
    _p = str(_PROJECT_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from courtier.agent.artifacts.projectors import (  # noqa: E402
    create_default_projector_registry,
    audit_projector_graph,
)


def main() -> int:
    registry = create_default_projector_registry()
    report = audit_projector_graph(registry)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
