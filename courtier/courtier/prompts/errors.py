"""Unified rendering for model-facing error text.

Every error string the host core writes into ``ExecutionResult.error`` /
``ToolResult.error`` (loop, registry, proxies, builtin tools, sub-agent
runtime, artifact binding) is rendered from an ``errors.*`` template key so
the language follows the configured locale instead of being hardcoded at the
raise site.  Plugin-internal error text is out of scope: plugins are separate
processes whose business errors are passed through verbatim.
"""

from __future__ import annotations

import os
import threading
from typing import Any

_engine = None
_engine_lock = threading.Lock()


def _default_engine():
    """Process-wide engine built from the core defaults of the env locale.

    Mirrors ``CourtierConfig.from_env`` locale resolution (COURTIER_LOCALE,
    zh-CN default) so framework errors use the same language as the prompts
    rendered for the session.  Domain overrides of ``errors.*`` keys are not
    applied here — no domain directories are merged.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from .engine import PromptEngine

                locale = os.getenv("COURTIER_LOCALE", "zh-CN")
                _engine = PromptEngine.from_domain_directories([], locale=locale)
    return _engine


def render_error(key: str, /, *, engine: Any | None = None, **params: Any) -> str:
    """Render an ``errors.*`` template key with the given parameters.

    Falls back to the process-wide engine when *engine* is None.  Returns the
    key itself when nothing could be rendered — the model should never see an
    empty error string.
    """
    resolved = engine if engine is not None else _default_engine()
    try:
        text = resolved.render(key, **params).strip()
    except Exception:
        return key
    return text if text else key
