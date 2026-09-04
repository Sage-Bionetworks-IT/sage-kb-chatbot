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
7. Add the **OAuth scope** `usergroups:read` to the bot token — this is required for the authorization check

### Authorization (User Groups)

Authorization is **fail-closed**: by default, with nothing configured, the bot **denies every user**. This is deliberate — a missing or forgotten config locks the bot down rather than exposing internal Confluence/Jira content to the whole workspace.

**Access rule:**

```
allowed = ("*" in slack_authorized_usergroups)
          OR (user is a member of a configured authorized group)
```

- `slack_authorized_usergroups` — only members of these groups may use the bot. When empty, everyone is denied.
- The wildcard `"*"` — when the list contains `"*"`, the bot is open to **all** workspace users and group membership is not checked. It's the single, explicit way to run the bot open; set it deliberately.

The auth check is always wired. `main.py` logs a warning when the list contains `"*"` (open to everyone) and a distinct warning when nothing is configured (denying everyone), so both edge states are visible at startup.

**How it works:**

1. On first request (or after cache expiry), the bot calls `usergroups.list` to resolve each configured group handle → ID
2. It calls `usergroups.users.list` per group and unions the members into the authorized set
3. The member set is cached for 5 minutes to avoid excessive API calls
4. Each incoming event is checked against the cached set before any processing (short-circuited when the wildcard `"*"` is configured — no API calls at all)

**Required Slack permissions:**

| OAuth Scope | Purpose |
|-------------|---------|
| `usergroups:read` | List User Groups and resolve group handles to their IDs |

**Behavior on errors:**

- If a group can't be resolved (e.g. it doesn't exist), that group contributes no members and the cache uses a short 30s retry TTL so resolution is retried soon. Consistent with fail-closed, an unresolved group simply means its members aren't authorized yet.
- If the API fails after a group was previously resolved, the stale cache is retained — a transient failure won't wipe a good member set.

**Configuring access:**

Use group **handles** (the `@`-mention slug), without the `@`. The list may be a YAML list in `config.yaml` or a comma-separated string via `SLACK_AUTHORIZED_USERGROUPS`. The env var replaces the file value wholesale (it does not merge) — see [Configuration](#configuration) for the full source-precedence rules.

```yaml
# config.yaml — restrict to IT and Security
slack_authorized_usergroups:
  - it-team
  - sec-team
```

```bash
# or via environment variable (comma-separated)
export SLACK_AUTHORIZED_USERGROUPS=it-team,sec-team
```

To intentionally open the bot to everyone, use the wildcard:

```yaml
# config.yaml
slack_authorized_usergroups: ["*"]
```

```bash
export SLACK_AUTHORIZED_USERGROUPS=*
```

Leave the list empty/unset to deny all users (the safe default).

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

## Event Processing Pipeline

Each incoming Slack event passes through the following stages before the bot responds:

```
Event received → Deduplication → Authorization → Rate Limiting → Orchestration → Response
```

### Event Deduplication

Slack may redeliver events when the WebSocket reconnects or if acknowledgement is slow. The bot uses an in-memory TTL cache to silently skip duplicate events.

**How it works:**

1. For `app_mention` and DM events, the bot derives a dedup key from (in priority order):
   - `event_id` (preferred — Slack's unique event identifier)
   - `client_msg_id` (fallback — client-assigned message ID)
   - `channel:event_ts` composite (last resort — for events with neither ID)
2. For `/sage-ask` slash commands, the bot deduplicates on `trigger_id`
3. If the key was seen within the last 60 seconds, the event is skipped silently
4. Otherwise, the key is recorded and processing continues

**Characteristics:**

- State is in-memory only — resets on container restart (acceptable for single-task ECS)
- 60-second TTL window covers Slack's typical retry behavior
- Periodic cleanup evicts expired entries to bound memory growth
- Empty or unreliable keys (missing channel or timestamp) bypass dedup entirely — the event is always processed

**Ordering:** Deduplication runs before authorization and rate limiting (per Requirement 2.3), so duplicate events never count against rate limits or trigger unnecessary Slack API calls.

### Progressive UX Feedback

While a question is being answered, the bot surfaces live progress so the user knows it's working (Requirement 4). This spans `SlackAgentApp._process_question` and the orchestrator's `on_progress` callback.

**How it works:**

1. On receipt, the bot adds a 👀 (`eyes`) reaction to the user's message via `reactions.add`.
2. It posts a **⏳ Thinking...** placeholder reply with `chat.postMessage` and keeps the returned `ts`.
3. `SlackAgentApp` builds an `on_progress` callback and passes it to `BedrockAgentOrchestrator.ask`. As each backend tool call begins, the orchestrator invokes the callback with the action group name, and the bot updates the placeholder in place with a per-backend message (`chat.update`) — e.g. `SearchConfluenceJira` maps to **⏳ Searching Confluence and Jira...**, with a generic **⏳ Searching...** fallback for unmapped action groups.
4. When the answer is ready, the placeholder is updated in place to the final answer (`chat.update`).
5. The 👀 reaction is removed and a ✅ (`white_check_mark`) reaction is added.

**Characteristics:**

- **Best-effort** — every reaction, placeholder, and update call is wrapped so failures are logged and swallowed; progress reporting never aborts answer delivery. The orchestrator likewise swallows `on_progress` errors (re-raising only `CancelledError`).
- **Graceful fallback** — if the placeholder can't be posted (or its later update fails), the bot posts the answer as a fresh threaded reply via `say`.
- **Cached calls are quiet** — the orchestrator fires `on_progress` only for real backend calls, not for tool calls served from its dedup cache, so the placeholder doesn't flicker on repeated calls.
- **Slash commands** — reactions are skipped (no user message to react to), but the thinking placeholder is still shown; `on_progress` is only wired up when a placeholder exists.
- **Retries** — all of these Slack calls go through the shared `_slack_call_with_retry` helper, which retries on 429 with exponential backoff honoring `Retry-After`.
- Action-group-to-message mapping lives in `_ACTION_GROUP_PROGRESS` in `slack_app.py`; add an entry there when introducing a new backend action group.

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

Configuration is loaded from a YAML/JSON file with environment variable overrides.

| Config key                       | Environment variable           | Description                              |
|----------------------------------|--------------------------------|------------------------------------------|
| `rovo_mcp_server_url`            | `ROVO_MCP_SERVER_URL`          | Atlassian Rovo MCP endpoint              |
| `atlassian_cloud_id`             | `ATLASSIAN_CLOUD_ID`           | Atlassian Cloud instance ID              |
| `atlassian_service_user`         | `ATLASSIAN_SERVICE_USER`       | Atlassian service account email          |
| `bedrock_agent_id`               | `BEDROCK_AGENT_ID`             | Amazon Bedrock Agent ID                  |
| `bedrock_agent_alias_id`         | `BEDROCK_AGENT_ALIAS_ID`       | Amazon Bedrock Agent Alias ID            |
| `slack_agent_router_secret_id`   | `SLACK_AGENT_ROUTER_SECRET_ID` | Secrets Manager secret name/ARN          |
| `slack_authorized_usergroups`    | `SLACK_AUTHORIZED_USERGROUPS`  | Authorized Slack User Group handles, comma-separated in the env var; `*` = allow all; empty = deny all |

**Precedence (per setting):**

1. **Environment variable wins** over the config file when it is set to a non-empty value.
2. **Config file** value is used when the env var is unset or empty.
3. **Built-in default** applies when neither is provided (for `slack_authorized_usergroups`, the default is empty → deny all).

Two things to note for the list setting `slack_authorized_usergroups`:

- The override is **whole-value replacement, not a merge** — if `SLACK_AUTHORIZED_USERGROUPS` is set, the file's list is ignored entirely (and vice versa). You cannot define some groups in the file and add more via the env var.
- An **empty env var does not override** — `SLACK_AUTHORIZED_USERGROUPS=` (blank) is treated as unset, so the config-file value still applies. To actually clear the list from the environment, unset the variable rather than setting it blank.

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
# Fill in config values. The Bedrock agent/alias IDs are NOT stored in config —
# export them (from the deployed BedrockAgentStack outputs) before running:
export BEDROCK_AGENT_ID=your-agent-id
export BEDROCK_AGENT_ALIAS_ID=your-alias-id
uv run python -m slack_agent_router.main
```

The agent/alias IDs are kept out of `config.yaml` so the app is a pure invoker with a single source of truth (the
`BedrockAgentStack`). In the ECS deployment these are injected automatically by CDK — see
[Infrastructure](#infrastructure).

The app starts a Socket Mode connection to Slack and a health check server on port 8080.

## Docker

The app is packaged as a Docker image using a multi-stage build with [uv](https://docs.astral.sh/uv/) for fast,
reproducible installs.

### Build the image

```bash
docker build -t sage-kb-chatbot .
```

### Run the container

The container needs AWS credentials and configuration passed via environment variables. Secrets are fetched from AWS
Secrets Manager at startup, so the container needs network access to AWS APIs.

```bash
docker run -p 8080:8080 \
  -e BEDROCK_AGENT_ID=your-agent-id \
  -e BEDROCK_AGENT_ALIAS_ID=your-alias-id \
  -e ROVO_MCP_SERVER_URL=https://mcp.atlassian.com/v1/mcp \
  -e ATLASSIAN_CLOUD_ID=your-cloud-id \
  -e ATLASSIAN_SERVICE_USER=your-service-account@example.com \
  -e SLACK_AGENT_ROUTER_SECRET_ID=infra/slack-agent-router \
  -e AWS_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  sage-kb-chatbot
```

When running on ECS/Fargate, AWS credentials come from the task role — no need to pass `AWS_ACCESS_KEY_ID` or
`AWS_SECRET_ACCESS_KEY`.

### Health check

The container exposes a health check endpoint at `http://localhost:8080/health`. Docker's built-in `HEALTHCHECK`
instruction is configured in the Dockerfile:

- Interval: 30s
- Timeout: 5s
- Start period: 10s (gives the app time to connect to Slack)
- Retries: 3

### Logging

The app writes structured JSON logs to stdout. On ECS Fargate, these are automatically captured by the `awslogs`
driver and shipped to CloudWatch. Set the `LOG_LEVEL` environment variable to control verbosity
(`DEBUG`, `INFO`, `WARNING`, `ERROR`). Defaults to `INFO`.

## Infrastructure

The Bedrock Agent and supporting AWS resources are defined as CDK stacks in [sage-kb-chatbot-infra]. The
`BedrockAgentStack` creates the agent and alias, and the CDK app injects their IDs into the ECS task's
`BEDROCK_AGENT_ID` / `BEDROCK_AGENT_ALIAS_ID` environment variables as stack references, resolved at deploy time.

This means the deployed app is a pure invoker: you do **not** copy the agent/alias IDs into config by hand for the
ECS deployment — CDK wires them in automatically, and CloudFormation guarantees the agent exists before the app
stack deploys. The `bedrock_agent_id` / `bedrock_agent_alias_id` values in `config.yaml` are only for running the
app locally (see [Running locally](#running-locally)).

## Project structure

```
src/slack_agent_router/
├── main.py            # Entrypoint — config, secrets, wiring, startup
├── slack_app.py       # Slack Socket Mode listener
├── orchestrator.py    # Bedrock Agent conversation loop
├── auth.py            # User Group authorization check
├── dedup.py           # Event deduplication (TTL cache)
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
