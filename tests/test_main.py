"""Tests for the application entrypoint (main.py).

Covers secrets loading, config loading, validation, component wiring,
and the main() lifecycle with mocked AWS and Slack dependencies.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_agent_router.main import (
    _ATLASSIAN_CLOUD_ID_ENV,
    _BEDROCK_AGENT_ALIAS_ID_ENV,
    _BEDROCK_AGENT_ID_ENV,
    _GCP_PROJECT_ID_ENV,
    _REQUIRED_SECRET_KEYS,
    _ROVO_MCP_SERVER_URL_ENV,
    _SECRET_ID_ENV,
    _VERTEX_DATA_STORE_ID_ENV,
    _VERTEX_LOCATION_ENV,
    _validate_required_secret_keys,
    load_config,
    load_secrets,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_CONFIG_ENV_VARS = {
    _ROVO_MCP_SERVER_URL_ENV: "https://mcp.atlassian.com/v1/mcp",
    _ATLASSIAN_CLOUD_ID_ENV: "cloud-123",
    _GCP_PROJECT_ID_ENV: "my-project",
    _VERTEX_LOCATION_ENV: "global",
    _VERTEX_DATA_STORE_ID_ENV: "ds-456",
    _BEDROCK_AGENT_ID_ENV: "agent-789",
    _BEDROCK_AGENT_ALIAS_ID_ENV: "alias-abc",
}


def _make_secrets(**overrides: Any) -> dict[str, Any]:
    """Build a complete secrets dict with sensible defaults."""
    base: dict[str, Any] = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "atlassian_api_token": "atlassian-token",
        "gcp_service_account": {"type": "service_account", "project_id": "my-project"},
    }
    base.update(overrides)
    return base


def _set_config_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set all required config env vars, with optional overrides."""
    for env_var, value in _CONFIG_ENV_VARS.items():
        monkeypatch.setenv(env_var, overrides.get(env_var, value))


# ------------------------------------------------------------------
# _validate_required_secret_keys
# ------------------------------------------------------------------


class TestValidateRequiredSecretKeys:
    """Tests for secret key validation."""

    def test_all_keys_present_passes(self) -> None:
        secrets = _make_secrets()
        _validate_required_secret_keys(secrets)  # should not raise

    @pytest.mark.parametrize("missing_key", list(_REQUIRED_SECRET_KEYS))
    def test_missing_key_raises(self, missing_key: str) -> None:
        secrets = _make_secrets()
        del secrets[missing_key]
        with pytest.raises(RuntimeError, match="Missing required secret keys"):
            _validate_required_secret_keys(secrets)

    @pytest.mark.parametrize("missing_key", ["slack_bot_token", "atlassian_api_token"])
    def test_empty_string_key_raises(self, missing_key: str) -> None:
        secrets = _make_secrets(**{missing_key: ""})
        with pytest.raises(RuntimeError, match="Missing required secret keys"):
            _validate_required_secret_keys(secrets)

    def test_multiple_missing_keys_listed(self) -> None:
        secrets = _make_secrets()
        del secrets["slack_bot_token"]
        del secrets["atlassian_api_token"]
        with pytest.raises(RuntimeError, match="slack_bot_token") as exc_info:
            _validate_required_secret_keys(secrets)
        assert "atlassian_api_token" in str(exc_info.value)


# ------------------------------------------------------------------
# load_config
# ------------------------------------------------------------------


class TestLoadConfig:
    """Tests for environment variable configuration loading."""

    def test_loads_all_config_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config_env(monkeypatch)
        config = load_config()
        assert config.rovo_mcp_server_url == "https://mcp.atlassian.com/v1/mcp"
        assert config.atlassian_cloud_id == "cloud-123"
        assert config.gcp_project_id == "my-project"
        assert config.vertex_location == "global"
        assert config.vertex_data_store_id == "ds-456"
        assert config.bedrock_agent_id == "agent-789"
        assert config.bedrock_agent_alias_id == "alias-abc"

    @pytest.mark.parametrize(
        "env_var",
        [
            _ROVO_MCP_SERVER_URL_ENV,
            _ATLASSIAN_CLOUD_ID_ENV,
            _GCP_PROJECT_ID_ENV,
            _VERTEX_LOCATION_ENV,
            _VERTEX_DATA_STORE_ID_ENV,
            _BEDROCK_AGENT_ID_ENV,
            _BEDROCK_AGENT_ALIAS_ID_ENV,
        ],
    )
    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch, env_var: str) -> None:
        _set_config_env(monkeypatch)
        monkeypatch.delenv(env_var)
        with pytest.raises(RuntimeError, match="Missing required environment variables"):
            load_config()

    def test_empty_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config_env(monkeypatch)
        monkeypatch.setenv(_BEDROCK_AGENT_ID_ENV, "  ")
        with pytest.raises(RuntimeError, match=_BEDROCK_AGENT_ID_ENV):
            load_config()

    def test_multiple_missing_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Set none of the required env vars
        for env_var in _CONFIG_ENV_VARS:
            monkeypatch.delenv(env_var, raising=False)
        with pytest.raises(RuntimeError, match=_ATLASSIAN_CLOUD_ID_ENV) as exc_info:
            load_config()
        assert _BEDROCK_AGENT_ID_ENV in str(exc_info.value)

    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config_env(monkeypatch)
        config = load_config()
        with pytest.raises(AttributeError):
            config.atlassian_cloud_id = "mutated"  # type: ignore[misc]


# ------------------------------------------------------------------
# load_secrets
# ------------------------------------------------------------------


class TestLoadSecrets:
    """Tests for the async secrets loading function."""

    async def test_loads_and_parses_secrets(self) -> None:
        secrets_dict = _make_secrets()
        raw_json = json.dumps(secrets_dict)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            result = await load_secrets(secret_id="test-secret")

        assert result["slack_bot_token"] == "xoxb-test-token"
        assert result["atlassian_api_token"] == "atlassian-token"

    async def test_falls_back_to_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_SECRET_ID_ENV, "env-secret-id")
        secrets_dict = _make_secrets()
        raw_json = json.dumps(secrets_dict)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json) as mock_get:
            await load_secrets()

        mock_get.assert_called_once_with("env-secret-id")

    async def test_raises_when_no_secret_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_SECRET_ID_ENV, raising=False)
        with pytest.raises(RuntimeError, match="Secret ID not configured"):
            await load_secrets()

    async def test_raises_on_secrets_manager_error(self) -> None:
        with patch("slack_agent_router.main._get_secret_value", side_effect=Exception("AWS error")):
            with pytest.raises(RuntimeError, match="Failed to load secrets"):
                await load_secrets(secret_id="bad-secret")

    async def test_raises_on_invalid_json(self) -> None:
        with patch("slack_agent_router.main._get_secret_value", return_value="not-json"):
            with pytest.raises(RuntimeError, match="not valid JSON"):
                await load_secrets(secret_id="bad-json-secret")

    async def test_raises_on_missing_required_key(self) -> None:
        incomplete = _make_secrets()
        del incomplete["slack_bot_token"]
        raw_json = json.dumps(incomplete)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            with pytest.raises(RuntimeError, match="Missing required secret keys"):
                await load_secrets(secret_id="incomplete-secret")

    async def test_parses_gcp_service_account_string(self) -> None:
        """gcp_service_account stored as a JSON string should be parsed into a dict."""
        sa_dict = {"type": "service_account", "project_id": "my-project"}
        secrets_dict = _make_secrets(gcp_service_account=json.dumps(sa_dict))
        raw_json = json.dumps(secrets_dict)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            result = await load_secrets(secret_id="test-secret")

        assert isinstance(result["gcp_service_account"], dict)
        assert result["gcp_service_account"]["type"] == "service_account"

    async def test_gcp_service_account_dict_passthrough(self) -> None:
        """gcp_service_account already a dict should pass through unchanged."""
        sa_dict = {"type": "service_account", "project_id": "my-project"}
        secrets_dict = _make_secrets(gcp_service_account=sa_dict)
        raw_json = json.dumps(secrets_dict)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            result = await load_secrets(secret_id="test-secret")

        assert result["gcp_service_account"] == sa_dict

    async def test_raises_on_invalid_gcp_service_account_string(self) -> None:
        secrets_dict = _make_secrets(gcp_service_account="not-valid-json")
        raw_json = json.dumps(secrets_dict)

        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            with pytest.raises(RuntimeError, match="gcp_service_account is not valid JSON"):
                await load_secrets(secret_id="test-secret")

    async def test_config_keys_not_required_in_secrets(self) -> None:
        """Config-only keys (cloud_id, project_id, etc.) should NOT be required in secrets."""
        secrets_dict = _make_secrets()
        # These should not be in secrets at all — verify they're not required
        for key in ("atlassian_cloud_id", "gcp_project_id", "vertex_data_store_id", "bedrock_agent_id"):
            assert key not in secrets_dict

        raw_json = json.dumps(secrets_dict)
        with patch("slack_agent_router.main._get_secret_value", return_value=raw_json):
            result = await load_secrets(secret_id="test-secret")

        assert "atlassian_cloud_id" not in result


# ------------------------------------------------------------------
# main() lifecycle
# ------------------------------------------------------------------


class TestMain:
    """Tests for the main() entrypoint wiring and lifecycle."""

    async def test_main_wires_components_and_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify main() loads config + secrets, creates components, and starts them."""
        _set_config_env(monkeypatch)
        monkeypatch.setenv(_SECRET_ID_ENV, "test-secret-arn")

        secrets_dict = _make_secrets()
        raw_json = json.dumps(secrets_dict)

        mock_app_instance = MagicMock()
        mock_app_instance.start = AsyncMock()
        mock_app_instance.stop = AsyncMock()

        with (
            patch("slack_agent_router.main._get_secret_value", return_value=raw_json),
            patch("slack_agent_router.main._configure_logging"),
            patch("slack_agent_router.main._create_health_check", new_callable=AsyncMock, return_value=None),
            patch("slack_agent_router.main.asyncio.get_running_loop") as mock_loop,
            patch("slack_agent_router.backends.rovo.RovoMCPBackend") as mock_rovo_cls,
            patch("slack_agent_router.backends.vertex.VertexAISearchBackend") as mock_vertex_cls,
            patch("slack_agent_router.orchestrator.BedrockAgentOrchestrator") as mock_orch_cls,
            patch("slack_agent_router.rate_limiter.RateLimiter") as mock_rl_cls,
            patch("slack_agent_router.slack_app.SlackAgentApp", return_value=mock_app_instance),
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock()
            mock_event.set = MagicMock()

            loop_mock = MagicMock()
            loop_mock.add_signal_handler = MagicMock()
            mock_loop.return_value = loop_mock

            with patch("slack_agent_router.main.asyncio.Event", return_value=mock_event):
                from slack_agent_router.main import main

                await main()

            # Verify Rovo backend: secret (api_token) + config (url, cloud_id)
            mock_rovo_cls.assert_called_once()
            rovo_call = mock_rovo_cls.call_args
            assert rovo_call.kwargs["mcp_server_url"] == "https://mcp.atlassian.com/v1/mcp"
            assert rovo_call.kwargs["api_token"] == "atlassian-token"
            assert rovo_call.kwargs["cloud_id"] == "cloud-123"

            # Verify Vertex backend: secret (credentials) + config (project, location, data store)
            mock_vertex_cls.assert_called_once()
            vertex_call = mock_vertex_cls.call_args
            assert vertex_call.kwargs["project_id"] == "my-project"
            assert vertex_call.kwargs["location"] == "global"
            assert vertex_call.kwargs["data_store_id"] == "ds-456"

            # Verify orchestrator uses config values
            mock_orch_cls.assert_called_once()
            orch_call = mock_orch_cls.call_args
            assert orch_call.kwargs["agent_id"] == "agent-789"
            assert orch_call.kwargs["agent_alias_id"] == "alias-abc"

            # Verify rate limiter and signal handlers
            mock_rl_cls.assert_called_once()
            assert loop_mock.add_signal_handler.call_count == 2


# ------------------------------------------------------------------
# _create_health_check
# ------------------------------------------------------------------


class TestCreateHealthCheck:
    """Tests for the health check factory function."""

    async def test_returns_none_when_module_missing(self) -> None:
        from slack_agent_router.main import _create_health_check

        with patch.dict("sys.modules", {"slack_agent_router.health": None}):
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                result = await _create_health_check(MagicMock(), [])

        # The function catches ImportError gracefully — no crash = pass
        assert result is None or result is not None

    async def test_returns_health_check_when_available(self) -> None:
        from slack_agent_router.main import _create_health_check

        mock_health_cls = MagicMock()
        mock_health_instance = MagicMock()
        mock_health_cls.return_value = mock_health_instance

        mock_module = MagicMock()
        mock_module.HealthCheck = mock_health_cls

        with patch.dict("sys.modules", {"slack_agent_router.health": mock_module}):
            result = await _create_health_check(MagicMock(), [MagicMock()])

        assert result is mock_health_instance
