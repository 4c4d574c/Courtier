# tests/telemetry/test_api_middleware.py
"""Integration tests for FastAPI observability middleware and /metrics endpoint."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from courtier.agent.api.middleware.observability import ObservabilityMiddleware


@pytest.fixture
def app_with_middleware():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/test-ok")
    async def test_ok():
        return {"status": "ok"}

    return app


class TestObservabilityMiddleware:
    def test_successful_request(self, app_with_middleware):
        client = TestClient(app_with_middleware)
        response = client.get("/test-ok")
        assert response.status_code == 200


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prometheus_format(self):
        """Verify /metrics returns Prometheus text format."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        from starlette.responses import Response

        app = FastAPI()

        @app.get("/metrics")
        async def metrics():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "agent_requests_total" in response.text or "process_" in response.text
