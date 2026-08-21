"""Tests for the application entrypoint (main.py).

Covers config file loading (JSON/YAML), environment variable overrides,
secrets loading, validation, component wiring, and the main() lifecycle.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_agent_router.main import (
    _ATLASSIAN_CLOUD_ID_ENV,
    _ATLASSIAN_SERVICE_USER_ENV,
    _BEDROCK_AGENT_ALIAS_ID_ENV,
    _BEDROCK_AGENT_ID_ENV,
    _CONFIG_FILE_ENV,
    _REQUIRED_SECRET_KEYS,
    _ROVO_MCP_SERVER_URL_ENV,
    _SLACK_AGENT_ROUTER_SECRET_ID_ENV,
    _validate_required_secret_keys,
    load_config,
    load_secrets,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_CONFIG_ENV_VARS = {
    _ATLASSIAN_SERVICE_USER_ENV: "test@example.com",
    _ROVO_MCP_SERVER_URL_ENV: "https://mcp.atlassian.com/v1/mcp",
    _ATLASSIAN_CLOUD_ID_ENV: "cloud-123",
    _BEDROCK_AGENT_ID_ENV: "agent-789",
    _BEDROCK_AGENT_ALIAS_ID_ENV: "alias-abc",
    _SLACK_AGENT_ROUTER_SECRET_ID_ENV: "test-secret-arn",
}

_CONFIG_FILE_VALUES = {
    "atlassian_service_user": "test@example.com",
    "rovo_mcp_server_url": "https://mcp.atlassian.com/v1/mcp",
    "atlassian_cloud_id": "cloud-123",
    "bedrock_agent_id": "agent-789",
    "bedrock_agent_alias_id": "alias-abc",
    "slack_agent_router_secret_id": "test-secret-arn",
}


def _make_secrets(**overrides: Any) -> dict[str, Any]:
    """Build a complete secrets dict with sensible defaults."""
    base: dict[str, Any] = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "atlassian_api_token": "atlassian-token",
    }
    base.update(overrides)
    return base


def _set_config_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set all required config env vars, with optional overrides."""
    for env_var, value in _CONFIG_ENV_VARS.items():
        monkeypatch.setenv(env_var, overrides.get(env_var, value))


def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all config env vars so file-only loading can be tested."""
    for env_var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(_CONFIG_FILE_ENV, raising=False)


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
# load_config — environment variables only
# ------------------------------------------------------------------


class TestLoadConfigEnvOnly:
    """Tests for loading config purely from environment variables."""

    def test_loads_all_config_values(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        _set_config_env(monkeypatch)
        config = load_config()
        assert config.rovo_mcp_server_url == "https://mcp.atlassian.com/v1/mcp"
        assert config.atlassian_cloud_id == "cloud-123"
        assert config.bedrock_agent_id == "agent-789"
        assert config.bedrock_agent_alias_id == "alias-abc"

    @pytest.mark.parametrize(
        "env_var",
        [
            _ROVO_MCP_SERVER_URL_ENV,
            _ATLASSIAN_CLOUD_ID_ENV,
            _BEDROCK_AGENT_ID_ENV,
            _BEDROCK_AGENT_ALIAS_ID_ENV,
        ],
    )
    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_var: str) -> None:
        monkeypatch.chdir(tmp_path)
        _set_config_env(monkeypatch)
        monkeypatch.delenv(env_var)
        with pytest.raises(RuntimeError, match="Missing required configuration"):
            load_config()

    def test_empty_env_var_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        _set_config_env(monkeypatch)
        monkeypatch.setenv(_BEDROCK_AGENT_ID_ENV, "  ")
        with pytest.raises(RuntimeError, match=_BEDROCK_AGENT_ID_ENV):
            load_config()

    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        _set_config_env(monkeypatch)
        config = load_config()
        with pytest.raises(AttributeError):
            config.atlassian_cloud_id = "mutated"  # type: ignore[misc]


# ------------------------------------------------------------------
# load_config — JSON config file
# ------------------------------------------------------------------


class TestLoadConfigJsonFile:
    """Tests for loading config from a JSON file."""

    def test_loads_from_json_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(_CONFIG_FILE_VALUES))

        config = load_config(config_path=str(config_file))
        assert config.bedrock_agent_id == "agent-789"

    def test_env_overrides_json_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(_CONFIG_FILE_VALUES))

        monkeypatch.setenv(_BEDROCK_AGENT_ID_ENV, "overridden-agent")
        config = load_config(config_path=str(config_file))
        assert config.bedrock_agent_id == "overridden-agent"
        # Non-overridden values come from file
        assert config.atlassian_cloud_id == "cloud-123"

    def test_raises_on_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json {{{")

        with pytest.raises(RuntimeError, match="Invalid JSON"):
            load_config(config_path=str(config_file))

    def test_raises_on_json_array(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2, 3]")

        with pytest.raises(RuntimeError, match="must contain a JSON object"):
            load_config(config_path=str(config_file))


# ------------------------------------------------------------------
# load_config — YAML config file
# ------------------------------------------------------------------


class TestLoadConfigYamlFile:
    """Tests for loading config from a YAML file."""

    def test_loads_from_yaml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            atlassian_service_user: test@example.com
            rovo_mcp_server_url: https://mcp.atlassian.com/v1/mcp
            atlassian_cloud_id: cloud-yaml
            bedrock_agent_id: agent-789
            bedrock_agent_alias_id: alias-abc
            slack_agent_router_secret_id: test-secret-arn
        """)
        )

        config = load_config(config_path=str(config_file))
        assert config.atlassian_cloud_id == "cloud-yaml"

    def test_loads_yml_extension(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            textwrap.dedent("""\
            atlassian_service_user: test@example.com
            rovo_mcp_server_url: https://mcp.atlassian.com/v1/mcp
            atlassian_cloud_id: cloud-yml
            bedrock_agent_id: agent-789
            bedrock_agent_alias_id: alias-abc
            slack_agent_router_secret_id: test-secret-arn
        """)
        )

        config = load_config(config_path=str(config_file))
        assert config.atlassian_cloud_id == "cloud-yml"

    def test_env_overrides_yaml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            atlassian_service_user: test@example.com
            rovo_mcp_server_url: https://mcp.atlassian.com/v1/mcp
            atlassian_cloud_id: cloud-yaml
            bedrock_agent_id: agent-789
            bedrock_agent_alias_id: alias-abc
            slack_agent_router_secret_id: test-secret-arn
        """)
        )

        monkeypatch.setenv(_BEDROCK_AGENT_ID_ENV, "overridden-agent")
        config = load_config(config_path=str(config_file))
        assert config.bedrock_agent_id == "overridden-agent"
        assert config.atlassian_cloud_id == "cloud-yaml"

    def test_raises_on_invalid_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(":\n  - :\n    bad: [yaml")

        with pytest.raises(RuntimeError, match="Invalid YAML"):
            load_config(config_path=str(config_file))

    def test_empty_yaml_file_falls_through_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty YAML file should not crash — values come from env."""
        _set_config_env(monkeypatch)
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        config = load_config(config_path=str(config_file))
        assert config.bedrock_agent_id == "agent-789"


# ------------------------------------------------------------------
# load_config — file resolution
# ------------------------------------------------------------------


class TestLoadConfigFileResolution:
    """Tests for config file path resolution."""

    def test_explicit_path_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "custom.json"
        config_file.write_text(json.dumps(_CONFIG_FILE_VALUES))

        config = load_config(config_path=str(config_file))
        assert config.bedrock_agent_id == "agent-789"

    def test_env_var_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        config_file = tmp_path / "from-env.json"
        config_file.write_text(json.dumps(_CONFIG_FILE_VALUES))
        monkeypatch.setenv(_CONFIG_FILE_ENV, str(config_file))

        config = load_config()
        assert config.bedrock_agent_id == "agent-789"

    def test_raises_when_explicit_path_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        with pytest.raises(RuntimeError, match="Config file not found"):
            load_config(config_path="/nonexistent/config.json")

    def test_raises_when_env_path_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_config_env(monkeypatch)
        monkeypatch.setenv(_CONFIG_FILE_ENV, "/nonexistent/config.yaml")
        with pytest.raises(RuntimeError, match="Config file not found"):
            load_config()

    def test_no_file_no_env_raises_for_missing_values(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """With no file and no env vars, all values are missing."""
        monkeypatch.chdir(tmp_path)
        _clear_config_env(monkeypatch)
        with pytest.raises(RuntimeError, match="Missing required configuration"):
            load_config()

    def test_partial_file_plus_env_fills_gaps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """File provides some values, env vars fill the rest."""
        _clear_config_env(monkeypatch)
        partial = {
            "rovo_mcp_server_url": "https://mcp.atlassian.com/v1/mcp",
            "atlassian_cloud_id": "from-file",
        }
        config_file = tmp_path / "partial.json"
        config_file.write_text(json.dumps(partial))

        monkeypatch.setenv(_ATLASSIAN_SERVICE_USER_ENV, "test@example.com")
        monkeypatch.setenv(_BEDROCK_AGENT_ID_ENV, "agent-env")
        monkeypatch.setenv(_BEDROCK_AGENT_ALIAS_ID_ENV, "alias-env")
        monkeypatch.setenv(_SLACK_AGENT_ROUTER_SECRET_ID_ENV, "test-secret-arn")

        config = load_config(config_path=str(config_file))
        assert config.atlassian_cloud_id == "from-file"
        assert config.bedrock_agent_id == "agent-env"


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

    async def test_config_keys_not_required_in_secrets(self) -> None:
        secrets_dict = _make_secrets()
        for key in ("atlassian_cloud_id", "bedrock_agent_id"):
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
        _set_config_env(monkeypatch)
        monkeypatch.setenv(_SLACK_AGENT_ROUTER_SECRET_ID_ENV, "test-secret-arn")

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

            mock_rovo_cls.assert_called_once()
            rovo_call = mock_rovo_cls.call_args
            assert rovo_call.kwargs["mcp_server_url"] == "https://mcp.atlassian.com/v1/mcp"
            assert rovo_call.kwargs["api_token"] == "atlassian-token"
            assert rovo_call.kwargs["cloud_id"] == "cloud-123"

            mock_orch_cls.assert_called_once()
            orch_call = mock_orch_cls.call_args
            assert orch_call.kwargs["agent_id"] == "agent-789"
            assert orch_call.kwargs["agent_alias_id"] == "alias-abc"

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

        assert result is None

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
