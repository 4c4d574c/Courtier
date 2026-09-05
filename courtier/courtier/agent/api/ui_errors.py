"""Structured UI error responses.

HTTP/SSE 面向用户的错误不再承载后端文案：路由抛 ``UiError``，detail 序列化为
``{"code", "params"}``，前端按 code 从文案表渲染（未知名回退域级通用文案）。
模型可见的错误（ExecutionResult）仍走 ``render_error`` 文本渲染，不受影响。

迁移模式（按路由组分批）::

    raise UiError("auth.invalid_credentials")
    raise UiError("auth.account_locked", seconds=598)
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

#: Registry of every code the backend may emit — the frontend messages table
#: must cover these (unknown codes fall back to a domain-generic message).
UI_ERROR_CODES: dict[str, str] = {
    code: domain
    for domain, codes in {
        "auth": (
            "auth.invalid_credentials",
            "auth.account_locked",
            "auth.pending_approval",
            "auth.disabled",
            "auth.username_taken",
            "auth.missing_token",
            "auth.token_expired",
            "auth.token_invalid",
            "auth.token_missing_sub",
            "auth.unsupported_algorithm",
            "auth.secret_not_configured",
        ),
        "profile": (
            "profile.current_password_required",
            "profile.wrong_current_password",
            "profile.no_fields",
        ),
        "session": (
            "session.task_required",
            "session.not_found",
            "session.edit_turn_requires_session",
            "session.running_conflict",
        ),
        "setup": (
            "setup.not_db_mode",
            "setup.key_required",
            "setup.invalid_username",
            "setup.already_done",
        ),
    }.items()
    for code in codes
}


class UiError(HTTPException):
    """HTTPException whose detail is a structured ``{code, params}`` object."""

    def __init__(self, status_code: int, code: str, /, **params: Any) -> None:
        super().__init__(status_code, {"code": code, "params": params})
