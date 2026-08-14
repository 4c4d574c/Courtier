"""Rebuild the ES chunks index into a new versioned physical index and swap the alias.

The configured ``es_index_chunks`` name acts as an alias (see
``courtier/es/client.py``).  This script rebuilds chunks from the source of
truth (MySQL ``resources`` rows + MinIO originals), verifies them, and swaps
the alias atomically.  Legacy deployments (plain index under the configured
name) are snapshotted before the swap.

Usage (from the ``courtier/`` project root)::

    uv run python scripts/reindex_chunks.py --check-only
    uv run python scripts/reindex_chunks.py --target-version v2
    uv run python scripts/reindex_chunks.py --swap-to v2 [--keep-old]
    uv run python scripts/reindex_chunks.py --rollback
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exc
from sqlalchemy import select

from courtier.agent.api.services.resource_service import (
    _extract_text,
    _split_chunks,
    build_chunk_actions,
)
from courtier.config import Settings
from courtier.db.db_manager import AsyncDatabase
from courtier.db.tables.resource import ResourceTable
from courtier.db.tables.user import UserTable
from courtier.es import get_es_client
from courtier.storage import get_object, object_exists

_BULK_BATCH = 2000
_VERSION_RE = re.compile(r"^(.+)_v(\d+)$")


def _real_index(base: str, version: int) -> str:
    return f"{base}_v{version}"


def _version_of(index_name: str, base: str) -> int | None:
    match = _VERSION_RE.match(index_name)
    if match and match.group(1) == base:
        return int(match.group(2))
    return None


def _all_related_indices(client: Elasticsearch, base: str) -> list[str]:
    """All indices whose name starts with ``{base}_`` (versions + legacy snapshots)."""
    try:
        return sorted(dict(client.indices.get(index=f"{base}_*")).keys())
    except es_exc.NotFoundError:
        return []


def _current_versions(client: Elasticsearch, base: str) -> set[int]:
    versions = set()
    for name in _all_related_indices(client, base):
        version = _version_of(name, base)
        if version is not None:
            versions.add(version)
    return versions


def _next_version(client: Elasticsearch, base: str) -> int:
    versions = _current_versions(client, base)
    legacy = client.indices.exists(index=base) and not client.indices.exists_alias(name=base)
    if versions:
        return max(versions) + 1
    return 2 if legacy else 1


async def _load_rows(db: AsyncDatabase) -> tuple[list[ResourceTable], dict[int, str]]:
    async with db.session() as session:
        rows = list((await session.execute(select(ResourceTable))).scalars().all())
        users = dict((await session.execute(select(UserTable.id, UserTable.username))).all())
    return rows, users


def _rebuild_one(resource: ResourceTable, content: bytes) -> list[str]:
    """Extract + chunk one resource's raw bytes.  Returns [] on empty text."""
    ext = Path(resource.minio_path).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".txt") as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text = _extract_text(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        return []
    return _split_chunks(text)


async def cmd_check(settings: Settings, db: AsyncDatabase) -> int:
    """Report MinIO coverage for every resources row.  Exit 1 on any gap."""
    rows, _users = await _load_rows(db)
    missing: list[tuple[int, str, str]] = []
    for resource in rows:
        exists = await asyncio.to_thread(
            object_exists, settings.minio_bucket_resources, resource.minio_path
        )
        if not exists:
            missing.append((resource.id, resource.title or "", resource.minio_path))
    print(f"resources: {len(rows)}  missing MinIO objects: {len(missing)}")
    for row_id, title, path in missing:
        print(f"  gap: id={row_id} title={title!r} minio_path={path}")
    return 1 if missing else 0


async def cmd_build(
    settings: Settings,
    client: Elasticsearch,
    db: AsyncDatabase,
    target: str,
) -> int:
    """Rebuild all resources into *target* (must not exist)."""
    if client.indices.exists(index=target):
        print(f"error: target index {target} already exists", file=sys.stderr)
        return 1

    # Create the target with the host-side mapping (includes analysis settings).
    from courtier.es.client import INDEX_MAPPING

    client.indices.create(index=target, body=INDEX_MAPPING)

    rows, users = await _load_rows(db)
    rebuilt, gaps, mismatch = 0, 0, 0
    batch: list[dict] = []

    async def flush() -> None:
        nonlocal batch
        if batch:
            await asyncio.to_thread(client.bulk, body=batch)
            batch = []

    for resource in rows:
        exists = await asyncio.to_thread(
            object_exists, settings.minio_bucket_resources, resource.minio_path
        )
        if not exists:
            gaps += 1
            print(f"  gap: id={resource.id} title={resource.title!r} (MinIO object missing)")
            continue
        content = await asyncio.to_thread(
            get_object, settings.minio_bucket_resources, resource.minio_path
        )
        chunks = await asyncio.to_thread(_rebuild_one, resource, content)
        if not chunks:
            gaps += 1
            print(f"  gap: id={resource.id} title={resource.title!r} (empty extracted text)")
            continue
        rebuilt_chars = sum(len(c) for c in chunks)
        if len(chunks) != resource.chunk_count or rebuilt_chars != resource.char_count:
            mismatch += 1
            print(
                f"  mismatch: id={resource.id} title={resource.title!r} "
                f"chunks={len(chunks)}/{resource.chunk_count} "
                f"chars={rebuilt_chars}/{resource.char_count}"
            )
        batch.extend(
            build_chunk_actions(
                resource.id,
                chunks,
                doc_type=resource.file_type,
                title=resource.title,
                author=resource.author or "",
                user_id=users.get(resource.owner_id, ""),
                visibility=resource.visibility,
                owner_id=resource.owner_id,
                tags=[t.strip() for t in (resource.tags or "").split(",") if t.strip()],
                publish_date=resource.publish_date,
                index_name=target,
            )
        )
        if len(batch) >= _BULK_BATCH:
            await flush()
        rebuilt += 1
    await flush()
    # search/count are near-realtime (default 1s refresh); refresh explicitly
    # so post-build verification sees all chunks immediately.
    client.indices.refresh(index=target)

    print(f"rebuilt: {rebuilt}  gaps: {gaps}  count/char mismatches: {mismatch}")
    print(f"target index: {target} (NOT yet serving — run --swap-to to activate)")
    return 0


def cmd_swap(
    client: Elasticsearch,
    base: str,
    target_version: int,
    keep_old: bool,
) -> int:
    """Atomically move the alias onto ``{base}_v{target_version}``.

    A legacy plain index under the alias name is snapshotted to
    ``{base}_legacy_<ts>`` first.  Old backing indices are deleted unless
    ``--keep-old`` is given.
    """
    target = _real_index(base, target_version)
    if not client.indices.exists(index=target):
        print(f"error: target index {target} does not exist", file=sys.stderr)
        return 1

    legacy = client.indices.exists(index=base) and not client.indices.exists_alias(name=base)
    actions: list[dict] = []
    removed: list[str] = []
    if legacy:
        snapshot = f"{base}_legacy_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        client.reindex(body={"source": {"index": base}, "dest": {"index": snapshot}})
        client.indices.delete(index=base)
        removed.append(snapshot)
        print(f"legacy plain index snapshotted to {snapshot}")
    if client.indices.exists_alias(name=base):
        for backing in dict(client.indices.get_alias(name=base)).keys():
            if backing != target:
                actions.append({"remove": {"index": backing, "alias": base}})
                removed.append(backing)
    actions.append({"add": {"index": target, "alias": base}})
    client.indices.update_aliases(body={"actions": actions})
    print(f"alias {base} -> {target}")

    if removed and not keep_old:
        for index_name in removed:
            client.indices.delete(index=index_name)
            print(f"deleted old index {index_name}")
    elif removed:
        print(f"kept old indices: {', '.join(removed)}")
    return 0


def cmd_rollback(client: Elasticsearch, base: str) -> int:
    """Re-attach the alias to the highest-versioned index other than the current one."""
    if not client.indices.exists_alias(name=base):
        print(f"error: alias {base} does not exist", file=sys.stderr)
        return 1
    current = set(dict(client.indices.get_alias(name=base)).keys())
    candidates = [name for name in _all_related_indices(client, base) if name not in current]
    if not candidates:
        print("error: no alternative index to roll back to", file=sys.stderr)
        return 1

    def sort_key(name: str) -> tuple[int, int | str]:
        version = _version_of(name, base)
        return (1, version) if version is not None else (0, name)

    target = sorted(candidates, key=sort_key)[-1]
    actions = [{"remove": {"index": c, "alias": base}} for c in current]
    actions.append({"add": {"index": target, "alias": base}})
    client.indices.update_aliases(body={"actions": actions})
    print(f"alias {base} -> {target} (rollback)")
    return 0


def _parse_version(value: str) -> int:
    """Accept both ``2`` and ``v2`` forms."""
    text = value.strip().lstrip("v")
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"invalid version: {value!r} (expected e.g. 2 or v2)")
    return int(text)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild/swap the ES chunks index.")
    parser.add_argument("--check-only", action="store_true", help="report MinIO coverage, exit")
    parser.add_argument(
        "--target-version", type=_parse_version, help="version to build (default: next)"
    )
    parser.add_argument("--swap-to", type=_parse_version, help="swap alias to the given version")
    parser.add_argument("--rollback", action="store_true", help="swap alias back to previous index")
    parser.add_argument("--keep-old", action="store_true", help="keep old indices after swap")
    args = parser.parse_args()

    settings = Settings()
    if not settings.es_hosts:
        print("error: ES_HOSTS is not configured", file=sys.stderr)
        return 1
    if not settings.mysql_url:
        print("error: MYSQL_URL is not configured", file=sys.stderr)
        return 1

    client = get_es_client()
    base = settings.es_index_chunks
    db = AsyncDatabase(settings.mysql_url)

    if args.check_only:
        return await cmd_check(settings, db)
    if args.rollback:
        return cmd_rollback(client, base)
    if args.swap_to is not None:
        return cmd_swap(client, base, args.swap_to, args.keep_old)

    target_version = args.target_version or _next_version(client, base)
    target = _real_index(base, target_version)
    return await cmd_build(settings, client, db, target)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
