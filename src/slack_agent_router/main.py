"""Application entrypoint for the Slack Agent Router.

Loads secrets from AWS Secrets Manager, reads configuration from
environment variables, initialises all components, and starts the
health check server and Socket Mode listener concurrently on a
single asyncio event loop.

Secrets (sensitive credentials) live in Secrets Manager.
Configuration (non-sensitive identifiers) live in environment variables.

Requirements: 14.5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration (non-sensitive, from environment variables)
# ------------------------------------------------------------------

_ROVO_MCP_SERVER_URL_ENV = "ROVO_MCP_SERVER_URL"
_ATLASSIAN_CLOUD_ID_ENV = "ATLASSIAN_CLOUD_ID"
_GCP_PROJECT_ID_ENV = "GCP_PROJECT_ID"
_VERTEX_LOCATION_ENV = "VERTEX_LOCATION"
_VERTEX_DATA_STORE_ID_ENV = "VERTEX_DATA_STORE_ID"
_BEDROCK_AGENT_ID_ENV = "BEDROCK_AGENT_ID"
_BEDROCK_AGENT_ALIAS_ID_ENV = "BEDROCK_AGENT_ALIAS_ID"


@dataclass(frozen=True)
class AppConfig:
    """Non-sensitive configuration loaded from environment variables."""

    rovo_mcp_server_url: str
    atlassian_cloud_id: str
    gcp_project_id: str
    vertex_location: str
    vertex_data_store_id: str
    bedrock_agent_id: str
    bedrock_agent_alias_id: str


def load_config() -> AppConfig:
    """Load non-sensitive configuration from environment variables.

    Environment variables:

    * ``ROVO_MCP_SERVER_URL``   — Rovo MCP Server endpoint URL
    * ``ATLASSIAN_CLOUD_ID``    — Atlassian Cloud instance ID
    * ``GCP_PROJECT_ID``        — GCP project hosting Vertex AI Search
    * ``VERTEX_LOCATION``       — Vertex AI Search location (e.g. "global")
    * ``VERTEX_DATA_STORE_ID``  — Vertex AI Search data store ID
    * ``BEDROCK_AGENT_ID``      — Amazon Bedrock Agent ID
    * ``BEDROCK_AGENT_ALIAS_ID`` — Amazon Bedrock Agent alias ID

    Raises:
        RuntimeError: If any required environment variable is missing.
    """
    env_map = {
        "rovo_mcp_server_url": _ROVO_MCP_SERVER_URL_ENV,
        "atlassian_cloud_id": _ATLASSIAN_CLOUD_ID_ENV,
        "gcp_project_id": _GCP_PROJECT_ID_ENV,
        "vertex_location": _VERTEX_LOCATION_ENV,
        "vertex_data_store_id": _VERTEX_DATA_STORE_ID_ENV,
        "bedrock_agent_id": _BEDROCK_AGENT_ID_ENV,
        "bedrock_agent_alias_id": _BEDROCK_AGENT_ALIAS_ID_ENV,
    }

    values: dict[str, str] = {}
    missing: list[str] = []

    for field_name, env_var in env_map.items():
        value = os.environ.get(env_var, "").strip()
        if not value:
            missing.append(env_var)
        else:
            values[field_name] = value

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return AppConfig(**values)


# ------------------------------------------------------------------
# Secrets loading (sensitive credentials from Secrets Manager)
# ------------------------------------------------------------------

_SECRET_ID_ENV = "SLACK_AGENT_ROUTER_SECRET_ID"


async def load_secrets(secret_id: str | None = None) -> dict[str, Any]:
    """Load sensitive credentials from AWS Secrets Manager.

    The secret is expected to be a JSON object with the following keys:

    * ``slack_bot_token``      — Slack Bot User OAuth Token (xoxb-…)
    * ``slack_app_token``      — Slack App-Level Token for Socket Mode (xapp-…)
    * ``atlassian_api_token``  — Atlassian API token for Rovo MCP
    * ``gcp_service_account``  — GCP service account credentials (JSON object)

    Args:
        secret_id: Secrets Manager secret name or ARN.  Falls back to
            the ``SLACK_AGENT_ROUTER_SECRET_ID`` environment variable.

    Returns:
        Parsed secret as a dictionary.

    Raises:
        RuntimeError: If the secret ID is not configured or the secret
            cannot be retrieved.
    """
    resolved_id = secret_id or os.environ.get(_SECRET_ID_ENV)
    if not resolved_id:
        raise RuntimeError(
            f"Secret ID not configured. Set the {_SECRET_ID_ENV} environment variable or pass secret_id explicitly."
        )

    try:
        raw = await asyncio.to_thread(_get_secret_value, resolved_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to load secrets from Secrets Manager: {exc}") from exc

    try:
        secrets = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Secret value is not valid JSON: {exc}") from exc

    # If gcp_service_account is stored as a JSON string rather than a
    # nested object, parse it into a dict so the Vertex backend can
    # consume it directly.
    gcp_sa = secrets.get("gcp_service_account")
    if isinstance(gcp_sa, str):
        try:
            secrets["gcp_service_account"] = json.loads(gcp_sa)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gcp_service_account is not valid JSON: {exc}") from exc

    _validate_required_secret_keys(secrets)
    return secrets


_REQUIRED_SECRET_KEYS = (
    "slack_bot_token",
    "slack_app_token",
    "atlassian_api_token",
    "gcp_service_account",
)


def _validate_required_secret_keys(secrets: dict[str, Any]) -> None:
    """Raise if any required secret key is missing or empty."""
    missing = [k for k in _REQUIRED_SECRET_KEYS if not secrets.get(k)]
    if missing:
        raise RuntimeError(f"Missing required secret keys: {', '.join(missing)}")


def _get_secret_value(secret_id: str) -> str:
    """Synchronous boto3 call to retrieve a secret (runs in a thread)."""
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    return response["SecretString"]


# ------------------------------------------------------------------
# Logging configuration
# ------------------------------------------------------------------


def _configure_logging() -> None:
    """Set up structured JSON logging to stdout.

    ECS Fargate captures stdout/stderr and ships it to CloudWatch.
    Using a JSON formatter makes logs machine-parseable for
    CloudWatch Logs Insights queries.
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    formatter = logging.Formatter(
        '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":%(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    # Avoid duplicate handlers on repeated calls (e.g. in tests)
    root.handlers.clear()
    root.addHandler(handler)


# ------------------------------------------------------------------
# Component wiring
# ------------------------------------------------------------------


async def main() -> None:
    """Application entrypoint — load config + secrets, wire components, start services."""
    _configure_logging()
    logger.info('"Starting Slack Agent Router"')

    config = load_config()
    secrets = await load_secrets()

    # --- Backends ---------------------------------------------------
    from slack_agent_router.backends.rovo import RovoMCPBackend
    from slack_agent_router.backends.vertex import VertexAISearchBackend

    rovo_backend = RovoMCPBackend(
        mcp_server_url=config.rovo_mcp_server_url,
        api_token=secrets["atlassian_api_token"],
        cloud_id=config.atlassian_cloud_id,
    )

    vertex_backend = VertexAISearchBackend(
        project_id=config.gcp_project_id,
        location=config.vertex_location,
        data_store_id=config.vertex_data_store_id,
        service_account_credentials=secrets["gcp_service_account"],
    )

    backends = [rovo_backend, vertex_backend]

    # --- Orchestrator -----------------------------------------------
    from slack_agent_router.orchestrator import BedrockAgentOrchestrator

    orchestrator = BedrockAgentOrchestrator(
        agent_id=config.bedrock_agent_id,
        agent_alias_id=config.bedrock_agent_alias_id,
        rovo_backend=rovo_backend,
        vertex_backend=vertex_backend,
    )

    # --- Rate limiter -----------------------------------------------
    from slack_agent_router.rate_limiter import RateLimiter

    rate_limiter = RateLimiter()

    # --- Slack app ---------------------------------------------------
    from slack_agent_router.slack_app import SlackAgentApp

    app = SlackAgentApp(
        bot_token=secrets["slack_bot_token"],
        app_token=secrets["slack_app_token"],
        orchestrator=orchestrator,
        rate_limiter=rate_limiter,
    )

    # --- Health check ------------------------------------------------
    # HealthCheck is not yet implemented (task 13). Import is deferred
    # and guarded so the entrypoint works for end-to-end testing now.
    health_server = await _create_health_check(app, backends)

    # --- Graceful shutdown -------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    async def _shutdown() -> None:
        logger.info('"Shutting down Slack Agent Router"')
        await app.stop()
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: loop.create_task(_shutdown()))

    # --- Start services concurrently --------------------------------
    tasks: list[asyncio.Task[None]] = []

    if health_server is not None:
        tasks.append(asyncio.create_task(health_server.start()))

    tasks.append(asyncio.create_task(app.start()))

    logger.info('"Slack Agent Router is running"')

    # Wait until a shutdown signal is received
    await shutdown_event.wait()

    # Cancel any remaining tasks
    for task in tasks:
        task.cancel()

    # Allow tasks to finish cancellation
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info('"Slack Agent Router stopped"')


async def _create_health_check(app: Any, backends: list[Any]) -> Any | None:
    """Try to create a HealthCheck server.

    Returns None if the HealthCheck module is not yet available
    (task 13). This lets the entrypoint work for end-to-end testing
    before the health check is implemented.
    """
    try:
        from slack_agent_router.health import HealthCheck

        return HealthCheck(app=app, backends=backends)
    except ImportError:
        logger.warning('"HealthCheck module not available — skipping health server"')
        return None


# ------------------------------------------------------------------
# Script entry
# ------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
