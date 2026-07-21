"""Template loading tool — wrap templates subpackage for DB-backed template loading."""

from __future__ import annotations

from typing import Any

from courtier.agent.tools.protocol import ToolResult


class LoadTemplateTool:
    """Load a format template from the database."""

    name: str = "load_template"
    display_name: str | None = "加载模板"
    description: str = (
        "Load a format template from the database by document type and optional "
        "template ID. Returns the template as a dict, or None if not found."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "description": "Document type to load template for (e.g. 通知).",
            },
            "template_id": {
                "type": "integer",
                "description": (
                    "Optional specific template ID. If omitted, loads the default "
                    "template."
                ),
            },
        },
        "required": ["doc_type"],
    }

    def __init__(self, db: Any = None) -> None:
        self._db = db

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            if self._db is None:
                return ToolResult(
                    success=False,
                    error=(
                        "Database backend not configured. "
                        "Pass an AsyncDatabase instance as `db=` when creating "
                        "LoadTemplateTool, e.g. LoadTemplateTool(db=async_db). "
                        "If using the chat agent, template loading is not available "
                        "without a database connection; use the audit pipeline instead."
                    ),
                )
            from validator.templates.crud import load_template_from_db

            doc_type = kwargs["doc_type"]
            template_id = kwargs.get("template_id")
            async with self._db.session() as session:
                result = await load_template_from_db(
                    session, doc_type=doc_type, template_id=template_id
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


def create_load_template_tool(db: Any = None) -> LoadTemplateTool:
    return LoadTemplateTool(db=db)
