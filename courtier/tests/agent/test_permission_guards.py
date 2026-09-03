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
    """Capability 声明即全部：声明 {"path": [...]} 就恰好只有那些路径。"""

    def _registry(self):
        from courtier.agent.core.capability import Capability, CapabilityRegistry

        registry = CapabilityRegistry()
        registry.register(
            Capability(
                type="tool",
                name="archive_dump",
                meta={"permission": {"path_policy": {"path": ["/srv/dumps"]}}},
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
    async def test_declared_paths_are_the_whole_truth(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=self._registry()
        )
        # 声明根内放行（即使不在会话根内——声明即全部）
        allowed = await guard.check_call(
            _call("archive_dump", path="/srv/dumps/out.txt"), _ctx()
        )
        assert allowed.action == "allow"
        # 声明根外拒绝（包括会话根——不再有隐式默认）
        elsewhere = await guard.check_call(
            _call("archive_dump", path="/etc/passwd"), _ctx()
        )
        assert elsewhere.action == "deny"

    @pytest.mark.asyncio
    async def test_declared_opt_out_overrides_default_list(self, roots):
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=self._registry()
        )
        # read 显式豁免 → 即使在白名单根内也不再受路径检查管（放行）
        result = await guard.check_call(_call("read", path="/etc/passwd"), _ctx())
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_incomplete_declaration_fails_closed(self, roots):
        from courtier.agent.core.capability import Capability, CapabilityRegistry

        registry = CapabilityRegistry()
        registry.register(
            Capability(
                type="tool",
                name="weird",
                meta={"permission": {"unknown_key": True}},
            )
        )
        allowlist, _, _ = roots
        guard = PathPolicyGuard(
            allowed_roots=allowlist, capability_registry=registry
        )
        result = await guard.check_call(_call("weird", path="/tmp/x"), _ctx())
        # 看不懂/不完整的声明 fail-closed：空根清单拒绝一切
        assert result.action == "deny"

class TestPerToolRoots:
    """自定义工具以 {"path": [...]} 声明自己的管辖根；未声明走老名单基线。"""

    def _registry(self, declaration_for_export):
        from courtier.agent.core.capability import Capability, CapabilityRegistry

        registry = CapabilityRegistry()
        if declaration_for_export is not None:
            registry.register(
                Capability(
                    type="tool",
                    name="export_report",
                    meta={"permission": {"path_policy": declaration_for_export}},
                )
            )
        return registry

    async def _check(self, registry, tool, path):
        from courtier.agent.core.guardrails import PathPolicyGuard

        guard = PathPolicyGuard(allowed_roots=["/tmp"], capability_registry=registry)
        return await guard.check_call(_call(tool, path=path), _ctx())

    @pytest.mark.asyncio
    async def test_declared_roots_exactly_as_given(self):
        registry = self._registry({"path": ["/srv/reports"]})
        allowed = await self._check(registry, "export_report", "/srv/reports/o.pdf")
        assert allowed.action == "allow"
        denied = await self._check(registry, "export_report", "/etc/passwd")
        assert denied.action == "deny"

    @pytest.mark.asyncio
    async def test_tilde_expansion_in_declaration(self):
        registry = self._registry({"path": ["~/reports"]})
        expanded = Path("~/reports").expanduser()
        allowed = await self._check(registry, "export_report", str(expanded / "o.pdf"))
        assert allowed.action == "allow"

    @pytest.mark.asyncio
    async def test_false_declaration_exempts(self):
        registry = self._registry(False)
        result = await self._check(registry, "export_report", "/etc/passwd")
        assert result.action == "allow"   # 不受管

    @pytest.mark.asyncio
    async def test_undeclared_tool_uses_legacy_baseline(self):
        """未声明的 read 走老名单基线（会话根），未在名单的自定义工具不受管。"""
        registry = self._registry(None)
        denied = await self._check(registry, "read", "/etc/hostname")
        assert denied.action == "deny" and "不在允许范围内" in denied.reason
        allowed = await self._check(registry, "export_report", "/etc/hostname")
        assert allowed.action == "allow"

    @pytest.mark.asyncio
    async def test_incomplete_declaration_fails_closed(self):
        registry = self._registry({"unknown_key": True})
        result = await self._check(registry, "export_report", "/tmp/x")
        # 带权限字典却没有 path 键 → fail-closed 拒绝一切
        assert result.action == "deny"
