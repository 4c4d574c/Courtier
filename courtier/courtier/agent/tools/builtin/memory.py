"""Memory tools — explicit access to the MemoryManager tiers.

The registry passes the run-scoped context_manager into every tool's
execute() (same seam as artifact_store), so sub-agent forks with their
isolated session namespaces are honored automatically.  The tools are
registered by ``Agent._ensure_builtin_memory_tools`` when the context
manager actually exposes the memory tiers (MemoryManager).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from courtier.prompts.errors import render_error

from ..protocol import OnToolProgress, ToolResult

if TYPE_CHECKING:
    from ..summary import ToolSummary


_TIER_BY_SCOPE: dict[str, Literal["session", "long_term"]] = {
    "session": "session",
    "long_term": "long_term",
}


def _memory_manager(context_manager: Any) -> Any | None:
    """Return the manager only when it exposes the memory tiers."""
    if context_manager is None or not hasattr(context_manager, "session_get"):
        return None
    return context_manager


class MemorySaveTool:
    """Persist a memory into the session or long-term tier."""

    name: str = "memory_save"
    description: str = (
        "保存一条记忆。scope=session 仅当前会话可见（跨轮次保留）；"
        "scope=long_term 跨会话持久。只保存无法从当前工作重新推导的知识："
        "用户偏好、关键决定、重要结论、实体摘要等。键名使用语义化命名。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "语义化键名，如 user_style、q3_report_conclusion",
            },
            "value": {
                "description": "要保存的内容：字符串或结构化对象",
            },
            "scope": {
                "type": "string",
                "enum": ["session", "long_term"],
                "description": "session=当前会话；long_term=跨会话持久",
            },
        },
        "required": ["key", "value"],
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        context_manager: Any = None,
        key: str = "",
        value: Any = None,
        scope: str = "session",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"保存记忆 {key}...", "detail": None})
        mgr = _memory_manager(context_manager)
        if mgr is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=render_error("errors.memory_unavailable"))
        if not key:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_missing_param", tool_name="memory_save", param_name="key"
                ),
            )
        if scope == "long_term":
            await mgr.long_term_set(key, value)
        else:
            await mgr.session_set(key, value)
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data={"saved": True, "key": key, "scope": scope})

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class MemoryGetTool:
    """Read one memory by key."""

    name: str = "memory_get"
    description: str = (
        "按键名精确读取记忆。scope=session 读取当前会话，"
        "scope=long_term 读取跨会话长期记忆；键不存在时返回未找到而非报错。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "要读取的记忆键名"},
            "scope": {
                "type": "string",
                "enum": ["session", "long_term"],
                "description": "session=当前会话；long_term=跨会话持久",
            },
        },
        "required": ["key"],
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        context_manager: Any = None,
        key: str = "",
        scope: str = "session",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"读取记忆 {key}...", "detail": None})
        mgr = _memory_manager(context_manager)
        if mgr is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=render_error("errors.memory_unavailable"))
        if not key:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_missing_param", tool_name="memory_get", param_name="key"
                ),
            )
        if scope == "long_term":
            value = await mgr.long_term_get(key)
        else:
            value = await mgr.session_get(key)
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        if value is None:
            return ToolResult(
                success=True,
                data={"found": False, "key": key, "scope": scope, "value": None},
            )
        return ToolResult(
            success=True,
            data={"found": True, "key": key, "scope": scope, "value": value},
        )

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class MemoryDeleteTool:
    """Delete one memory by key."""

    name: str = "memory_delete"
    description: str = (
        "删除一条记忆（scope=session 当前会话；scope=long_term 长期记忆）。"
        "删除不存在的键是静默成功。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "要删除的记忆键名"},
            "scope": {
                "type": "string",
                "enum": ["session", "long_term"],
                "description": "session=当前会话；long_term=跨会话持久",
            },
        },
        "required": ["key"],
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        context_manager: Any = None,
        key: str = "",
        scope: str = "session",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"删除记忆 {key}...", "detail": None})
        mgr = _memory_manager(context_manager)
        if mgr is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=render_error("errors.memory_unavailable"))
        if not key:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_missing_param", tool_name="memory_delete", param_name="key"
                ),
            )
        if scope == "long_term":
            await mgr.long_term_delete(key)
        else:
            await mgr.session_delete(key)
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data={"deleted": True, "key": key, "scope": scope})

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class MemoryRecallTool:
    """Keyword-recall relevant memories across tiers."""

    name: str = "memory_recall"
    description: str = (
        "按关键词检索相关记忆（当前会话 + 长期记忆 + 当前上下文摘要），"
        "按相关度排序。query 必须包含具体检索词（不支持通配符或空查询）；"
        "适合回忆与当前话题相关的经验与结论。要查看现有记忆的完整清单，用 memory_list；"
        "已知确切键名时用 memory_get 更精确。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索文本（支持中英文关键词）"},
            "top_k": {
                "type": "integer",
                "description": "最多返回条数，默认 5",
            },
            "scope": {
                "type": "string",
                "enum": ["all", "session", "long_term"],
                "description": "all=全部层级（默认）；session / long_term=限定层级",
            },
        },
        "required": ["query"],
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        context_manager: Any = None,
        query: str = "",
        top_k: int = 5,
        scope: str = "all",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": "检索记忆...", "detail": None})
        mgr = _memory_manager(context_manager)
        if mgr is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=render_error("errors.memory_unavailable"))
        if not query:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_missing_param", tool_name="memory_recall", param_name="query"
                ),
            )
        from ...core.memory_manager import MemoryQuery, _extract_query_terms

        # A query with no extractable terms (wildcards, punctuation-only) would
        # silently return zero matches and mislead the model into "no memories
        # exist" — fail loudly instead so it can rephrase or use memory_list.
        if not _extract_query_terms(query):
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="query 中没有可检索的关键词，请给出具体的检索词；"
                "要查看现有记忆的完整清单，使用 memory_list 工具",
            )
        tier = _TIER_BY_SCOPE.get(scope)
        recalls = await mgr.retrieve(MemoryQuery(text=query, top_k=max(1, top_k), tier=tier))
        items = [
            {
                "key": r.key,
                "tier": r.tier,
                "score": round(r.score, 3),
                "value": r.value,
            }
            for r in recalls
        ]
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data={"matches": items, "count": len(items)})

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


class MemoryListTool:
    """List all stored memories (keys + short value preview) per tier."""

    name: str = "memory_list"
    description: str = (
        "列出现有记忆的完整清单（键名 + 值预览），scope=all 时同时列出"
        "当前会话记忆与跨会话长期记忆。回答「有哪些记忆」这类清单问题用本工具；"
        "按相关性模糊查找用 memory_recall；已知键名读取完整值用 memory_get。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["all", "session", "long_term"],
                "description": "all=全部层级（默认）；session / long_term=限定层级",
            },
        },
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        context_manager: Any = None,
        scope: str = "all",
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": "列出记忆...", "detail": None})
        mgr = _memory_manager(context_manager)
        if mgr is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=render_error("errors.memory_unavailable"))

        items: list[dict[str, Any]] = []
        if scope in ("all", "session"):
            for key in await mgr.session_keys():
                value = await mgr.session_get(key)
                items.append({"key": key, "tier": "session", "preview": _preview(value)})
        if scope in ("all", "long_term"):
            for key in await mgr.long_term_keys():
                value = await mgr.long_term_get(key)
                items.append({"key": key, "tier": "long_term", "preview": _preview(value)})

        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data={"items": items, "count": len(items)})

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


def _preview(value: Any, limit: int = 120) -> str:
    """Short value preview for list output; full values via memory_get."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + "…"
    return text
