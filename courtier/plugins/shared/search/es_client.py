"""Plugin-local Elasticsearch client for chunk search.

Reads the ES endpoint from the process environment (injected by the host
from plugin.yaml runtime.env).  The host's ``courtier.es`` module keeps
the indexing side; this module owns only the search read path.
"""

from __future__ import annotations

import os
import threading

from elasticsearch import Elasticsearch

_es_client: Elasticsearch | None = None
_es_lock = threading.Lock()

_MAX_ES_SIZE = 10_000


def _get_es_client() -> Elasticsearch:
    """Process-wide ES client singleton (thread-safe)."""
    global _es_client
    if _es_client is None:
        with _es_lock:
            if _es_client is None:
                hosts = [h.strip() for h in os.environ.get("ES_HOSTS", "").split(",") if h.strip()]
                if not hosts:
                    raise RuntimeError("ES_HOSTS not configured for the search plugin")
                kwargs: dict = {"request_timeout": 30}
                username = os.environ.get("ES_USERNAME", "")
                password = os.environ.get("ES_PASSWORD", "")
                if username and password:
                    kwargs["basic_auth"] = (username, password)
                _es_client = Elasticsearch(hosts, **kwargs)
    return _es_client


def _index_name() -> str:
    return os.environ.get("ES_INDEX_CHUNKS") or "courtier_chunks"


def search_chunks(query_body: dict, skip: int = 0, limit: int = 20) -> dict:
    """Run a search query. *limit* is clamped to ``_MAX_ES_SIZE`` (10 000)."""
    client = _get_es_client()
    resp = client.search(
        index=_index_name(),
        body=query_body,
        from_=skip,
        size=min(limit, _MAX_ES_SIZE),
    )
    # elasticsearch-py returns ObjectApiResponse; convert to plain dict for
    # JSON serialization across the JSON-RPC boundary.
    return dict(resp)
