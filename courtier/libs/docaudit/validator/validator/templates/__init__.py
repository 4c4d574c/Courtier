"""模板子包。

提供模板内容校验和印章默认值处理。模板的数据库读取由宿主
``template_store`` host service 提供（见 courtier/plugin/manager.py），
插件侧经 ``courtier_plugin_sdk.HostTemplateStore`` 访问。
"""

from validator.templates.preview import (
    get_default_seal_base64,
    replace_default_seal,
    spec_from_template_content,
)

__all__ = [
    "get_default_seal_base64",
    "replace_default_seal",
    "spec_from_template_content",
]
