#!/usr/bin/env python3
"""clean_agent_cache.py — remove cache files not referenced by any session.

Scans the agent cache dir for persisted tool-result files and reports those
not referenced by any session's artifact snapshot ``ref_map``.  Pass
``--delete`` to actually remove them (with their ``.schema.json``
companions).

Usage:
    uv run python scripts/clean_agent_cache.py [--cache-dir uploads/.cache] \
        [--sessions-dir uploads/.cache/sessions] [--delete]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _referenced_files(sessions_dir: Path, cache_root: Path) -> set[Path]:
    """Resolved paths of cache files referenced by at least one session."""
    referenced: set[Path] = set()
    if not sessions_dir.is_dir():
        return referenced
    for path in sessions_dir.glob("sess_*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            snapshot = raw.get("artifact_snapshot") or ""
            if snapshot:
                ref_map = json.loads(snapshot).get("ref_map") or {}
                for filepath in ref_map.values():
                    referenced.add((cache_root / filepath).resolve())
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return referenced


def _schema_companion(path: Path) -> Path:
    if path.suffix == ".json":
        return path.with_suffix(".schema.json")
    return Path(str(path) + ".schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default="uploads/.cache")
    parser.add_argument("--sessions-dir", default="")
    parser.add_argument("--delete", action="store_true", help="actually remove orphan files")
    args = parser.parse_args()

    cache_root = Path(args.cache_dir).resolve()
    sessions_dir = (
        Path(args.sessions_dir).resolve() if args.sessions_dir else cache_root / "sessions"
    )
    if not cache_root.is_dir():
        print(f"缓存目录不存在：{cache_root}", file=sys.stderr)
        return 1

    referenced = _referenced_files(sessions_dir, cache_root)

    orphans: list[Path] = []
    for path in sorted(cache_root.iterdir()):
        if not path.is_file():
            continue
        if path.name == ".hash_index.json" or path.name.endswith(".schema.json"):
            continue
        if path.suffix not in (".json", ".txt"):
            continue
        if path.resolve() not in referenced:
            orphans.append(path)

    if not orphans:
        print("无孤儿缓存文件。")
        return 0

    total = sum(p.stat().st_size for p in orphans)
    print(f"发现 {len(orphans)} 个孤儿缓存文件（共 {total / 1024:.1f} KiB）：")
    for p in orphans:
        print(f"  {p.name}")

    if args.delete:
        for p in orphans:
            p.unlink(missing_ok=True)
            _schema_companion(p).unlink(missing_ok=True)
        print(f"已删除 {len(orphans)} 个文件及其 schema 伴随文件。")
    else:
        print("（干跑模式；加 --delete 实际删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
