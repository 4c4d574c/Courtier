"""Host-side parameter injectors for the tool dispatch boundary.

A tool contract marks a schema property with ``"x-host-injected":
``<injector>`` to declare that the host — not the model — fills the
parameter at dispatch time (the mirror of the ``file-ref`` boundary
rewrite, which rewrites model-filled values).  Injectors registered on
the ToolRegistry under that name run after ``$ref`` resolution and
before the tool executes; a None return leaves the parameter unset so
the tool degrades exactly as it would without the optional input.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Schema marker: property annotation ``"x-host-injected": "<injector>"``.
HOST_INJECTED_MARKER = "x-host-injected"


async def embedding_injector(
    tool: Any, kwargs: dict[str, Any], param_name: str
) -> list[float] | None:
    """Embed the tool's ``query`` argument with the host embedding client.

    Returns None (parameter stays unset → tool runs lexical-only) when
    embedding is not configured, the call has no textual ``query``, or
    the embedding call fails.
    """
    from courtier.config import get_settings
    from courtier.es.embeddings import embed_query, embedding_enabled

    settings = get_settings()
    if not embedding_enabled(settings):
        return None
    query = kwargs.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    try:
        return await embed_query(settings, query)
    except Exception:
        logger.warning(
            "embedding injection failed for %s.%s; tool runs without the vector",
            getattr(tool, "name", "<tool>"),
            param_name,
            exc_info=True,
        )
        return None


def make_embedding_param_injector():
    """Return the ``embedding`` injector for ToolRegistry.configure_param_injectors."""
    return embedding_injector


def make_memory_caller_injector(
    owner_id: int | None, username: str = "", is_admin: bool = False
):
    """Return the ``memory_caller`` injector binding one session's identity.

    Fills the memory tool's ``_caller_uid`` / ``_caller_name`` /
    ``_caller_is_admin`` params at the dispatch boundary (the plugin only
    relays them to the host service).  Built per build_agent call with the
    session owner's identity captured in the closure — the model never
    sees or fills these params (schema-stripped, and model-supplied values
    are discarded by the registry before injection).
    """

    async def memory_caller_injector(
        tool: Any, kwargs: dict[str, Any], param_name: str
    ) -> int | str | bool | None:
        if param_name == "_caller_uid":
            return owner_id if owner_id is not None else 0
        if param_name == "_caller_name":
            return username
        if param_name == "_caller_is_admin":
            return is_admin
        return None

    return memory_caller_injector
