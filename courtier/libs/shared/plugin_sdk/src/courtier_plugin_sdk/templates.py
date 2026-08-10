"""Plugin-side format-template client.

Loads format templates from the host's database via the ``template_store``
host service.  Database credentials stay in the host process — plugins
only see the template content dict.
"""

from __future__ import annotations

from typing import Any, cast

from .protocol import METHOD_TEMPLATE_STORE_GET


class HostTemplateStore:
    """Remote format-template client for plugins.

    Wraps a ``HostServiceClient``; requires the plugin manifest to declare
    ``host_services: [template_store]`` and ``permissions: [read:templates]``.
    """

    def __init__(self, host_client: Any) -> None:
        self._client = host_client

    async def get(self, doc_type: str, template_id: int | None = None) -> dict[str, Any] | None:
        """Load the template for *doc_type* (or a specific *template_id*).

        Returns the template content dict, or None when no matching
        template exists.
        """
        params: dict[str, Any] = {"doc_type": doc_type}
        if template_id is not None:
            params["template_id"] = template_id
        return cast(
            dict[str, Any] | None,
            await self._client.call(METHOD_TEMPLATE_STORE_GET, params),
        )
