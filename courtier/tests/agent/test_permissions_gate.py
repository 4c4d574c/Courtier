"""PermissionGate path policy + name-level checks (file-tools plan task 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.agent.core.tool_call import ToolCall
from courtier.agent.permissions.gate import PermissionGate


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="t1", name=name, arguments=arguments)


@pytest.fixture
def roots(tmp_path):
    memory = tmp_path / "memory"
    sessions = tmp_path / "sessions" / "sess_a"
    memory.mkdir()
    sessions.mkdir(parents=True)
    return [str(memory), str(sessions)], memory, sessions


class TestNameLevelDenial:
    def test_blocked_tool_denied_with_reason(self):
        gate = PermissionGate()
        gate.block("deploy")
        reason = gate.check(_call("deploy"))
        assert reason is not None and "deploy" in reason and "禁用" in reason

    def test_unblocked_tool_passes(self):
        gate = PermissionGate()
        assert gate.check(_call("echo", text="hi")) is None


class TestPathPolicy:
    def test_policy_inactive_without_roots(self, tmp_path):
        """默认门（无根）参数级不激活——保持既有 allow-all 行为。"""
        gate = PermissionGate()
        outside = str(tmp_path.parent / "elsewhere" / "x.md")
        assert gate.check(_call("write", path=outside, content="x")) is None

    def test_path_inside_root_allowed(self, roots):
        allowlist, memory, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        target = memory / "docaudit" / "MEMORY.md"
        assert gate.check(_call("write", path=str(target), content="x")) is None

    def test_path_outside_roots_denied_with_listing(self, roots):
        allowlist, memory, sessions = roots
        gate = PermissionGate(allowed_roots=allowlist)
        outside = memory.parent / "elsewhere.md"
        reason = gate.check(_call("edit", path=str(outside), old_string="a", new_string="b"))
        assert reason is not None
        assert "不在允许范围内" in reason
        assert str(memory) in reason and str(sessions) in reason

    def test_traversal_denied(self, roots):
        allowlist, memory, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        sneaky = str(memory / "docaudit" / ".." / ".." / "etc" / "passwd")
        reason = gate.check(_call("read", path=sneaky))
        assert reason is not None and "不在允许范围内" in reason

    def test_symlink_escape_denied(self, roots, tmp_path):
        allowlist, memory, _ = roots
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("x", encoding="utf-8")
        link = memory / "innocent"
        link.symlink_to(outside_dir)
        gate = PermissionGate(allowed_roots=allowlist)
        reason = gate.check(_call("read", path=str(link / "secret.txt")))
        assert reason is not None and "不在允许范围内" in reason

    def test_session_root_allowed(self, roots):
        allowlist, _, sessions = roots
        gate = PermissionGate(allowed_roots=allowlist)
        assert gate.check(_call("write", path=str(sessions / "notes.md"), content="n")) is None

    def test_non_string_path_denied(self, roots):
        allowlist, _, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        reason = gate.check(_call("read", path={"ref": "$ref:x:1"}))
        assert reason is not None and "非空字符串" in reason

    def test_missing_path_passes_gate(self, roots):
        """缺 path → 放行，由工具自身的"必须提供 path"报错（更准确）。"""
        allowlist, _, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        assert gate.check(_call("read")) is None

    def test_non_path_tools_unaffected(self, roots):
        allowlist, _, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        assert gate.check(_call("echo", text="x")) is None

    def test_fail_closed_on_resolve_error(self, roots, monkeypatch):
        allowlist, _, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)

        def _boom(self):
            raise RuntimeError("resolve exploded")

        monkeypatch.setattr(Path, "resolve", _boom)
        reason = gate.check(_call("read", path="/tmp/x.md"))
        assert reason is not None and "拒绝" in reason

    def test_other_tools_do_not_trigger_path_policy(self, roots):
        """get_artifact 等带 id 参数的工具不受 path 策略影响。"""
        allowlist, _, _ = roots
        gate = PermissionGate(allowed_roots=allowlist)
        assert gate.check(_call("get_artifact", id="$ref:tool:1")) is None
