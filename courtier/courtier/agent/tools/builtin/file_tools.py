"""Universal file tools — read / edit / write.

Deliberately policy-free primitives (docs/architecture/memory-file-tools-plan.md):
any path is operable; the boundary lives in the permission gate's path
policy (guardrails permission guards), consulted per call before dispatch.
File-based memory is a convention over these tools: files under the
memory workspace root plus MEMORY.md indexes.

skip_persist: read output must reach the model verbatim — a large read
would otherwise be re-persisted and shown as a $ref preview, which is
pointless for content that is already on disk.  skip_ref_resolution:
path arguments are paths, never data refs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..protocol import OnToolProgress, ToolResult

if TYPE_CHECKING:
    from ..summary import ToolSummary

# Output shaping (context protection), not path policy: a read of a huge
# file must not flood the observation window.
_READ_CHAR_CAP = 48_000
_LISTING_ENTRY_CAP = 500


def _err(message: str) -> ToolResult:
    return ToolResult(success=False, error=message)


class ReadTool:
    # 自声明：本工具的 path 参数受会话路径白名单（PathPolicyGuard）管辖，
    # 作用域 = 会话自有空间（记忆目录 + 会话工作区）
    path_policy = "session"
    """Read a file (line-numbered, paged) or list a directory."""

    name: str = "read"
    display_name: str | None = "读取文件"
    description: str = (
        "读取文件内容，带行号，可用 offset/limit 读取指定行区间（翻页）；"
        "path 为目录时返回目录清单（文件名 + 大小）。"
        "读取记忆、文档、代码等任何文件均可。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录的绝对路径"},
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始），默认 1",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数，默认 500",
            },
        },
        "required": ["path"],
    }
    skip_persist: bool = True
    skip_ref_resolution: bool = True

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        path: str = "",
        offset: int = 1,
        limit: int = 500,
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"读取 {path}...", "detail": None})
        if not path:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("必须提供 path 参数")
        p = Path(path)
        if p.is_dir():
            result = self._list_dir(p)
        else:
            result = self._read_file(p, offset, limit)
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return result

    def _read_file(self, p: Path, offset: int, limit: int) -> ToolResult:
        if not p.exists():
            return _err(f"文件不存在: {p}")
        if not p.is_file():
            return _err(f"不是常规文件: {p}")
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(f"无法按 UTF-8 文本读取（可能是二进制文件）: {p}")
        except OSError as exc:
            return _err(f"读取失败: {exc}")

        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(offset))
        limit = max(1, int(limit))
        if start > total and total > 0:
            return _err(f"offset {start} 超出文件行数（共 {total} 行）")
        end = min(total, start + limit - 1)

        parts: list[str] = []
        chars = 0
        char_truncated = False
        for lineno in range(start, end + 1):
            line = f"{lineno:>6}\t{lines[lineno - 1]}"
            chars += len(line)
            if chars > _READ_CHAR_CAP:
                char_truncated = True
                break
            parts.append(line)

        shown_end = start + len(parts) - 1
        header = f"[{p} | 共 {total} 行"
        if total:
            header += f" | 显示 {start}-{shown_end} 行"
        header += "]"
        if start + len(parts) <= total or char_truncated:
            next_offset = shown_end + 1
            header += f"（未完，续读用 offset={next_offset}）"
        body = "\n".join(parts)
        return ToolResult(
            success=True,
            data=f"{header}\n{body}" if body else f"{header}\n（空文件或区间无内容）",
        )

    def _list_dir(self, p: Path) -> ToolResult:
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except OSError as exc:
            return _err(f"读取目录失败: {exc}")
        total = len(entries)
        shown = entries[:_LISTING_ENTRY_CAP]
        lines = [f"[目录 {p} | 共 {total} 项]"]
        for entry in shown:
            if entry.is_dir():
                lines.append(f"{entry.name}/\t<dir>")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = -1
                lines.append(f"{entry.name}\t{size} B")
        if total > _LISTING_ENTRY_CAP:
            lines.append(f"…（其余 {total - _LISTING_ENTRY_CAP} 项未列出）")
        return ToolResult(success=True, data="\n".join(lines))

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class WriteTool:
    # 自声明：本工具的 path 参数受会话路径白名单（PathPolicyGuard）管辖，
    # 作用域 = 会话自有空间（记忆目录 + 会话工作区）
    path_policy = "session"
    """Create or overwrite a file (parent directories created as needed)."""

    name: str = "write"
    display_name: str | None = "写入文件"
    description: str = (
        "创建或整体覆盖文件（自动创建父目录）。"
        "content 为完整文件内容；写入空字符串即清空文件。"
        "维护记忆时，写入记忆文件后记得同步更新对应的 MEMORY.md 索引。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件的绝对路径"},
            "content": {"type": "string", "description": "完整文件内容（可为空串）"},
        },
        "required": ["path", "content"],
    }
    skip_persist: bool = True
    skip_ref_resolution: bool = True

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        path: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"写入 {path}...", "detail": None})
        if not path:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("必须提供 path 参数")
        p = Path(path)
        if p.is_dir():
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"目标是目录，不能作为文件写入: {p}")
        created = not p.exists()
        try:
            if not content.endswith("\n") and content:
                content += "\n"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"写入失败: {exc}")
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(
            success=True,
            data={"written": str(p), "chars": len(content), "created": created},
        )

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class EditTool:
    # 自声明：本工具的 path 参数受会话路径白名单（PathPolicyGuard）管辖，
    # 作用域 = 会话自有空间（记忆目录 + 会话工作区）
    path_policy = "session"
    """Replace an exact, unique occurrence within a file."""

    name: str = "edit"
    display_name: str | None = "编辑文件"
    description: str = (
        "精确替换文件中的一段内容：old_string 必须在文件中恰好出现一次。"
        "适合修改记忆文件的单条记录而不重写整文件；"
        "修改记忆条目后记得同步更新 MEMORY.md 索引。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件的绝对路径"},
            "old_string": {"type": "string", "description": "要被替换的原文（须唯一匹配）"},
            "new_string": {"type": "string", "description": "替换后的内容"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    skip_persist: bool = True
    skip_ref_resolution: bool = True

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        path: str = "",
        old_string: str = "",
        new_string: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"编辑 {path}...", "detail": None})
        if not path:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("必须提供 path 参数")
        if not old_string:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("必须提供 old_string 参数（不能为空串）")
        if old_string == new_string:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("old_string 与 new_string 相同，无需编辑")
        p = Path(path)
        if not p.exists() or not p.is_file():
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"文件不存在或不是常规文件: {p}")
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"读取失败: {exc}")

        count = text.count(old_string)
        if count == 0:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err("old_string 在文件中未找到，请先用 read 确认原文")
        if count > 1:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"old_string 匹配 {count} 处，需要唯一匹配——请加入更多上下文使其唯一")

        try:
            p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        except OSError as exc:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return _err(f"写入失败: {exc}")
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data={"edited": str(p), "replacements": 1})

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)
