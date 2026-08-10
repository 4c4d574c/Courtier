"""DB 持久层。

save_doc/load_doc 是与 docmodels.Document 对齐的文档持久化辅助函数
（当前无生产调用方，保持可用并随模型契约演进）：
- 以 doc_id（文件内容 sha256）为唯一键，模型重构后不再有 user_id；
- load_doc 查无文档时返回 None（不再返回空 Document）；
- 间距/缩进、alignment、warnings、schema_version 均支持往返。
"""

from .db_manager import AsyncDatabase, CRUDRepository
from .load_doc import load_doc, load_page
from .save_doc import save_doc, save_page

__all__ = [
    "AsyncDatabase",
    "CRUDRepository",
    "load_doc",
    "load_page",
    "save_doc",
    "save_page",
]
