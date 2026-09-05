"""Unit tests for the HealthCheck HTTP server.

Verifies the /health endpoint returns:
  * HTTP 200 when the Socket Mode WebSocket is connected
  * HTTP 503 when the WebSocket is disconnected
  * a body reporting per-backend health, with a backend whose
    health_check() exceeds the 500ms budget reported as "timeout"
    (informational only — it does not change the HTTP status).

Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import make_mocked_request

from slack_agent_router.health import HealthCheck

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(*, connected: bool) -> MagicMock:
    """Build a mock SlackAgentApp with a given WebSocket connection state.

    HealthCheck determines healthy/unhealthy by awaiting ``app.is_connected()``.
    """
    app = MagicMock()
    app.is_connected = AsyncMock(return_value=connected)
    return app


def _make_backend(name: str, *, healthy: bool = True, delay: float = 0.0) -> MagicMock:
    """Build a mock backend with a ``name`` property and async ``health_check()``.

    ``delay`` lets a test simulate a slow backend that should trip the
    per-backend 500ms timeout.
    """
    backend = MagicMock()
    # ``name`` is a property on real backends; a plain attribute is fine here.
    backend.name = name

    async def _health_check() -> bool:
        if delay:
            await asyncio.sleep(delay)
        return healthy

    backend.health_check = AsyncMock(side_effect=_health_check)
    return backend


def _request() -> Any:
    """A minimal mocked GET /health aiohttp request."""
    return make_mocked_request("GET", "/health")


async def _body_json(response: Any) -> dict[str, Any]:
    """Parse an aiohttp Response body into a dict."""
    return json.loads(response.body.decode("utf-8") if isinstance(response.body, bytes) else response.text)


# ---------------------------------------------------------------------------
# WebSocket connection status → HTTP status code
# ---------------------------------------------------------------------------


class TestWebSocketStatus:
    """The HTTP status reflects the Socket Mode WebSocket connection."""

    async def test_returns_200_when_connected(self) -> None:
        """WebSocket connected → HTTP 200 healthy."""
        health = HealthCheck(app=_make_app(connected=True), backends=[])
        response = await health.handle(_request())
        assert response.status == 200
        body = await _body_json(response)
        assert body["status"] == "healthy"
        assert body["websocket"] == "connected"

    async def test_returns_503_when_disconnected(self) -> None:
        """WebSocket disconnected → HTTP 503 unhealthy."""
        health = HealthCheck(app=_make_app(connected=False), backends=[])
        response = await health.handle(_request())
        assert response.status == 503
        body = await _body_json(response)
        assert body["status"] != "healthy"
        assert body["websocket"] == "disconnected"


# ---------------------------------------------------------------------------
# Backend health reporting (informational)
# ---------------------------------------------------------------------------


class TestBackendHealthReporting:
    """Backend status is reported in the body but does not change HTTP status."""

    async def test_healthy_backend_reported_ok(self) -> None:
        backend = _make_backend("Atlassian Rovo", healthy=True)
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        response = await health.handle(_request())
        body = await _body_json(response)
        assert body["backends"]["Atlassian Rovo"] == "ok"

    async def test_unhealthy_backend_reported_error(self) -> None:
        """A reachable-but-unhealthy backend is 'error' yet HTTP stays 200."""
        backend = _make_backend("Atlassian Rovo", healthy=False)
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        response = await health.handle(_request())
        assert response.status == 200  # backend down is degraded, not unhealthy
        body = await _body_json(response)
        assert body["backends"]["Atlassian Rovo"] == "error"

    async def test_backend_timeout_reported_as_timeout(self) -> None:
        """A backend slower than the 500ms budget is reported as 'timeout'."""
        # 5s delay is well beyond the 0.5s per-backend timeout.
        backend = _make_backend("Atlassian Rovo", healthy=True, delay=5.0)
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        response = await health.handle(_request())
        body = await _body_json(response)
        assert body["backends"]["Atlassian Rovo"] == "timeout"

    async def test_backend_exception_reported_as_error(self) -> None:
        """A backend whose health_check raises is reported as 'error', not crash."""
        backend = MagicMock()
        backend.name = "Atlassian Rovo"
        backend.health_check = AsyncMock(side_effect=RuntimeError("boom"))
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        response = await health.handle(_request())
        assert response.status == 200
        body = await _body_json(response)
        assert body["backends"]["Atlassian Rovo"] == "error"

    async def test_timeout_does_not_change_http_status(self) -> None:
        """Even with a timed-out backend, connected WebSocket → HTTP 200."""
        backend = _make_backend("Atlassian Rovo", healthy=True, delay=5.0)
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        response = await health.handle(_request())
        assert response.status == 200

    async def test_health_check_completes_quickly_with_slow_backend(self) -> None:
        """The per-backend timeout keeps the endpoint fast despite a slow backend."""
        backend = _make_backend("Atlassian Rovo", healthy=True, delay=5.0)
        health = HealthCheck(app=_make_app(connected=True), backends=[backend])
        elapsed = await _timed(health.handle(_request()))
        # 0.5s budget + overhead; must not wait the full 5s backend delay.
        assert elapsed < 2.0


async def _timed(coro: Any) -> float:
    """Await *coro* and return the elapsed wall-clock seconds."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    await coro
    return loop.time() - start
