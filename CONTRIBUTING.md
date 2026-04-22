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
- **Unit tests** — specific scenarios and edge cases with `pytest`
- **Integration tests** — full pipeline flows with mocked external services

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

### Google Vertex AI Search

1. Create a **GCP project** with the Discovery Engine API enabled
2. Create a **Vertex AI Search data store** indexed against your Google Sites content
3. Create a **GCP service account** with the `discoveryengine.viewer` role scoped to the data store
4. Export the service account **JSON key file** (this goes into Secrets Manager)
5. Note the **project ID**, **location** (typically `global`), and **data store ID**

### Amazon Bedrock Agent

1. Create a **Bedrock Agent** in the AWS console with Claude Sonnet as the model
2. Add two action groups configured with `RETURN_CONTROL`:
   - `SearchConfluenceJira` — describes searching Confluence and Jira via Rovo
   - `SearchGoogleSites` — describes searching the company Google Sites website
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
| GCP service account | JSON key file contents |
| GCP project ID | Project identifier |
| Vertex AI data store ID | Data store identifier |
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
