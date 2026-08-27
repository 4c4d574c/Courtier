import logging
import threading
import time

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exc

logger = logging.getLogger(__name__)

_es_client: Elasticsearch | None = None
_es_lock = threading.Lock()

#: Seconds a resolved write-index name stays cached before re-resolving.
#: Index swaps (reindex script) are visible to running processes after at
#: most this delay.
_WRITE_INDEX_TTL_SECONDS = 60.0
_write_index_cache: str | None = None
_write_index_cache_ts: float = 0.0


def build_index_mapping(embedding_dim: int = 1024) -> dict:
    """Chunks index mapping.  *embedding_dim* fixes the dense_vector
    dimensionality for hybrid retrieval (a dim change requires reindex)."""
    mapping: dict = {
        "mappings": {
            "properties": {
                "resource_id": {"type": "integer"},
                "document_id": {"type": "integer"},
                "doc_type": {"type": "keyword"},
                "paragraph_index": {"type": "integer"},
                "chunk_no": {"type": "integer"},
                # The built-in cjk analyzer emits overlapping bigrams (vs the
                # default standard analyzer's per-character tokens), giving
                # word-approximate Chinese matching without an ES plugin.
                "chunk_text": {"type": "text", "analyzer": "cjk"},
                "title": {"type": "text", "analyzer": "cjk"},
                "author": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "source_id": {"type": "integer"},
                "tags": {"type": "keyword"},
                "publish_date": {"type": "date"},
                "char_count": {"type": "integer"},
                "annotations": {
                    "type": "nested",
                    "properties": {
                        "type": {"type": "keyword"},
                        "severity": {"type": "keyword"},
                        "message": {"type": "text"},
                        "suggestion": {"type": "text"},
                    },
                },
                "audit_status": {"type": "keyword"},
                "created_at": {"type": "date"},
                # Chunks without a vector (embedding disabled or failed)
                # are simply skipped by kNN; RRF then merges lexical only.
                "chunk_vector": {
                    "type": "dense_vector",
                    "dims": embedding_dim,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        },
    }
    return mapping


#: Default mapping (1024-dim vectors); index creation resolves the configured
#: embedding dim at runtime via build_index_mapping.
INDEX_MAPPING = build_index_mapping()


def _get_settings():
    """延迟导入，避免循环导入；读共享快照而非重建 Settings。"""
    from courtier.config import get_settings

    return get_settings()


def get_es_client() -> Elasticsearch:
    """获取全局 ES 客户端单例（线程安全）。"""
    global _es_client
    if _es_client is None:
        with _es_lock:
            if _es_client is None:
                settings = _get_settings()
                hosts = [h.strip() for h in settings.es_hosts.split(",") if h.strip()]
                kwargs: dict = {"request_timeout": 30}
                if settings.es_username and settings.es_password:
                    kwargs["basic_auth"] = (settings.es_username, settings.es_password)
                _es_client = Elasticsearch(hosts, **kwargs)
    return _es_client


def _real_index_name(version: int) -> str:
    """Versioned physical index backing the alias, e.g. ``courtier_chunks_v2``."""
    return f"{_es_index_name()}_v{version}"


def _invalidate_write_index_cache() -> None:
    global _write_index_cache, _write_index_cache_ts
    _write_index_cache = None
    _write_index_cache_ts = 0.0


def resolve_write_index() -> str:
    """Resolve the physical index that writes must target.

    ``settings.es_index_chunks`` names the alias.  When the alias exists,
    its single backing index wins; a plain index of the same name (legacy
    pre-alias deployment) is used directly; otherwise the v1 physical
    index is assumed (``init_index`` creates it).  Result is cached for
    ``_WRITE_INDEX_TTL_SECONDS`` so alias swaps propagate without restart.
    """
    global _write_index_cache, _write_index_cache_ts
    now = time.monotonic()
    if _write_index_cache and now - _write_index_cache_ts < _WRITE_INDEX_TTL_SECONDS:
        return _write_index_cache
    client = get_es_client()
    base = _es_index_name()
    if client.indices.exists_alias(name=base):
        aliases = dict(client.indices.get_alias(name=base))
        if aliases:
            real = next(iter(aliases))
        else:
            real = _real_index_name(1)
    elif client.indices.exists(index=base):
        # Legacy deployment: the configured name is a plain index, not an alias.
        real = base
        logger.info("chunks index %s exists as a plain index; writing directly", base)
    else:
        real = _real_index_name(1)
    _write_index_cache = real
    _write_index_cache_ts = now
    return real


def init_index() -> None:
    """初始化 ES 索引（如果不存在则创建）。

    Creates the v1 physical index and atomically attaches the configured
    name as an alias, so future analyzer/mapping upgrades can rebuild into
    ``{alias}_vN`` and swap the alias without downtime.  Legacy deployments
    (plain index with the configured name) are left untouched.
    """
    client = get_es_client()
    base = _es_index_name()
    if client.indices.exists_alias(name=base):
        return
    if client.indices.exists(index=base):
        return
    mapping = build_index_mapping(_get_settings().llm_embedding_dim or 1024)
    try:
        client.indices.create(index=_real_index_name(1), body=mapping)
    except es_exc.RequestError as e:
        if e.error != "resource_already_exists_exception":
            raise
    client.indices.put_alias(index=_real_index_name(1), name=base)
    _invalidate_write_index_cache()


def _rewrite_bulk_indices(actions: list[dict], index_name: str) -> None:
    """Point every action in a bulk payload at *index_name* in place."""
    for action in actions:
        for meta_key in ("index", "create", "update", "delete"):
            meta = action.get(meta_key)
            if isinstance(meta, dict) and "_index" in meta:
                meta["_index"] = index_name
                break


def index_chunk(doc_id: str, body: dict) -> None:
    """单条索引切片。"""
    client = get_es_client()
    client.index(index=resolve_write_index(), id=doc_id, body=body)


def bulk_index_chunks(actions: list[dict]) -> None:
    """批量索引切片。"""
    if not actions:
        return
    client = get_es_client()
    _rewrite_bulk_indices(actions, resolve_write_index())
    client.bulk(body=actions)


_es_index_name_cache: str | None = None


def _es_index_name() -> str:
    """Cached lookup of the ES chunks index name."""
    global _es_index_name_cache
    if _es_index_name_cache is None:
        _es_index_name_cache = _get_settings().es_index_chunks
    return _es_index_name_cache


def update_chunk(doc_id: str, body: dict) -> None:
    """更新单条切片（用于审核后添加标注）。"""
    client = get_es_client()
    client.update(
        index=resolve_write_index(),
        id=doc_id,
        body={"doc": body},
    )


def _delete_by_field(field: str, value: int) -> None:
    """按指定字段值删除所有相关切片。"""
    client = get_es_client()
    client.delete_by_query(
        index=resolve_write_index(),
        body={"query": {"term": {field: value}}},
    )


def delete_by_resource_id(resource_id: int) -> None:
    """按 resource_id 删除所有相关切片。"""
    _delete_by_field("resource_id", resource_id)


def delete_by_document_id(document_id: int) -> None:
    """按 document_id 删除所有相关切片。"""
    _delete_by_field("document_id", document_id)


def append_annotation(doc_id: str, annotation: dict) -> None:
    """向 ES 切片的 annotations 数组追加一条标注（不覆盖已有标注）。

    使用 scripted upsert 确保并发安全：
    - 如果 annotations 字段不存在，初始化为空数组
    - 追加新标注到数组末尾
    - 同时更新 audit_status
    """
    client = get_es_client()
    client.update(
        index=resolve_write_index(),
        id=doc_id,
        body={
            "script": {
                "source": """
                    if (ctx._source.annotations == null) {
                        ctx._source.annotations = [];
                    }
                    ctx._source.annotations.add(params.annotation);
                    ctx._source.audit_status = 'audited';
                """,
                "params": {"annotation": annotation},
            }
        },
    )
