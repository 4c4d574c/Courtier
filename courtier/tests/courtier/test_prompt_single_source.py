"""Prompt/config single-source-of-truth guard.

Tool declarations and usage guidance are runtime-registered (entry.py's
register_tool / register_capabilities); a capabilities block in any
plugin.yaml is dead config that silently drifts — search's manifest
prompt never reached the host and had already drifted from the live
behavioral.yaml rules when 832dea7 cleaned it up. These tests keep
anyone from reintroducing dead manifest config or stripping the live
citation rules.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_manifest_declares_capabilities():
    offenders = []
    for path in sorted((_REPO_ROOT / "plugins").rglob("plugin.yaml")):
        manifest = yaml.safe_load(path.read_text("utf-8")) or {}
        if "capabilities" in manifest:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"plugin.yaml capabilities is dead config in {offenders}: runtime "
        "registration is driven by entry.py register_tool() and "
        "register_capabilities()"
    )


def test_search_manifest_keeps_runtime_section():
    manifest = yaml.safe_load(
        (_REPO_ROOT / "plugins" / "shared" / "search" / "plugin.yaml").read_text("utf-8")
    )
    # runtime section intact: standalone listen port + literal env defaults.
    assert manifest["runtime"]["port"] == 9105
    assert manifest["runtime"]["env"]["SEARCH_KNN_K"] == "50"


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
        "key_excerpts",
        "无需先 get_artifact 读回",
        "outline=true",
        "materialize_as=string",
        "source_scope",
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
    assert "materialize_as=string" in content
    assert "source_scope" in content
