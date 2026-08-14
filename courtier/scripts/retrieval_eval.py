"""Offline retrieval evaluation for ``search_documents``.

Measures recall@k / MRR / NDCG@k against a golden JSONL of
(query, expected_resource_ids, expected_chunk_substrings) and prints a
per-query table plus summary averages.  Queries run through the real
search plugin (internal caller — no owner-scope filtering), so the
numbers reflect the deployed retrieval stack as configured.

Usage (from the ``courtier/`` project root)::

    uv run python scripts/retrieval_eval.py
    uv run python scripts/retrieval_eval.py --jsonl scripts/retrieval_golden.jsonl --top-k 10
    uv run python scripts/retrieval_eval.py --json  # machine-readable report
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


def _load_env() -> None:
    """Mirror .env into os.environ (the plugin reads process env only)."""
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return
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


async def _eval_entry(tool, entry: dict, top_k: int) -> dict:
    expected_ids = set(entry.get("expected_resource_ids") or [])
    substrings = entry.get("expected_chunk_substrings") or []
    result = await tool.execute(query=entry["query"], limit=top_k)
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
    return {
        "query": entry["query"],
        "expected": sorted(expected_ids),
        "found": sorted(found_ids & expected_ids),
        "first_rank": first_rank,
        "recall": recall,
        "mrr": mrr,
        "ndcg": ndcg,
        "mode": (result.data or {}).get("mode"),
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

    summary = {
        "queries": len(entries),
        "top_k": args.top_k,
        "recall@k": round(mean("recall"), 4),
        "mrr": round(mean("mrr"), 4),
        "ndcg@k": round(mean("ndcg"), 4),
        "errors": len(results) - len(ok),
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
        return 0

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
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
