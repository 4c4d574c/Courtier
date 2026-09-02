"""Permission guards (tool_call layer): name-level + path policy.

Ported from the former PermissionGate tests — denial texts are part of the
regression contract and asserted verbatim here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.agent.core.guardrails import GuardContext, PathPolicyGuard, ToolDisabledGuard
from courtier.agent.core.tool_call import ToolCall


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="t1", name=name, arguments=arguments)


def _ctx() -> GuardContext:
    return GuardContext()


@pytest.fixture
def roots(tmp_path):
    memory = tmp_path / "memory"
    sessions = tmp_path / "sessions" / "sess_a"
    memory.mkdir()
    sessions.mkdir(parents=True)
    return [str(memory), str(sessions)], memory, sessions


class TestToolDisabledGuard:
    @pytest.mark.asyncio
    async def test_blocked_tool_denied_with_reason(self):
        guard = ToolDisabledGuard(blocked=("deploy",))
        result = await guard.check_call(_call("deploy"), _ctx())
        assert result.action == "deny"
        assert "deploy" in result.reason and "已被禁用" in result.reason
        assert result.error_code == "permission_denied"

    @pytest.mark.asyncio
    async def test_unblocked_tool_passes(self):
        guard = ToolDisabledGuard()
        result = await guard.check_call(_call("echo", text="hi"), _ctx())
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_unrelated_tool_allowed_after_block(self):
        guard = ToolDisabledGuard(blocked=("delete_rule",))
        assert (await guard.check_call(_call("delete_rule"), _ctx())).action == "deny"
        assert (await guard.check_call(_call("echo"), _ctx())).action == "allow"


class TestPathPolicy:
    @pytest.mark.asyncio
    async def test_policy_inactive_without_roots(self, tmp_path):
        """默认门（无根）参数级不激活——保持既有 allow-all 行为。"""
        guard = PathPolicyGuard()
        outside = str(tmp_path.parent / "elsewhere" / "x.md")
        assert (await guard.check_call(_call("write", path=outside, content="x"), _ctx())).action == "allow"

    @pytest.mark.asyncio
    async def test_path_inside_root_allowed(self, roots):
        allowlist, memory, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        target = memory / "docaudit" / "MEMORY.md"
        assert (
            await guard.check_call(_call("write", path=str(target), content="x"), _ctx())
        ).action == "allow"

    @pytest.mark.asyncio
    async def test_path_outside_roots_denied_with_listing(self, roots):
        allowlist, memory, sessions = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        outside = memory.parent / "elsewhere.md"
        result = await guard.check_call(
            _call("edit", path=str(outside), old_string="a", new_string="b"), _ctx()
        )
        assert result.action == "deny"
        assert "不在允许范围内" in result.reason
        assert str(memory) in result.reason and str(sessions) in result.reason

    @pytest.mark.asyncio
    async def test_traversal_denied(self, roots):
        allowlist, memory, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        sneaky = str(memory / "docaudit" / ".." / ".." / "etc" / "passwd")
        result = await guard.check_call(_call("read", path=sneaky), _ctx())
        assert result.action == "deny" and "不在允许范围内" in result.reason

    @pytest.mark.asyncio
    async def test_symlink_escape_denied(self, roots, tmp_path):
        allowlist, memory, _ = roots
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("x", encoding="utf-8")
        link = memory / "innocent"
        link.symlink_to(outside_dir)
        guard = PathPolicyGuard(allowed_roots=allowlist)
        result = await guard.check_call(_call("read", path=str(link / "secret.txt")), _ctx())
        assert result.action == "deny" and "不在允许范围内" in result.reason

    @pytest.mark.asyncio
    async def test_session_root_allowed(self, roots):
        allowlist, _, sessions = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        result = await guard.check_call(
            _call("write", path=str(sessions / "notes.md"), content="n"), _ctx()
        )
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_non_string_path_denied(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        result = await guard.check_call(_call("read", path={"ref": "$ref:x:1"}), _ctx())
        assert result.action == "deny" and "非空字符串" in result.reason

    @pytest.mark.asyncio
    async def test_missing_path_passes_gate(self, roots):
        """缺 path → 放行，由工具自身的"必须提供 path"报错（更准确）。"""
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        assert (await guard.check_call(_call("read"), _ctx())).action == "allow"

    @pytest.mark.asyncio
    async def test_non_path_tools_unaffected(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        assert (await guard.check_call(_call("echo", text="x"), _ctx())).action == "allow"

    @pytest.mark.asyncio
    async def test_fail_closed_on_resolve_error(self, roots, monkeypatch):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)

        def _boom(self):
            raise RuntimeError("resolve exploded")

        monkeypatch.setattr(Path, "resolve", _boom)
        result = await guard.check_call(_call("read", path="/tmp/x.md"), _ctx())
        assert result.action == "deny" and "拒绝" in result.reason

    @pytest.mark.asyncio
    async def test_other_tools_do_not_trigger_path_policy(self, roots):
        """get_artifact 等带 id 参数的工具不受 path 策略影响。"""
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        assert (
            await guard.check_call(_call("get_artifact", id="$ref:tool:1"), _ctx())
        ).action == "allow"


class TestCapabilityDeclaredPolicy:
    """meta["permission"]["path_policy"] 声明优先，未声明回退默认名单。"""

    def _registry(self):
        from courtier.agent.core.capability import Capability, CapabilityRegistry

        registry = CapabilityRegistry()
        registry.register(
            Capability(
                type="tool",
                name="archive_dump",
                meta={"permission": {"path_policy": True}},
            )
        )
        registry.register(
            Capability(
                type="tool",
                name="read",
                meta={"permission": {"path_policy": False}},
            )
        )
        return registry

    @pytest.mark.asyncio
    async def test_declared_opt_in_denies_custom_tool(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=self._registry()
        )
        result = await guard.check_call(
            _call("archive_dump", path="/etc/passwd"), _ctx()
        )
        assert result.action == "deny"

    @pytest.mark.asyncio
    async def test_declared_opt_out_overrides_default_list(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=self._registry()
        )
        result = await guard.check_call(_call("read", path="/etc/passwd"), _ctx())
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_undeclared_tool_falls_back_to_default_list(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=self._registry()
        )
        # "edit" has no capability declaration -> legacy list applies.
        result = await guard.check_call(_call("edit", path="/etc/passwd"), _ctx())
        assert result.action == "deny"

    @pytest.mark.asyncio
    async def test_without_registry_fallback_list_unchanged(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(allowed_roots=allowlist)
        assert (await guard.check_call(_call("read", path="/etc/passwd"), _ctx())).action == "deny"
        assert (await guard.check_call(_call("echo", text="x"), _ctx())).action == "allow"
