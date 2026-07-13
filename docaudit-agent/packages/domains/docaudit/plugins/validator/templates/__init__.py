"""模板子包。

提供模板的 CRUD、骨架加载和预览功能。
"""

from validator.templates.crud import load_template_from_db
from validator.templates.preview import (
    get_default_seal_base64,
    replace_default_seal,
    spec_from_template_content,
    spec_to_sample_document,
)
from validator.templates.skeleton import load_builtin_skeleton

__all__ = [
    "get_default_seal_base64",
    "load_builtin_skeleton",
    "load_template_from_db",
    "replace_default_seal",
    "spec_from_template_content",
    "spec_to_sample_document",
]
