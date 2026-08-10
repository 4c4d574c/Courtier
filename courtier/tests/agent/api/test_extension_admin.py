"""Tests for skill admin service (list/toggle/create) and plugin manager controls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from courtier.agent.api.services.skill_admin_service import (
    _rewrite_enabled,
    create_skill,
    list_skills,
    set_skill_enabled,
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
