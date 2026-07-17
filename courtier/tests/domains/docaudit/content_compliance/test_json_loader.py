import json
from pathlib import Path

from content_compliance.engine import RuleConfig
from content_compliance.loaders.json_loader import load_checker_config


def test_load_checker_config(tmp_path: Path):
    data = {
        "doc_type": "测试",
        "subtypes": {
            "测试子类": [
                {
                    "id": "TEST_001",
                    "name": "必须有问候",
                    "check_scope": "开头",
                    "check_length": 50,
                    "type": "required",
                    "pattern": "你好",
                    "message": "开头缺少问候语",
                }
            ]
        },
    }
    path = tmp_path / "测试.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    doc_type, subtypes = load_checker_config(path)
    assert doc_type == "测试"
    assert "测试子类" in subtypes
    assert len(subtypes["测试子类"]) == 1
    rule = subtypes["测试子类"][0]
    assert isinstance(rule, RuleConfig)
    assert rule.id == "TEST_001"
    assert rule.check_scope == "开头"
    assert rule.check_length == 50
    assert rule.type == "required"
    assert rule.pattern == "你好"
    assert rule.message == "开头缺少问候语"


def test_load_checker_config_without_check_length(tmp_path: Path):
    data = {
        "doc_type": "测试",
        "subtypes": {
            "测试子类": [
                {
                    "id": "TEST_002",
                    "name": "禁止脏话",
                    "check_scope": "全文",
                    "type": "forbidden",
                    "pattern": "脏话",
                    "message": "全文出现禁止用语",
                }
            ]
        },
    }
    path = tmp_path / "测试2.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    doc_type, subtypes = load_checker_config(path)
    rule = subtypes["测试子类"][0]
    assert rule.check_length is None
