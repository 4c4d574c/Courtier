"""Shared rate-limiter instance.

Moved to a standalone module to break the circular import between app.py and
routes/__init__.py (which imports routes/sessions.py, which needs the limiter).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


@lru_cache(maxsize=4)
def _trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the ``trusted_proxies`` setting into networks (invalid entries ignored)."""
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def client_ip(request: Request) -> str:
    """Best-effort real client address.

    Direct-peer address by default.  Only when the peer itself is a
    configured trusted proxy do we look at ``X-Forwarded-For`` — and take
    the *rightmost* entry, the one our own proxy appended; client-supplied
    entries further left are ignored, so header spoofing cannot mint new
    rate-limit buckets.
    """
    peer = request.client.host if request.client else ""
    if not peer:
        return ""
    raw = ""
    try:
        from courtier.config import get_settings

        raw = get_settings().trusted_proxies
    except Exception:
        return peer
    networks = _trusted_networks(raw)
    if not networks:
        return peer
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_addr in net for net in networks):
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    candidates = [c.strip() for c in xff.split(",") if c.strip()]
    return candidates[-1] if candidates else peer


def user_or_ip_key(request: Request) -> str:
    """Rate-limit bucket key: authenticated username, falling back to IP.

    All users behind a reverse proxy / NAT share one IP — IP-only keying made
    one user's burst exhaust everyone's budget. ``get_current_user`` stamps
    ``request.state.auth_user`` before the endpoint (and thus the limiter
    decorator) runs; unauthenticated routes (login, register, setup) never
    have it and keep IP keying.  Behind a trusted proxy the real client IP
    comes from X-Forwarded-For (see ``client_ip``).
    """
    user = getattr(request.state, "auth_user", None)
    if isinstance(user, str) and user:
        return f"user:{user}"
    return client_ip(request) or get_remote_address(request)


limiter = Limiter(key_func=user_or_ip_key)
