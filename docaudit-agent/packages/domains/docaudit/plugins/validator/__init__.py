"""validator - 公文格式校验模块。"""

from .format_checker import (
    compare_document_to_spec,
    list_available_rules,
    load_spec,
    normalize_font_family,
    validator,
)
from .wenzhong import detect_wenzhong, detect_wenzhong_from_doc

__all__ = [
    "compare_document_to_spec",
    "detect_wenzhong",
    "detect_wenzhong_from_doc",
    "list_available_rules",
    "load_spec",
    "normalize_font_family",
    "validator",
]
