# src/agent/api/middleware/observability.py
"""HTTP-level observability middleware for FastAPI.

Records request count and latency per endpoint without touching
business logic.
"""

from __future__ import annotations

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from courtier.agent.telemetry.metrics import AGENT_REQUESTS_TOTAL


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Records HTTP request metrics.

    Does NOT create OTel spans — HTTP tracing is handled by the
    agent-level spans in agent_loop and orch.py, which carry richer
    business context (agent_name, session_id, etc.).
    """

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        status = "success" if response.status_code < 400 else "error"
        AGENT_REQUESTS_TOTAL.labels(
            agent_name="http",
            status=status,
        ).inc()

        return response
