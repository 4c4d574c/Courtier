"""Citation-rule single-source-of-truth guard.

The search plugin's manifest used to carry a detailed system_prompt that
never reached the host (the register notification only forwards what
entry.py returns), while the live rules live in the core behavioral.yaml
bundle — the two had already drifted. These tests keep anyone from
reintroducing dead prompt config or stripping the live rules.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_search_manifest_declares_no_capabilities_or_prompt():
    manifest = yaml.safe_load(
        (_REPO_ROOT / "plugins" / "shared" / "search" / "plugin.yaml").read_text("utf-8")
    )
    assert "capabilities" not in manifest, (
        "plugin.yaml capabilities is dead config: runtime registration is "
        "driven by entry.py register_tool()"
    )
    assert manifest["runtime"]["env"]["ES_HOSTS"]  # runtime section intact


def test_behavioral_zh_carries_citation_rules():
    content = (
        _REPO_ROOT / "courtier" / "prompts" / "defaults" / "zh-CN" / "behavioral.yaml"
    ).read_text("utf-8")
    for needle in (
        "[[n]]",
        "citation_index",
        "【引用编号",
        "read_chunks",
        "严禁使用 [[0]]",
    ):
        assert needle in content, f"behavioral.yaml (zh-CN) lost citation rule: {needle}"
    # The two rules jammed onto one line (formatting damage) must stay split.
    assert "无效标记；      ·" not in content


def test_behavioral_en_carries_citation_rules():
    content = (
        _REPO_ROOT / "courtier" / "prompts" / "defaults" / "en-US" / "behavioral.yaml"
    ).read_text("utf-8")
    assert "[[n]]" in content
    assert "read_chunks" in content
