"""Universal file tools (read / edit / write) — unit tests.

Policy-free primitives: path boundaries live in the permission gate
(test_permissions_gate.py).  These tests cover file semantics only.
"""

from __future__ import annotations

import pytest

from courtier.agent.tools.builtin.file_tools import EditTool, ReadTool, WriteTool


def _noop_progress(progress: dict) -> None:
    return


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


class TestReadTool:
    async def test_reads_file_with_line_numbers(self, ws):
        f = ws / "a.md"
        f.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
        result = await ReadTool().execute(on_progress=_noop_progress, path=str(f))
        assert result.success is True
        assert "共 3 行 | 显示 1-3 行" in result.data
        assert "     1\t第一行" in result.data
        assert "     3\t第三行" in result.data
        assert "未完" not in result.data

    async def test_line_range_paging(self, ws):
        f = ws / "a.txt"
        f.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
        result = await ReadTool().execute(
            on_progress=_noop_progress, path=str(f), offset=4, limit=3
        )
        assert result.success is True
        assert "显示 4-6 行" in result.data
        assert "     4\tline4" in result.data
        assert "     6\tline6" in result.data
        assert "line7" not in result.data
        assert "续读用 offset=7" in result.data

    async def test_offset_beyond_eof_errors(self, ws):
        f = ws / "a.txt"
        f.write_text("one\n", encoding="utf-8")
        result = await ReadTool().execute(
            on_progress=_noop_progress, path=str(f), offset=99
        )
        assert result.success is False
        assert "超出文件行数" in result.error

    async def test_missing_file_errors(self, ws):
        result = await ReadTool().execute(
            on_progress=_noop_progress, path=str(ws / "nope.md")
        )
        assert result.success is False
        assert "文件不存在" in result.error

    async def test_directory_listing(self, ws):
        (ws / "sub").mkdir()
        (ws / "b.txt").write_text("hello", encoding="utf-8")
        result = await ReadTool().execute(on_progress=_noop_progress, path=str(ws))
        assert result.success is True
        assert "sub/" in result.data and "<dir>" in result.data
        assert "b.txt" in result.data
        # dirs listed before files
        assert result.data.index("sub/") < result.data.index("b.txt")

    async def test_missing_path_param(self):
        result = await ReadTool().execute(on_progress=_noop_progress, path="")
        assert result.success is False

    async def test_binary_file_errors_cleanly(self, ws):
        f = ws / "blob.bin"
        f.write_bytes(b"\x00\xff\xfe\x01")
        result = await ReadTool().execute(on_progress=_noop_progress, path=str(f))
        assert result.success is False
        assert "UTF-8" in result.error


class TestWriteTool:
    async def test_creates_file_with_parents(self, ws):
        target = ws / "docaudit" / "deep" / "MEMORY.md"
        result = await WriteTool().execute(
            on_progress=_noop_progress, path=str(target), content="# 索引\n"
        )
        assert result.success is True
        assert result.data["created"] is True
        assert target.read_text(encoding="utf-8").startswith("# 索引")

    async def test_overwrites_existing(self, ws):
        f = ws / "a.md"
        f.write_text("old", encoding="utf-8")
        result = await WriteTool().execute(
            on_progress=_noop_progress, path=str(f), content="new content"
        )
        assert result.success is True
        assert result.data["created"] is False
        assert f.read_text(encoding="utf-8") == "new content\n"

    async def test_empty_content_clears_file(self, ws):
        f = ws / "a.md"
        f.write_text("stuff", encoding="utf-8")
        result = await WriteTool().execute(
            on_progress=_noop_progress, path=str(f), content=""
        )
        assert result.success is True
        assert f.read_text(encoding="utf-8") == ""

    async def test_write_onto_directory_errors(self, ws):
        d = ws / "sub"
        d.mkdir()
        result = await WriteTool().execute(
            on_progress=_noop_progress, path=str(d), content="x"
        )
        assert result.success is False
        assert "目录" in result.error


class TestEditTool:
    async def test_unique_replacement(self, ws):
        f = ws / "a.md"
        f.write_text("- 条目一\n- 条目二\n", encoding="utf-8")
        result = await EditTool().execute(
            on_progress=_noop_progress,
            path=str(f),
            old_string="- 条目一",
            new_string="- 条目一（已修订）",
        )
        assert result.success is True
        assert "条目一（已修订）" in f.read_text(encoding="utf-8")
        assert "条目二" in f.read_text(encoding="utf-8")

    async def test_not_found_errors(self, ws):
        f = ws / "a.md"
        f.write_text("hello\n", encoding="utf-8")
        result = await EditTool().execute(
            on_progress=_noop_progress, path=str(f), old_string="nope", new_string="x"
        )
        assert result.success is False
        assert "未找到" in result.error

    async def test_multiple_matches_require_uniqueness(self, ws):
        f = ws / "a.md"
        f.write_text("dup\ndup\n", encoding="utf-8")
        result = await EditTool().execute(
            on_progress=_noop_progress, path=str(f), old_string="dup", new_string="x"
        )
        assert result.success is False
        assert "2 处" in result.error
        assert f.read_text(encoding="utf-8") == "dup\ndup\n"  # untouched

    async def test_empty_old_string_errors(self, ws):
        f = ws / "a.md"
        f.write_text("x", encoding="utf-8")
        result = await EditTool().execute(
            on_progress=_noop_progress, path=str(f), old_string="", new_string="y"
        )
        assert result.success is False

    async def test_same_old_and_new_errors(self, ws):
        f = ws / "a.md"
        f.write_text("x", encoding="utf-8")
        result = await EditTool().execute(
            on_progress=_noop_progress, path=str(f), old_string="x", new_string="x"
        )
        assert result.success is False

    async def test_missing_file_errors(self, ws):
        result = await EditTool().execute(
            on_progress=_noop_progress,
            path=str(ws / "nope.md"),
            old_string="a",
            new_string="b",
        )
        assert result.success is False


class TestAgentRegistration:
    def test_file_tools_registered_on_every_agent(self):
        """通用文件工具对每个 agent 无条件注册（边界在权限门，不在注册）。"""
        from courtier.agent.agents.base import Agent
        from courtier.agent.testing import MockModelClient

        agent = Agent(name="T", role="r", tools=[], model=MockModelClient())
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert {"read", "edit", "write"} <= names
