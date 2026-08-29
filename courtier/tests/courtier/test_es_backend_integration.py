"""Real-ES integration for the ArtifactStore primary backend (plan §2 补测).

Exercises the cross-process read path against the live Elasticsearch
configured in .env / plugins/plugin.env: store A persists (disk + ES),
a fresh store B with an empty ref_map recovers the payload through the
ES fallback.  Excluded from default runs via the ``integration`` marker.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _load_env() -> dict[str, str]:
    """Mirror .env then plugins/plugin.env into os.environ (same as the
    search integration tests)."""
    loaded: dict[str, str] = {}
    for env_file in (_REPO_ROOT / ".env", _REPO_ROOT / "plugins" / "plugin.env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in loaded:
                loaded[key] = value
                os.environ.setdefault(key, value)
    if not loaded.get("ES_HOSTS"):
        pytest.skip("ES_HOSTS not configured — real-ES integration unavailable")
    return loaded


@pytest.fixture
def es_index():
    _load_env()
    from courtier.es.client import get_es_client, invalidate_es_client

    invalidate_es_client()  # rebuild the singleton from the mirrored env
    index = f"courtier_test_ctx_{uuid.uuid4().hex[:8]}"
    yield index
    try:
        get_es_client().indices.delete(index=index, ignore=[400, 404])
    except Exception:
        pass


async def test_persist_recovers_via_es_from_a_fresh_store(tmp_path, es_index):
    """Store A 落盘 + 写 ES；全新 Store B（ref_map 为空）经 ES 回读一致。"""
    from courtier.agent.artifacts.store import ArtifactStore
    from courtier.agent.runtime.es_backend import ElasticsearchResultBackend
    from courtier.es.client import get_es_client

    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    backend_a = ElasticsearchResultBackend(index_name=es_index, session_id=session_id)
    store_a = ArtifactStore(cache_dir=str(tmp_path / "a"), primary_backend=backend_a)
    payload = {"pages": [{"text": "上下文管理集成测试 " * 50}]}

    marker = (await store_a.persist(payload, "parse_document", force=True)).data
    assert marker["__persisted_output__"] is True
    ref_id = marker["ref_id"]

    # Give the ES index a beat (refresh) before the cross-instance read.
    get_es_client().indices.refresh(index=es_index)
    deadline = time.monotonic() + 10
    last_error: dict | None = None
    backend_b = ElasticsearchResultBackend(index_name=es_index, session_id=session_id)
    store_b = ArtifactStore(cache_dir=str(tmp_path / "b"), primary_backend=backend_b)
    result: dict | None = None
    while time.monotonic() < deadline:
        result = await store_b.read(ref_id)
        if "error" not in result:
            break
        last_error = result
        await asyncio.sleep(0.2)
    assert result is not None and "error" not in result, f"ES read failed: {last_error}"
    assert result["data"] == payload
