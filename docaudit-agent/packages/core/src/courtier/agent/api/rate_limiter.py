"""Shared rate-limiter instance.

Moved to a standalone module to break the circular import between app.py and
routes/__init__.py (which imports routes/sessions.py, which needs the limiter).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
