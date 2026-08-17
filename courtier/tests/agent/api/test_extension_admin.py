"""Tests for skill admin service (list/toggle/create) and plugin manager controls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from courtier.agent.api.routes.admin_extensions import _plugin_items
from courtier.agent.api.services.skill_admin_service import (
    _rewrite_enabled,
    create_skill,
    get_skill,
    list_skills,
    set_skill_enabled,
    update_skill,
)

SKILL_MD = """---
name: demo
display_name: 演示
description: 演示技能
type: skill
version: "1.0"
---

# 目标
做演示。
"""


def _write_skill(dir_path, name="demo", enabled_line=None):
    content = SKILL_MD
    if enabled_line:
        content = content.replace('version: "1.0"', f'version: "1.0"\n{enabled_line}')
    p = dir_path / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


class TestListSkills:
    def test_list_returns_configs(self, tmp_path):
        _write_skill(tmp_path)
        result = list_skills(str(tmp_path))
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["name"] == "demo"
        assert item["displayName"] == "演示"
        assert item["enabled"] is True
        assert result["errors"] == []


class TestRewriteEnabled:
    def test_replaces_existing_key(self):
        fm = '\nname: a\nenabled: true\nversion: "1.0"\n'
        assert "enabled: false" in _rewrite_enabled(fm, False)
        assert "enabled: true" not in _rewrite_enabled(fm, False)

    def test_inserts_missing_key(self):
        fm = '\nname: a\nversion: "1.0"\n'
        out = _rewrite_enabled(fm, False)
        assert out.rstrip().endswith("enabled: false")

    def test_set_enabled_roundtrip(self, tmp_path):
        _write_skill(tmp_path)
        set_skill_enabled(str(tmp_path), "demo", False)
        assert list_skills(str(tmp_path))["items"][0]["enabled"] is False
        set_skill_enabled(str(tmp_path), "demo", True)
        assert list_skills(str(tmp_path))["items"][0]["enabled"] is True

    def test_set_enabled_unknown_raises(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            set_skill_enabled(str(tmp_path), "ghost", False)
        assert exc.value.status_code == 404


class TestCreateSkill:
    def test_create_writes_valid_skill(self, tmp_path):
        result = create_skill(
            str(tmp_path),
            name="weekly_report",
            display_name="周报",
            description="生成周报",
            tools=["search_documents"],
            tags=["报告"],
            system_prompt="# 目标\n生成周报。",
            known_tools={"search_documents"},
        )
        assert result["name"] == "weekly_report"
        listed = list_skills(str(tmp_path))["items"]
        assert any(i["name"] == "weekly_report" and i["enabled"] for i in listed)

    def test_rejects_bad_name(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            create_skill(str(tmp_path), name="Bad-Name", system_prompt="x")
        assert exc.value.status_code == 400

    def test_rejects_duplicate(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            create_skill(str(tmp_path), name="demo", system_prompt="x")
        assert exc.value.status_code == 409

    def test_rejects_empty_prompt(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            create_skill(str(tmp_path), name="ok_name", system_prompt="  ")
        assert exc.value.status_code == 400

    def test_rejects_unknown_subskill(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            create_skill(str(tmp_path), name="newone", skills=["ghost"], system_prompt="x")
        assert exc.value.status_code == 400

    def test_unknown_tool_warns_not_fails(self, tmp_path):
        result = create_skill(
            str(tmp_path),
            name="newone",
            tools=["nonexistent_tool"],
            system_prompt="x",
            known_tools={"search_documents"},
        )
        assert result["warnings"]


class TestGetSkill:
    def test_returns_full_detail_with_prompt(self, tmp_path):
        _write_skill(tmp_path)
        detail = get_skill(str(tmp_path), "demo")
        assert detail["name"] == "demo"
        assert detail["displayName"] == "演示"
        assert detail["systemPrompt"].startswith("# 目标")
        assert detail["source"] == "demo.md"

    def test_unknown_raises_404(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            get_skill(str(tmp_path), "ghost")
        assert exc.value.status_code == 404


class TestUpdateSkill:
    def test_update_rewrites_fields_and_body(self, tmp_path):
        _write_skill(tmp_path)
        result = update_skill(
            str(tmp_path),
            "demo",
            display_name="新演示",
            description="改后的描述",
            mode="parallel",
            tools=["search_documents"],
            tags=["审核"],
            system_prompt="# 新目标\n做另一件事。",
            known_tools={"search_documents"},
        )
        assert result["name"] == "demo"
        detail = get_skill(str(tmp_path), "demo")
        assert detail["displayName"] == "新演示"
        assert detail["description"] == "改后的描述"
        assert detail["mode"] == "parallel"
        assert detail["tools"] == ["search_documents"]
        assert detail["tags"] == ["审核"]
        assert detail["systemPrompt"].startswith("# 新目标")

    def test_update_preserves_untouched_frontmatter_keys(self, tmp_path):
        path = _write_skill(tmp_path)
        text = path.read_text(encoding="utf-8").replace(
            'version: "1.0"',
            'version: "2.1"\ntimeout_seconds: 900\noutput_artifact_type: audit_result',
        )
        path.write_text(text, encoding="utf-8")
        update_skill(str(tmp_path), "demo", system_prompt="# 目标\n新内容。")
        raw = path.read_text(encoding="utf-8")
        fm = yaml.safe_load(raw[3 : raw.index("\n---", 3)])
        assert fm["version"] == "2.1"
        assert fm["timeout_seconds"] == 900
        assert fm["output_artifact_type"] == "audit_result"

    def test_update_clears_optional_keys_when_emptied(self, tmp_path):
        _write_skill(tmp_path)
        update_skill(str(tmp_path), "demo", system_prompt="# 目标\n清空可选字段。")
        detail = get_skill(str(tmp_path), "demo")
        # displayName 回落为 name；description/tags 等键被移除
        assert detail["displayName"] == "demo"
        assert detail["description"] != "演示技能"
        assert detail["tags"] == []

    def test_update_keeps_enabled_flag(self, tmp_path):
        _write_skill(tmp_path, enabled_line="enabled: false")
        update_skill(str(tmp_path), "demo", system_prompt="# 目标\n内容。")
        assert get_skill(str(tmp_path), "demo")["enabled"] is False

    def test_update_unknown_raises_404(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            update_skill(str(tmp_path), "ghost", system_prompt="x")
        assert exc.value.status_code == 404

    def test_update_rejects_empty_prompt(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            update_skill(str(tmp_path), "demo", system_prompt="  ")
        assert exc.value.status_code == 400

    def test_update_rejects_unknown_subskill(self, tmp_path):
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            update_skill(str(tmp_path), "demo", skills=["ghost"], system_prompt="x")
        assert exc.value.status_code == 400

    def test_update_may_reference_itself_as_subskill_excluded(self, tmp_path):
        # 自身不允许作为子技能（known_skills 已排除自身）
        _write_skill(tmp_path)
        with pytest.raises(HTTPException) as exc:
            update_skill(str(tmp_path), "demo", skills=["demo"], system_prompt="x")
        assert exc.value.status_code == 400


# ---- Plugin manager start/stop/restart ---------------------------------------


class TestPluginManagerControls:
    def _manager(self):
        from courtier.plugin.manager import ProcessManager

        mgr = ProcessManager.__new__(ProcessManager)
        mgr._processes = {}
        mgr._scan_results = {}
        mgr._lifecycle = None
        mgr._max_restarts = 3
        mgr._health_interval = 30.0
        mgr._artifact_store = None
        mgr._artifact_store_registry = None
        mgr._extension_registry = SimpleNamespace(
            on_unregister=lambda name: None,
        )
        mgr._start_one_calls = []
        mgr._stop_one_calls = []

        async def fake_start_one(proc):
            from courtier.plugin.manager import PluginState

            mgr._start_one_calls.append(proc.name)
            proc.state = PluginState.ACTIVE

        async def fake_stop_one(proc):
            from courtier.plugin.manager import PluginState

            mgr._stop_one_calls.append(proc.name)
            proc.state = PluginState.STOPPED

        mgr._start_one = fake_start_one
        mgr._stop_one = fake_stop_one
        return mgr

    def _proc(self, name, state):
        from courtier.plugin.manager import PluginState  # noqa: F401

        proc = SimpleNamespace(
            name=name,
            state=state,
            manifest=SimpleNamespace(version="1.0"),
            plugin_dir=None,
            _restart_count=0,
            _health_failures=0,
        )
        return proc

    @pytest.mark.asyncio
    async def test_start_unknown_raises(self):
        mgr = self._manager()
        with pytest.raises(KeyError):
            await mgr.start_plugin("ghost")

    @pytest.mark.asyncio
    async def test_start_active_is_noop(self):
        from courtier.plugin.manager import PluginState

        mgr = self._manager()
        mgr._processes["p1"] = self._proc("p1", PluginState.ACTIVE)
        state = await mgr.start_plugin("p1")
        assert state == PluginState.ACTIVE
        assert mgr._start_one_calls == []

    @pytest.mark.asyncio
    async def test_start_fatal_process_restarts_it(self):
        from courtier.plugin.manager import PluginState

        mgr = self._manager()
        mgr._processes["p1"] = self._proc("p1", PluginState.FATAL)
        state = await mgr.start_plugin("p1")
        assert state == PluginState.ACTIVE
        assert mgr._start_one_calls == ["p1"]

    @pytest.mark.asyncio
    async def test_stop_unknown_raises(self):
        mgr = self._manager()
        with pytest.raises(KeyError):
            await mgr.stop_plugin("ghost")

    @pytest.mark.asyncio
    async def test_restart_stops_then_starts(self):
        from courtier.plugin.manager import PluginState

        mgr = self._manager()
        mgr._processes["p1"] = self._proc("p1", PluginState.ACTIVE)
        state = await mgr.restart_plugin("p1")
        assert state == PluginState.ACTIVE
        assert mgr._stop_one_calls == ["p1"]
        assert mgr._start_one_calls == ["p1"]

    @pytest.mark.asyncio
    async def test_restart_stopped_process_only_starts(self):
        from courtier.plugin.manager import PluginState

        mgr = self._manager()
        mgr._processes["p1"] = self._proc("p1", PluginState.STOPPED)
        state = await mgr.restart_plugin("p1")
        assert state == PluginState.ACTIVE
        assert mgr._stop_one_calls == []
        assert mgr._start_one_calls == ["p1"]


class TestPluginItemsSourceGrouping:
    """Plugins group by domain (plugin→domain mapping), not by wrapper dir."""

    def _real_plugin_system(self):
        """Real PluginSystem with scan results loaded, no processes started."""
        from courtier.config import CourtierConfig
        from courtier.plugin import PluginSystem

        repo_root = CourtierConfig.from_env().repo_root
        ps = PluginSystem(plugins_dir=str(repo_root / "plugins"))
        ps._manager._scan_results = {r.name: r for r in ps._scanner.scan(repo_root / "plugins")}
        return ps

    def _request_with(self, plugin_system):
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(plugin_system=plugin_system))
        )

    def test_audit_wrapper_plugins_group_under_domain(self):
        items = _plugin_items(self._request_with(self._real_plugin_system()))
        by_name = {i["name"]: i["source"] for i in items}

        assert by_name["anydoc"] == "shared"
        assert by_name["parse"] == "docaudit"
        # Plugins under plugins/docaudit/audit/ belong to docaudit — the
        # nested wrapper dir must not surface as its own "audit" group.
        assert by_name["check_format"] == "docaudit"
        assert by_name["check_content"] == "docaudit"
        assert by_name["correct_text"] == "docaudit"
        assert by_name["detect_plagiarism"] == "docaudit"
        assert "audit" not in {i["source"] for i in items}
