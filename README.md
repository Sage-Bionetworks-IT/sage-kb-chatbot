# Sage Internal Knowledge Slack Chatbot

A Slack chatbot that answers Sage Bionetworks employee questions by routing queries through an Amazon Bedrock Agent
to fetch information from internal data sources.

## Architecture

```
Slack (Socket Mode) → Slack Agent App → Bedrock Agent Orchestrator → Rovo MCP Backend → Atlassian (Confluence/Jira)
```

The app uses a RETURN_CONTROL action group pattern: the Bedrock Agent decides when to call tools, returns control to
the application, which executes the tool call against the Rovo MCP server, then sends results back to the agent for
answer synthesis.

Key components:

- **SlackAgentApp** — Slack Socket Mode listener, handles messages and mentions
- **BedrockAgentOrchestrator** — Manages the Bedrock Agent conversation loop with timeout and iteration guards
- **RovoMCPBackend** — Calls Atlassian Rovo MCP to search Confluence pages and Jira issues
- **RateLimiter** — Per-user rate limiting
- **AuditLogger** — Structured logging of all interactions

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- AWS credentials with access to Bedrock and Secrets Manager
- A configured Amazon Bedrock Agent (see [sage-kb-chatbot-infra])
- A Slack app with Socket Mode enabled

## Setup

```bash
# Clone and enter the project
cd sage-kb-chatbot

# Install dependencies
uv sync --extra dev

# Copy and fill in configuration
cp config.yaml.example config.yaml
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

```bash
# With config.yaml in place and AWS credentials configured:
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

## Testing

```bash
# Run all tests
uv run pytest

# With coverage
uv run pytest --cov

# Run a specific test file
uv run pytest tests/test_rovo_backend.py -v
```

Tests use [Hypothesis](https://hypothesis.readthedocs.io/) for property-based testing alongside standard pytest
unit tests.

## Linting

```bash
uv run ruff check .
uv run ruff format .
```

## Infrastructure

The Bedrock Agent and supporting AWS resources are defined as CDK stacks in [sage-kb-chatbot-infra]. After
deploying, the `AgentId` and `AgentAliasId` outputs should be set in your config.

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

## License

See [LICENSE](LICENSE).

[sage-kb-chatbot-infra]: https://github.com/Sage-Bionetworks-IT/sage-kb-chatbot-infra
