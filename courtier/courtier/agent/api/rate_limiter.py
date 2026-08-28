"""Shared rate-limiter instance.

Moved to a standalone module to break the circular import between app.py and
routes/__init__.py (which imports routes/sessions.py, which needs the limiter).
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def user_or_ip_key(request: Request) -> str:
    """Rate-limit bucket key: authenticated username, falling back to IP.

    All users behind a reverse proxy / NAT share one IP — IP-only keying made
    one user's burst exhaust everyone's budget. ``get_current_user`` stamps
    ``request.state.auth_user`` before the endpoint (and thus the limiter
    decorator) runs; unauthenticated routes (login, register, setup) never
    have it and keep IP keying.
    """
    user = getattr(request.state, "auth_user", None)
    if isinstance(user, str) and user:
        return f"user:{user}"
    return get_remote_address(request)


limiter = Limiter(key_func=user_or_ip_key)
