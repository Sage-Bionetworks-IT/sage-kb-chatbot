# Contributing

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)

## Setup

```bash
# Clone and set up the project
git clone <repo-url>
cd sage-kb-chatbot
uv sync --all-extras
```

This creates a `.venv`, installs all runtime and dev dependencies,
and installs the project in editable mode.

## Common Commands

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov

# Run a specific test file
uv run pytest tests/test_sanitize.py -v

# Add a dependency
uv add <package>

# Add a dev dependency
uv add --optional dev <package>

# Update lock file after editing pyproject.toml
uv lock

# Sync environment after pulling changes
uv sync --all-extras
```

## Pre-commit Hooks

```bash
# Install pre-commit hooks (first time only)
uv run pre-commit install

# Run hooks manually
uv run pre-commit run --all-files
```

## Testing

This project follows test-driven development (RED → GREEN → REFACTOR).
All new code requires tests with 80%+ coverage.

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_sanitize.py -v

# Run a single test by name
uv run pytest -k "test_auth_failure_returns_failed_result"
```

### Test types

- **Property tests** — use [Hypothesis](https://hypothesis.readthedocs.io/) to verify
  universal correctness properties (e.g., "for any valid MCP response, the backend
  produces a BackendResult with success=True")
- **Unit tests** — specific scenarios, edge cases, and full pipeline flows with mocked external services

### Writing tests

- Write tests first (RED), then implement (GREEN)
- Async tests work automatically — just use `async def test_*` (no decorator needed)
- Mock external dependencies (`AsyncMock` for async, `MagicMock` for sync)
- Use `pytest.fixture` for shared setup and `pytest.mark.parametrize` for multiple inputs

## External Services Setup

This project integrates with several external services. Each needs to be configured before the system can run end-to-end.

### Slack App

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** and generate an **app-level token** (`xapp-...`) with `connections:write` scope
3. Add a **bot user** and install the app to your workspace to get a **bot token** (`xoxb-...`)
4. Subscribe to these **Events API** events:
   - `app_mention` — bot mentioned in channels
   - `message.im` — direct messages to the bot
5. Register the `/sage-ask` **slash command**
6. Add the bot to channels where it should respond

### Atlassian Rovo (Confluence/Jira)

1. Create a dedicated **service account** in your Atlassian Cloud instance (for broad content access)
2. Generate an **API token** for the service account at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
3. Note your **Atlassian Cloud ID** (found in admin settings or the URL: `https://<instance>.atlassian.net`)
4. The MCP endpoint is `https://mcp.atlassian.com/v1/mcp`

### Amazon Bedrock Agent

1. Create a **Bedrock Agent** in the AWS console with Claude Sonnet as the model
2. Add an action group configured with `RETURN_CONTROL`:
   - `SearchConfluenceJira` — describes searching Confluence and Jira via Rovo
3. Configure agent instructions with grounding rules, citation requirements, and refusal behavior
4. Create an **agent alias** and note the **agent ID** and **alias ID**

### AWS Secrets Manager

Store all credentials as Secrets Manager secrets (referenced by the ECS task at runtime):

| Secret | Contents |
|--------|----------|
| Slack bot token | `xoxb-...` |
| Slack app-level token | `xapp-...` |
| Atlassian API token | Service account token |
| Atlassian Cloud ID | Cloud instance ID |
| Bedrock Agent ID | Agent identifier |
| Bedrock Agent alias ID | Alias identifier |

The ECS task role needs `secretsmanager:GetSecretValue` permission for these secrets.

## Commit Messages

Use [conventional commits](https://www.conventionalcommits.org/):

```
feat(component): add new feature
fix(component): fix a bug
test(component): add or update tests
chore: maintenance tasks
docs: documentation updates
refactor(component): code restructuring
```

## Configuration

Configuration is loaded from a YAML/JSON file with environment variable overrides. Env vars always take precedence
over file values.

| Config key               | Environment variable           | Description                     |
|--------------------------|--------------------------------|---------------------------------|
| `rovo_mcp_server_url`    | `ROVO_MCP_SERVER_URL`          | Atlassian Rovo MCP endpoint     |
| `atlassian_cloud_id`     | `ATLASSIAN_CLOUD_ID`           | Atlassian Cloud instance ID     |
| `atlassian_service_user` | `ATLASSIAN_SERVICE_USER`       | Atlassian service account email |
| `bedrock_agent_id`       | `BEDROCK_AGENT_ID`             | Amazon Bedrock Agent ID         |
| `bedrock_agent_alias_id` | `BEDROCK_AGENT_ALIAS_ID`       | Amazon Bedrock Agent Alias ID   |
| `secret_id`              | `SLACK_AGENT_ROUTER_SECRET_ID` | Secrets Manager secret name/ARN |

The config file path is resolved in order:
1. `SLACK_AGENT_ROUTER_CONFIG` environment variable
2. `config.yaml` or `config.json` in the working directory

### Secrets (AWS Secrets Manager)

Sensitive credentials are stored in AWS Secrets Manager as a JSON object:

```json
{
  "slack_bot_token": "xoxb-...",
  "slack_app_token": "xapp-...",
  "atlassian_api_token": "..."
}
```

## Running locally

Requires AWS credentials, a configured Bedrock Agent (see [sage-kb-chatbot-infra]), and a Slack app with Socket Mode
enabled.

```bash
cp config.yaml.example config.yaml
# Fill in config values, then:
uv run python -m slack_agent_router.main
```

The app starts a Socket Mode connection to Slack and a health check server on port 8080.

## Docker

```bash
docker build -t sage-kb-chatbot .
docker run -p 8080:8080 \
  -e BEDROCK_AGENT_ID=... \
  -e BEDROCK_AGENT_ALIAS_ID=... \
  -e ROVO_MCP_SERVER_URL=https://mcp.atlassian.com/v1/mcp \
  -e ATLASSIAN_CLOUD_ID=... \
  -e ATLASSIAN_SERVICE_USER=... \
  -e SLACK_AGENT_ROUTER_SECRET_ID=... \
  sage-kb-chatbot
```

## Infrastructure

The Bedrock Agent and supporting AWS resources are defined as CDK stacks in [sage-kb-chatbot-infra]. After deploying,
the `AgentId` and `AgentAliasId` outputs should be set in your config.

## Project structure

```
src/slack_agent_router/
├── main.py            # Entrypoint — config, secrets, wiring, startup
├── slack_app.py       # Slack Socket Mode listener
├── orchestrator.py    # Bedrock Agent conversation loop
├── backends/
│   └── rovo.py        # Atlassian Rovo MCP client
├── rate_limiter.py    # Per-user rate limiting
├── sanitize.py        # Input sanitization
├── formatter.py       # Response formatting for Slack
├── audit_logger.py    # Structured audit logging
└── models.py          # Shared data models
tests/
├── test_orchestrator.py
├── test_rovo_backend.py
└── ...
```

[sage-kb-chatbot-infra]: https://github.com/Sage-Bionetworks-IT/sage-kb-chatbot-infra
