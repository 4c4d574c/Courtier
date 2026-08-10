"""Plugin-side object-storage client.

Uploads plugin-produced files (annotated documents, generated reports, …)
to the host's MinIO storage via the ``storage.put`` host service and returns
a presigned download URL.  MinIO credentials stay in the host process —
plugins only see the resulting URL.
"""

from __future__ import annotations

import base64
from typing import Any, cast

from .protocol import METHOD_STORAGE_PUT


class HostStorage:
    """Remote object-storage client for plugins.

    Wraps a ``HostServiceClient``; requires the plugin manifest to declare
    ``host_services: [storage]`` and ``permissions: [write:storage]``.
    """

    # Uploads carry whole files as base64 inside one JSON-RPC line.
    _PUT_TIMEOUT = 120.0

    def __init__(self, host_client: Any) -> None:
        self._client = host_client

    async def put(
        self,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Store *data* under *filename* and return the storage receipt.

        The receipt dict contains ``bucket``, ``object_key``,
        ``download_url`` (presigned), ``expires_in`` and ``size_bytes``.
        """
        return cast(
            dict[str, Any],
            await self._client.call(
                METHOD_STORAGE_PUT,
                {
                    "filename": filename,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                    "content_type": content_type,
                },
                timeout=self._PUT_TIMEOUT,
            ),
        )
