"""Application entrypoint for the Slack Agent Router.

Loads secrets from AWS Secrets Manager, reads configuration from a
JSON/YAML file (with environment variable overrides), initialises
all components, and starts the health check server and Socket Mode
listener concurrently on a single asyncio event loop.

Secrets (sensitive credentials) live in Secrets Manager.
Configuration (non-sensitive identifiers) live in a config file
and/or environment variables. Env vars override file values.

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
# Configuration (non-sensitive, from file + environment overrides)
# ------------------------------------------------------------------

_CONFIG_FILE_ENV = "SLACK_AGENT_ROUTER_CONFIG"
_DEFAULT_CONFIG_PATHS = ("config.yaml", "config.json")

_ATLASSIAN_SERVICE_USER_ENV = "ATLASSIAN_SERVICE_USER"
_ROVO_MCP_SERVER_URL_ENV = "ROVO_MCP_SERVER_URL"
_ATLASSIAN_CLOUD_ID_ENV = "ATLASSIAN_CLOUD_ID"
_BEDROCK_AGENT_ID_ENV = "BEDROCK_AGENT_ID"
_BEDROCK_AGENT_ALIAS_ID_ENV = "BEDROCK_AGENT_ALIAS_ID"
_SLACK_AGENT_ROUTER_SECRET_ID_ENV = "SLACK_AGENT_ROUTER_SECRET_ID"  # pragma: allowlist secret

# Maps AppConfig field name → environment variable name.
_ENV_MAP: dict[str, str] = {
    "atlassian_service_user": _ATLASSIAN_SERVICE_USER_ENV,
    "rovo_mcp_server_url": _ROVO_MCP_SERVER_URL_ENV,
    "atlassian_cloud_id": _ATLASSIAN_CLOUD_ID_ENV,
    "bedrock_agent_id": _BEDROCK_AGENT_ID_ENV,
    "bedrock_agent_alias_id": _BEDROCK_AGENT_ALIAS_ID_ENV,
    "slack_agent_router_secret_id": _SLACK_AGENT_ROUTER_SECRET_ID_ENV,
}


@dataclass(frozen=True)
class AppConfig:
    """Non-sensitive configuration loaded from a config file and/or environment variables."""

    atlassian_service_user: str
    rovo_mcp_server_url: str
    atlassian_cloud_id: str
    bedrock_agent_id: str
    bedrock_agent_alias_id: str
    slack_agent_router_secret_id: str


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from a JSON/YAML file with environment variable overrides.

    Resolution order (last wins):
    1. Config file (JSON or YAML)
    2. Environment variables

    The config file path is resolved as:
    1. Explicit ``config_path`` argument
    2. ``SLACK_AGENT_ROUTER_CONFIG`` environment variable
    3. First existing file from ``config.yaml``, ``config.json`` in the
       current working directory

    A config file is optional — environment variables alone are sufficient.

    Environment variables:

    * ``ROVO_MCP_SERVER_URL``    — Rovo MCP Server endpoint URL
    * ``ATLASSIAN_CLOUD_ID``     — Atlassian Cloud instance ID
    * ``BEDROCK_AGENT_ID``       — Amazon Bedrock Agent ID
    * ``BEDROCK_AGENT_ALIAS_ID`` — Amazon Bedrock Agent alias ID
    * ``SLACK_AGENT_ROUTER_SECRET_ID`` — Secrets Manager secret name or ARN

    Raises:
        RuntimeError: If any required config value is missing after
            merging file and environment sources, or if the config
            file exists but cannot be parsed.
    """
    # --- Load base values from config file --------------------------
    file_values = _load_config_file(config_path)

    # --- Apply environment variable overrides -----------------------
    values: dict[str, str] = {}
    for field_name, env_var in _ENV_MAP.items():
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            values[field_name] = env_value
        elif field_name in file_values and file_values[field_name]:
            values[field_name] = str(file_values[field_name]).strip()

    # --- Validate all required fields are present -------------------
    missing: list[str] = []
    for field_name, env_var in _ENV_MAP.items():
        if not values.get(field_name):
            missing.append(env_var)

    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

    return AppConfig(**values)


def _load_config_file(config_path: str | None = None) -> dict[str, Any]:
    """Load and parse a JSON or YAML config file.

    Returns an empty dict if no config file is found (env-only mode).

    Raises:
        RuntimeError: If an explicit path is given but the file doesn't
            exist or can't be parsed.
    """
    import pathlib

    resolved = _resolve_config_path(config_path)
    if resolved is None:
        return {}

    path = pathlib.Path(resolved)
    if not path.is_file():
        # Only raise if the path was explicitly specified
        if config_path or os.environ.get(_CONFIG_FILE_ENV):
            raise RuntimeError(f"Config file not found: {resolved}")
        return {}

    text = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        return _parse_yaml(text, resolved)
    if path.suffix == ".json":
        return _parse_json(text, resolved)

    # Try JSON first, fall back to YAML for extensionless files
    try:
        return _parse_json(text, resolved)
    except RuntimeError:
        return _parse_yaml(text, resolved)


def _resolve_config_path(config_path: str | None) -> str | None:
    """Determine which config file to load, if any."""
    import pathlib

    if config_path:
        return config_path

    env_path = os.environ.get(_CONFIG_FILE_ENV, "").strip()
    if env_path:
        return env_path

    for default in _DEFAULT_CONFIG_PATHS:
        if pathlib.Path(default).is_file():
            return default

    return None


def _parse_json(text: str, path: str) -> dict[str, Any]:
    """Parse JSON config text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file {path} must contain a JSON object, got {type(data).__name__}")
    return data


def _parse_yaml(text: str, path: str) -> dict[str, Any]:
    """Parse YAML config text."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML in config file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file {path} must contain a YAML mapping, got {type(data).__name__}")
    return data


# ------------------------------------------------------------------
# Secrets loading (sensitive credentials from Secrets Manager)
# ------------------------------------------------------------------


async def load_secrets(secret_id: str) -> dict[str, Any]:
    """Load sensitive credentials from AWS Secrets Manager.

    The secret is expected to be a JSON object with the following keys:

    * ``slack_bot_token``      — Slack Bot User OAuth Token (xoxb-…)
    * ``slack_app_token``      — Slack App-Level Token for Socket Mode (xapp-…)
    * ``atlassian_api_token``  — Atlassian API token for Rovo MCP

    Args:
        slack_agent_router_secret_id: Secrets Manager secret name or ARN. Typically
            comes from ``AppConfig.slack_agent_router_secret_id``.

    Returns:
        Parsed secret as a dictionary.

    Raises:
        RuntimeError: If the secret cannot be retrieved or parsed.
    """

    try:
        raw = await asyncio.to_thread(_get_secret_value, secret_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to load secrets from Secrets Manager: {exc}") from exc

    try:
        secrets = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Secret value is not valid JSON: {exc}") from exc

    if not isinstance(secrets, dict):
        raise RuntimeError(f"Secret value must be a JSON object, got {type(secrets).__name__}")

    _validate_required_secret_keys(secrets)
    return secrets


_REQUIRED_SECRET_KEYS = (
    "slack_bot_token",
    "slack_app_token",
    "atlassian_api_token",
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
    Uses a custom JSON formatter that properly escapes all fields
    so output is always valid JSON regardless of message content.
    """
    import json as _json

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    class _JsonFormatter(logging.Formatter):
        """Emit each log record as a single valid JSON line."""

        def format(self, record: logging.LogRecord) -> str:
            entry = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info and record.exc_info[0] is not None:
                entry["exception"] = self.formatException(record.exc_info)
            return _json.dumps(entry, default=str)

    root = logging.getLogger()
    root.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(_JsonFormatter())

    # Avoid duplicate handlers on repeated calls (e.g. in tests)
    root.handlers.clear()
    root.addHandler(handler)


# ------------------------------------------------------------------
# Component wiring
# ------------------------------------------------------------------


async def main() -> None:
    """Application entrypoint — load config + secrets, wire components, start services."""
    _configure_logging()
    logger.info("Starting Slack Agent Router")

    config = load_config()
    secrets = await load_secrets(config.slack_agent_router_secret_id)

    # --- Backends ---------------------------------------------------
    from slack_agent_router.backends.rovo import RovoMCPBackend

    rovo_backend = RovoMCPBackend(
        mcp_server_url=config.rovo_mcp_server_url,
        api_token=secrets["atlassian_api_token"],
        cloud_id=config.atlassian_cloud_id,
        service_user=config.atlassian_service_user,
    )

    backends = [rovo_backend]

    # --- Orchestrator -----------------------------------------------
    from slack_agent_router.orchestrator import BedrockAgentOrchestrator

    orchestrator = BedrockAgentOrchestrator(
        agent_id=config.bedrock_agent_id,
        agent_alias_id=config.bedrock_agent_alias_id,
        rovo_backend=rovo_backend,
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
        logger.info("Shutting down Slack Agent Router")
        await app.stop()
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: loop.create_task(_shutdown()))

    # --- Start services concurrently --------------------------------
    tasks: list[asyncio.Task[None]] = []

    if health_server is not None:
        tasks.append(asyncio.create_task(health_server.start(), name="health_server"))

    tasks.append(asyncio.create_task(app.start(), name="slack_app"))

    # Monitor task: triggers shutdown_event if a service task fails
    async def _watch_tasks() -> None:
        """Exit promptly if any service task raises an exception."""
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception() is not None:
                logger.error(
                    "Service task %s failed: %s",
                    task.get_name(),
                    task.exception(),
                )
                shutdown_event.set()

    watcher = asyncio.create_task(_watch_tasks(), name="task_watcher")

    logger.info("Slack Agent Router is running")

    # Wait until a shutdown signal or a task failure
    await shutdown_event.wait()

    # Cancel all tasks (services + watcher)
    watcher.cancel()
    for task in tasks:
        task.cancel()

    # Allow tasks to finish cancellation
    await asyncio.gather(watcher, *tasks, return_exceptions=True)

    logger.info("Slack Agent Router stopped")


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
        logger.warning("HealthCheck module not available — skipping health server")
        return None


# ------------------------------------------------------------------
# Script entry
# ------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
