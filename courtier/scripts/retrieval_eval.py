"""Offline retrieval evaluation for ``search_documents``.

Measures recall@k / MRR / NDCG@k against a golden JSONL of
(query, expected_resource_ids, expected_chunk_substrings) and prints a
per-query table plus summary averages. Queries run through the real
search plugin (internal caller — no owner-scope filtering unless
``--scope`` is given), so the numbers reflect the deployed retrieval
stack as configured.

Regression modes (each adds a gate to the summary; exit code 1 when a
gate fails):

- ``--rerank``: second pass with ``rerank=true`` per query.  Gate: the
  reranked page never returns fewer hits than the lexical/hybrid page
  (guards the unranked-remainder append).
- ``--scope N``: reruns the golden set with ``_owner_scope=N`` so the
  visibility filter path is exercised end-to-end.  On a public corpus
  scoped recall must stay within 0.1 of unscoped recall.
- ``--pagination``: fetches skip=0/10/20 pages per query.  Gate: pages
  are disjoint (no chunk repeats), page ranks restart at 1, and a full
  page 1 with total>10 is followed by a non-empty page 2 (guards the
  unified fetch window).

Usage (from the ``courtier/`` project root)::

    uv run python scripts/retrieval_eval.py
    uv run python scripts/retrieval_eval.py --jsonl scripts/retrieval_golden.jsonl --top-k 10
    uv run python scripts/retrieval_eval.py --json  # machine-readable report
    uv run python scripts/retrieval_eval.py --rerank --pagination --scope 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEARCH_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "search"

_SCOPE_RECALL_TOLERANCE = 0.1


def _load_env() -> None:
    """Mirror .env then plugins/plugin.env into os.environ.

    Plugin-owned knobs (ES_HOSTS etc.) live in plugins/plugin.env since
    the standalone-plugin migration; the host .env only keeps host-side
    variables.
    """
    for env_file in (_REPO_ROOT / ".env", _REPO_ROOT / "plugins" / "plugin.env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _relevant(hit: dict, expected_ids: set[int], substrings: list[str]) -> bool:
    if hit.get("resource_id") in expected_ids:
        return True
    text = hit.get("chunk_text") or ""
    return any(sub in text for sub in substrings)


def _dcg(relevance: list[bool]) -> float:
    return sum(1.0 / math.log2(i + 2) for i, rel in enumerate(relevance) if rel)


def _ideal_dcg(num_relevant: int, k: int) -> float:
    n = min(num_relevant, k)
    return sum(1.0 / math.log2(i + 2) for i in range(n))


async def _eval_entry(
    tool,
    entry: dict,
    top_k: int,
    rerank: bool = False,
    owner_scope: int | None = None,
) -> dict:
    expected_ids = set(entry.get("expected_resource_ids") or [])
    substrings = entry.get("expected_chunk_substrings") or []
    kwargs: dict = {"query": entry["query"], "limit": top_k}
    if rerank:
        kwargs["rerank"] = True
    if owner_scope is not None:
        kwargs["_owner_scope"] = owner_scope
    result = await tool.execute(**kwargs)
    if not result.success:
        return {
            "query": entry["query"],
            "error": result.error,
            "recall": 0.0,
            "mrr": 0.0,
            "ndcg": 0.0,
        }
    hits = (result.data or {}).get("hits") or []
    relevance = [_relevant(h, expected_ids, substrings) for h in hits]
    found_ids = {
        h.get("resource_id") for h, rel in zip(hits, relevance) if rel and h.get("resource_id")
    }
    recall = len(found_ids & expected_ids) / len(expected_ids) if expected_ids else 0.0
    first_rank = next((i + 1 for i, rel in enumerate(relevance) if rel), 0)
    mrr = 1.0 / first_rank if first_rank else 0.0
    ideal = _ideal_dcg(min(len(expected_ids), top_k), top_k) if expected_ids else 0.0
    ndcg = _dcg(relevance) / ideal if ideal else 0.0
    row = {
        "query": entry["query"],
        "expected": sorted(expected_ids),
        "found": sorted(found_ids & expected_ids),
        "first_rank": first_rank,
        "recall": recall,
        "mrr": mrr,
        "ndcg": ndcg,
        "mode": (result.data or {}).get("mode"),
        "hits": len(hits),
    }
    if rerank:
        row["rerank_partial"] = bool((result.data or {}).get("rerank_partial"))
    return row


async def _check_rerank(tool, entries: list[dict], top_k: int) -> dict:
    """Reranked pages must never return fewer hits than the plain page."""
    regressions: list[str] = []
    partial = 0
    checked = 0
    for entry in entries:
        base = await tool.execute(query=entry["query"], limit=top_k)
        rr = await tool.execute(query=entry["query"], limit=top_k, rerank=True)
        if not base.success or not rr.success:
            continue
        checked += 1
        n_base = len((base.data or {}).get("hits") or [])
        n_rr = len((rr.data or {}).get("hits") or [])
        if n_rr < n_base:
            regressions.append(entry["query"])
        if (rr.data or {}).get("rerank_partial"):
            partial += 1
    return {
        "mode": "rerank",
        "checked": checked,
        "count_regressions": len(regressions),
        "rerank_partial_rate": round(partial / checked, 4) if checked else None,
        "pass": not regressions,
        "failed_queries": regressions[:5],
    }


async def _check_scope(tool, entries: list[dict], top_k: int, owner: int) -> dict:
    """Scoped runs exercise the visibility filter; on a public corpus the
    scoped recall must stay close to the unscoped recall."""
    rows_unscoped = [
        r
        for r in await asyncio.gather(*(_eval_entry(tool, e, top_k) for e in entries))
        if "error" not in r
    ]
    rows_scoped = [
        r
        for r in await asyncio.gather(
            *(_eval_entry(tool, e, top_k, owner_scope=owner) for e in entries)
        )
        if "error" not in r
    ]
    mean_u = sum(r["recall"] for r in rows_unscoped) / max(len(rows_unscoped), 1)
    mean_s = sum(r["recall"] for r in rows_scoped) / max(len(rows_scoped), 1)
    return {
        "mode": "scope",
        "owner_id": owner,
        "recall_unscoped": round(mean_u, 4),
        "recall_scoped": round(mean_s, 4),
        "pass": mean_s >= mean_u - _SCOPE_RECALL_TOLERANCE,
    }


async def _check_pagination(tool, entries: list[dict]) -> dict:
    """Three pages per query must be disjoint, rank-restarted, and
    continuous (a full page 1 with total>10 implies a non-empty page 2)."""
    violations: list[str] = []
    checked = 0
    for entry in entries:
        pages: list[list[tuple]] = []
        total = 0
        for skip in (0, 10, 20):
            result = await tool.execute(query=entry["query"], skip=skip, limit=10)
            if not result.success:
                violations.append(f"{entry['query']}: error {result.error}")
                break
            data = result.data or {}
            hits = data.get("hits") or []
            total = max(total, data.get("total") or 0)
            if any(h.get("rank") != i + 1 for i, h in enumerate(hits)):
                violations.append(f"{entry['query']}: rank not page-relative at skip={skip}")
            pages.append([(h.get("resource_id"), h.get("chunk_no")) for h in hits])
        if len(pages) < 3:
            continue
        checked += 1
        flat = [coord for page in pages for coord in page]
        if len(flat) != len(set(flat)):
            violations.append(f"{entry['query']}: duplicate chunk across pages")
        if len(pages[0]) == 10 and total > 10 and not pages[1]:
            violations.append(f"{entry['query']}: page 2 empty despite total={total}")
    return {
        "mode": "pagination",
        "checked": checked,
        "violations": len(violations),
        "pass": not violations,
        "details": violations[:5],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval evaluation for search_documents.")
    parser.add_argument(
        "--jsonl",
        default=str(_REPO_ROOT / "scripts" / "retrieval_golden.jsonl"),
        help="golden query file",
    )
    parser.add_argument("--top-k", type=int, default=10, help="results fetched per query")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--rerank", action="store_true", help="rerank A/B gate (needs LLM env)")
    parser.add_argument(
        "--scope",
        type=int,
        default=None,
        metavar="OWNER_ID",
        help="rerun golden set with this owner scope (visibility filter gate)",
    )
    parser.add_argument(
        "--pagination", action="store_true", help="3-page consistency gate per query"
    )
    args = parser.parse_args()

    golden_path = Path(args.jsonl)
    if not golden_path.exists():
        print(f"golden file not found: {golden_path}", file=sys.stderr)
        return 1
    entries = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    _load_env()
    sys.path.insert(0, str(_SEARCH_PLUGIN_DIR))
    from tools import SearchDocumentsTool

    tool = SearchDocumentsTool()
    results = [await _eval_entry(tool, entry, args.top_k) for entry in entries]
    ok = [r for r in results if "error" not in r]

    def mean(key: str) -> float:
        values = [r[key] for r in ok]
        return sum(values) / len(values) if values else 0.0

    gates: list[dict] = []
    if args.rerank:
        gates.append(await _check_rerank(tool, entries, args.top_k))
    if args.scope is not None:
        gates.append(await _check_scope(tool, entries, args.top_k, args.scope))
    if args.pagination:
        gates.append(await _check_pagination(tool, entries))

    summary = {
        "queries": len(entries),
        "top_k": args.top_k,
        "recall@k": round(mean("recall"), 4),
        "mrr": round(mean("mrr"), 4),
        "ndcg@k": round(mean("ndcg"), 4),
        "errors": len(results) - len(ok),
        "gates": gates,
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
        return 1 if any(not g["pass"] for g in gates) else 0

    print(f"query results (top-k={args.top_k}):")
    for r in results:
        if "error" in r:
            print(f"  ERROR {r['query']}: {r['error']}")
            continue
        print(
            f"  recall={r['recall']:.2f} mrr={r['mrr']:.2f} ndcg={r['ndcg']:.2f} "
            f"found={r['found']} mode={r['mode']} | {r['query']}"
        )
    print(
        f"summary: recall@{args.top_k}={summary['recall@k']} "
        f"mrr={summary['mrr']} ndcg@{args.top_k}={summary['ndcg@k']} "
        f"errors={summary['errors']}"
    )
    gate_failed = False
    for g in gates:
        status = "PASS" if g["pass"] else "FAIL"
        gate_failed |= not g["pass"]
        detail = {k: v for k, v in g.items() if k not in ("pass", "failed_queries", "details")}
        print(f"gate[{g['mode']}]: {status} {detail}")
        for q in g.get("failed_queries") or g.get("details") or []:
            print(f"    - {q}")
    return 1 if gate_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
