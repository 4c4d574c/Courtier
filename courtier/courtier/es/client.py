import logging
import threading

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exc

logger = logging.getLogger(__name__)

_es_client: Elasticsearch | None = None
_es_lock = threading.Lock()

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "resource_id": {"type": "integer"},
            "document_id": {"type": "integer"},
            "doc_type": {"type": "keyword"},
            "paragraph_index": {"type": "integer"},
            "chunk_no": {"type": "integer"},
            "chunk_text": {"type": "text"},
            "title": {"type": "text"},
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
                }
            },
            "audit_status": {"type": "keyword"},
            "created_at": {"type": "date"},
        }
    },
}


def _get_settings():
    """延迟导入 Settings，避免循环导入。"""
    from courtier.config import Settings

    return Settings()


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


def init_index() -> None:
    """初始化 ES 索引（如果不存在则创建）。"""
    client = get_es_client()
    try:
        client.indices.create(index=_es_index_name(), body=INDEX_MAPPING)
    except es_exc.RequestError as e:
        if e.error != "resource_already_exists_exception":
            raise


def index_chunk(doc_id: str, body: dict) -> None:
    """单条索引切片。"""
    client = get_es_client()
    client.index(index=_es_index_name(), id=doc_id, body=body)


def bulk_index_chunks(actions: list[dict]) -> None:
    """批量索引切片。"""
    if not actions:
        return
    client = get_es_client()
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
        index=_es_index_name(),
        id=doc_id,
        body={"doc": body},
    )


def _delete_by_field(field: str, value: int) -> None:
    """按指定字段值删除所有相关切片。"""
    client = get_es_client()
    client.delete_by_query(
        index=_es_index_name(),
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
        index=_es_index_name(),
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
