"""HTTP health check server for ECS container health checks.

Exposes a lightweight ``/health`` endpoint used by the ECS container
health check (``curl http://localhost:8080/health``). The HTTP status
reflects the Socket Mode WebSocket connection: 200 when connected, 503
when disconnected. The response body additionally reports per-backend
reachability, which is informational only — a backend being down is a
degraded state, not an unhealthy one, so it does not change the status
code.

Each backend health probe is bounded by a 500ms timeout so the endpoint
stays fast even when a backend hangs; a timed-out probe is reported as
``"timeout"`` in the body.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Per-backend health probe budget. The overall endpoint aims to stay
# under ~500ms; each backend probe is capped so one slow backend cannot
# stall the response.
_BACKEND_HEALTH_TIMEOUT_SECONDS = 0.5

_HEALTH_PATH = "/health"
_DEFAULT_PORT = 8080


class HealthCheck:
    """HTTP health check server for ECS container health checks.

    Args:
        app: The :class:`~slack_agent_router.slack_app.SlackAgentApp`.
            Its async ``is_connected()`` determines healthy/unhealthy.
        backends: Backends to probe for reachability. Each must expose a
            ``name`` and an async ``health_check() -> bool``.
        port: TCP port to listen on (default 8080).
    """

    def __init__(
        self,
        app: Any,
        backends: list[Any],
        port: int = _DEFAULT_PORT,
    ) -> None:
        self._app = app
        self._backends = backends
        self._port = port
        self._runner: web.AppRunner | None = None

    async def handle(self, request: web.Request) -> web.Response:
        """Handle a GET /health request.

        Returns HTTP 200 when the Socket Mode WebSocket is connected and
        HTTP 503 when it is disconnected. The body reports the WebSocket
        status and per-backend reachability (informational only).
        """
        connected = await self._is_connected()
        backend_status = await self._collect_backend_status()

        body = {
            "status": "healthy" if connected else "unhealthy",
            "websocket": "connected" if connected else "disconnected",
            "backends": backend_status,
        }
        status_code = 200 if connected else 503
        return web.json_response(body, status=status_code)

    async def start(self) -> None:
        """Start the health check HTTP server on the configured port."""
        server = web.Application()
        server.router.add_get(_HEALTH_PATH, self.handle)

        self._runner = web.AppRunner(server)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)  # noqa: S104
        await site.start()
        logger.info("Health check server listening on port %d%s", self._port, _HEALTH_PATH)

    async def stop(self) -> None:
        """Stop the health check server and release its resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _is_connected(self) -> bool:
        """Return whether the Socket Mode WebSocket is connected.

        Any error querying the app is treated as disconnected (fail
        unhealthy) so the container is recycled rather than left in an
        unknown state.
        """
        try:
            return bool(await self._app.is_connected())
        except Exception as exc:
            logger.warning("Failed to determine WebSocket connection status: %s", exc)
            return False

    async def _collect_backend_status(self) -> dict[str, str]:
        """Probe every backend's health, bounding each probe by a timeout.

        Returns a mapping of backend name to ``"ok"``, ``"error"``, or
        ``"timeout"``. Probes run concurrently so total time is bounded by
        the single-probe timeout rather than the sum of all backends.
        """
        if not self._backends:
            return {}

        results = await asyncio.gather(
            *(self._probe_backend(backend) for backend in self._backends),
            return_exceptions=False,
        )
        return dict(results)

    async def _probe_backend(self, backend: Any) -> tuple[str, str]:
        """Probe a single backend, returning ``(name, status)``.

        ``status`` is ``"ok"`` when ``health_check()`` returns truthy,
        ``"error"`` when it returns falsy or raises, and ``"timeout"``
        when it exceeds the per-backend budget.
        """
        name = getattr(backend, "name", backend.__class__.__name__)
        try:
            healthy = await asyncio.wait_for(
                backend.health_check(),
                timeout=_BACKEND_HEALTH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug("Backend %s health check timed out", name)
            return name, "timeout"
        except Exception as exc:
            logger.debug("Backend %s health check errored: %s", name, exc)
            return name, "error"
        return name, "ok" if healthy else "error"
