"""Session service — simple CRUD wrappers around SessionStore."""

from __future__ import annotations

from typing import Any, cast


async def list_sessions(
    store: Any,
    current_user: str,
    is_admin: bool = False,
    skip: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List sessions visible to the current user, most recent first."""
    return cast(list[dict[str, Any]], await store.list_all(
        current_user=current_user, is_admin=is_admin, skip=skip, limit=limit
    ))


async def get_session(
    store: Any, session_id: str, current_user: str, is_admin: bool = False
) -> dict[str, Any] | None:
    """Get full detail for a session, or None if not found or not owned."""
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        return None
    return cast(dict[str, Any], session.to_detail_dict())


async def delete_session(
    store: Any, session_id: str, current_user: str, is_admin: bool = False
) -> bool:
    """Delete a session if the current user is allowed to access it."""
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        return False
    return cast(bool, await store.delete(session_id))
