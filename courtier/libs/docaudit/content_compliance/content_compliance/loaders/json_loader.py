import json
from pathlib import Path
from typing import Any

from ..engine import RuleConfig

_REQUIRED_TOP_KEYS = ("doc_type", "subtypes")
_REQUIRED_RULE_KEYS = ("id", "name", "check_scope", "type", "pattern", "message")


def load_checker_config(path: Path) -> tuple[str, dict[str, list[RuleConfig]]]:
    """从 JSON 文件加载检查器配置。

    Returns:
        (doc_type, {subtype: [RuleConfig, ...]})

    Raises:
        ValueError: If required keys are missing from the config.
        FileNotFoundError: If the config file does not exist.
        json.JSONDecodeError: If the config file contains invalid JSON.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # 验证顶级必需字段
    _validate_required_keys(data, _REQUIRED_TOP_KEYS, str(path))

    doc_type: str = data["doc_type"]
    subtypes: dict[str, list[RuleConfig]] = {}
    for subtype_name, rules_data in data.get("subtypes", {}).items():
        subtypes[subtype_name] = [_parse_rule(r) for r in rules_data]

    return doc_type, subtypes


def _validate_required_keys(
    data: dict[str, Any], required_keys: tuple[str, ...], source: str
) -> None:
    """验证字典包含所有必需的键。"""
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ValueError(
            f"Missing required keys in {source}: {', '.join(missing)}"
        )


def _parse_rule(data: dict[str, Any]) -> RuleConfig:
    _validate_required_keys(data, _REQUIRED_RULE_KEYS, "rule")
    return RuleConfig(
        id=data["id"],
        name=data["name"],
        check_scope=data["check_scope"],
        type=data["type"],
        pattern=data["pattern"],
        message=data["message"],
        check_length=data.get("check_length"),
    )
