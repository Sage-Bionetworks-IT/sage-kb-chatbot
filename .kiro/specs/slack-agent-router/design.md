# Design Document: Slack Agent Router

## Overview

The Slack Agent Router is a chatbot for Sage Bionetworks employees that receives questions via Slack (app mentions, DMs, or slash commands) and uses a managed Amazon Bedrock Agent to route questions to external knowledge sources, synthesize results, and return coherent, cited answers.

The Bedrock Agent acts as the brain of the system — it decides which knowledge sources to query for each question, orchestrates the tool calls via the return control pattern, and synthesizes a single answer from the combined results. The application code handles Slack interaction, backend API execution, and operational concerns (rate limiting, health checks, logging). This separation means routing logic, prompt engineering, and answer synthesis are managed by Bedrock, while backend integrations and infrastructure are managed by our code.

Two knowledge backends are configured as Bedrock Agent action groups:
- Atlassian Rovo MCP Server — searches Confluence and Jira content
- Google Drive — searches the internal corporate knowledge base (Google Workspace) content stored as Drive files (Docs, Sheets, Slides, PDFs)

Because the Bedrock Agent performs synthesis across all tool results, adding the Google Drive backend requires no custom merge logic: the agent queries both sources for a given question and produces a single, coherent, cited answer that blends Confluence/Jira and Google Drive content.

> **Note on Google Sites vs. Drive content.** The internal corporate site is built on *new* Google Sites, but the authoritative content lives in Google Drive files organized under a shared folder tree. New Google Sites do not expose page *body* text to the Drive API or any Google Workspace MCP server, so the Site pages themselves are not full-text searchable. This backend therefore searches the **Drive files** (where the real content lives), not the rendered Site pages.

The architecture uses Slack Socket Mode to receive events over a persistent WebSocket connection, eliminating the need for a public HTTP endpoint. A single ECS Fargate service handles event reception, Bedrock Agent interaction, backend execution, and response posting. This makes the system simple to operate, secure by default (no public attack surface), and straightforward to extend.

Adding a new backend means creating a new `RETURN_CONTROL` action group on the Bedrock Agent and adding a corresponding backend class in the application code. The Bedrock Agent automatically incorporates the new tool into its routing decisions — no custom routing logic needed. The Google Drive backend follows exactly this pattern (`SearchGoogleWorkspace` action group + `GoogleDriveBackend` class).

## Architecture

![Slack Agent Router Architecture](slack-agent-router-architecture.drawio.png)


### Why Socket Mode?

- **No public endpoint** — Slack pushes events over a WebSocket connection initiated by the bot. No API Gateway, no public URL, no attack surface to defend.
- **Simpler architecture** — One ECS Fargate service replaces API Gateway + Ingress Lambda + SQS + Router Lambda. Less infrastructure to build, deploy, and monitor.
- **Secure by default** — No need for WAF, IP restrictions, or signature verification. The WebSocket connection is authenticated by Slack using an app-level token.
- **Good fit for this workload** — Backend queries are lightweight HTTP calls (Rovo MCP), not heavy compute. A single process handles everything.

### Slack's 3-Second Acknowledgment

In Socket Mode, the `slack_bolt` library automatically acknowledges **event envelopes** (for example, `app_mention` and `message` events). When an event arrives over the WebSocket, Bolt sends an `ack()` response immediately before your handler runs, which satisfies Slack's 3-second requirement without any special async architecture. However, **interactive payloads** such as slash commands, button clicks, and modal submissions still require your handler to call `ack()` explicitly within 3 seconds. In all cases, the handler then processes the question and posts the answer asynchronously.


## Agent State Machine

![Agent State Machine](agent-state-machine.drawio.png)


## Sequence Diagrams

### Main Flow: Question → Answer

```mermaid
sequenceDiagram
    participant U as Slack User
    participant S as Slack Platform
    participant ECS as ECS Fargate (Socket Mode)
    participant BA as Bedrock Agent
    participant RV as Rovo MCP Server
    participant GD as Google Drive API

    U->>S: @bot What is our onboarding process?
    S->>ECS: WebSocket event (app_mention)
    ECS->>S: ack() (immediate)
    ECS->>BA: InvokeAgent (question)
    BA->>ECS: Return control (SearchConfluenceJira, params)
    ECS->>RV: MCP tool call (search/summarize)
    RV-->>ECS: Confluence/Jira results
    ECS->>BA: InvokeAgent (returnControlInvocationResults)
    BA->>ECS: Return control (SearchGoogleWorkspace, params)
    ECS->>GD: files.list (fullText contains, impersonated user)
    GD-->>ECS: Matching Drive files (title, snippet, viewUrl)
    ECS->>BA: InvokeAgent (returnControlInvocationResults)
    BA-->>ECS: Final answer blending Confluence/Jira + Drive, with citations
    ECS->>S: chat.postMessage (synthesized answer)
    S->>U: Bot reply in thread
```

The agent decides which backends are relevant for a given question. It may call one or both; the order and selection are the agent's own. The sequence above shows both being queried, which is the common case for a broad knowledge question.

### Error Flow: Backend Failure

```mermaid
sequenceDiagram
    participant ECS as ECS Fargate (Socket Mode)
    participant BA as Bedrock Agent
    participant RV as Rovo MCP Server
    participant S as Slack Platform

    ECS->>BA: InvokeAgent (question)
    BA->>ECS: Return control (SearchConfluenceJira, params)
    ECS->>RV: MCP tool call (search/summarize)
    RV--xECS: 500 / timeout
    ECS->>BA: InvokeAgent (returnControlInvocationResults: error)
    BA-->>ECS: Answer noting "Rovo unavailable"
    ECS->>S: chat.postMessage (error message)
```

### Connection Lifecycle

```mermaid
sequenceDiagram
    participant ECS as ECS Fargate
    participant S as Slack Platform

    ECS->>S: Connect WebSocket (app-level token)
    S-->>ECS: Connection established
    Note over ECS,S: Persistent connection, Slack sends events as they arrive
    S->>ECS: Event (app_mention)
    ECS->>S: ack()
    Note over ECS: Process question...
    ECS->>S: chat.postMessage (answer)
    Note over ECS,S: Connection maintained until process stops
    Note over ECS: On SIGTERM (deployment/restart):
    ECS->>ECS: Drain in-flight requests
    ECS->>S: Disconnect WebSocket
    Note over ECS: New task connects immediately
```


## Components and Interfaces

### Component 1: Socket Mode Application

**Purpose**: Maintains the WebSocket connection to Slack, receives events, acknowledges them immediately, and dispatches questions to the agents for async processing.

**Interface**:
```python
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


class SlackAgentApp:
    """Main application using Slack Bolt with async Socket Mode."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        orchestrator: "BedrockAgentOrchestrator",
    ):
        self.app = AsyncApp(token=bot_token)
        self.handler = AsyncSocketModeHandler(self.app, app_token)
        self._orchestrator = orchestrator
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register event handlers for mentions, DMs, and slash commands."""
        self.app.event("app_mention")(self._handle_mention)
        self.app.event("message")(self._handle_dm)
        self.app.command("/sage-ask")(self._handle_slash_command)

    async def _handle_mention(self, event: dict, say: callable) -> None:
        """Handle @bot mentions in channels."""
        ...

    async def _handle_dm(self, event: dict, say: callable) -> None:
        """Handle direct messages to the bot."""
        ...

    async def _handle_slash_command(self, ack: callable, command: dict, say: callable) -> None:
        """Handle /sage-ask slash command."""
        ...

    async def start(self) -> None:
        """Start the async Socket Mode connection."""
        await self.handler.start_async()

    async def stop(self) -> None:
        """Gracefully disconnect and drain in-flight requests."""
        await self.handler.close_async()
```

**Responsibilities**:
- Maintain persistent WebSocket connection to Slack via Socket Mode
- Acknowledge events immediately (handled by `slack_bolt` automatically)
- Parse `app_mention` and `message` Events API events (including DMs, filtered via `channel_type="im"`)
- Handle slash command payloads from the Commands API and explicitly call `ack()` within 3 seconds
- Strip bot mention prefix from message text
- Provide progressive feedback to the user:
  1. Immediately add 👀 (eyes) reaction to the user's message (~0-1s)
  2. Post a placeholder message with ⏳ and a generic searching indicator, e.g., "⏳ Thinking..." (~1-3s). Update the message as each tool is invoked during the return control loop, e.g., "⏳ Searching Confluence and Jira..." or "⏳ Searching Google Drive..."
  3. Update the placeholder message with the final synthesized answer via `chat.update` when complete
  4. Remove the 👀 reaction and add ✅ when the answer is posted
- Dispatch to the BedrockAgentOrchestrator for processing
- Handle graceful shutdown on SIGTERM (drain in-flight requests)
- Reconnect automatically on WebSocket disconnection
- Enforce per-user rate limits before dispatching to the Bedrock Agent
- Deduplicate events to prevent duplicate answers:
  - Track recently processed Slack event IDs (`event_id` or envelope `envelope_id`) in an in-memory TTL cache (e.g., 60s window)
  - If an event ID has already been seen, skip processing silently
  - For slash commands: Slack may retry if ack is slow — deduplicate on `trigger_id`
  - For message posting: before calling `chat.postMessage`, check if a reply already exists in the thread for this `request_id` to prevent duplicate answers on retries
  - Note: in-memory cache resets on task restart — brief window for duplicates during restarts, acceptable for MVP

### Component 2: Bedrock Agent (Managed Orchestrator)

**Purpose**: Replaces the custom Question Router and Answer Synthesizer with a managed Amazon Bedrock Agent. The agent handles question routing, backend tool selection, answer synthesis, and citation formatting — all managed by Bedrock's orchestration engine. Uses the return control feature so that backend API calls are executed by our application code, not by Lambda functions.

**How it works**:
1. The Socket Mode app sends the user's question to the Bedrock Agent via `InvokeAgent`
2. The agent decides which action groups (tools) to call based on the question
3. Instead of calling a Lambda, the agent returns control to our application with the tool name and parameters (`RETURN_CONTROL`)
4. Our application executes the backend call (Rovo MCP or Google Drive API) and sends the results back to the agent via another `InvokeAgent` call with `returnControlInvocationResults`
5. The agent synthesizes a final answer from the tool results and returns it
6. Our application formats and posts the answer to Slack

**Agent Configuration**:
- Model: Claude Sonnet (primary) via Bedrock
- Action Groups:
  - `SearchConfluenceJira` — configured with `RETURN_CONTROL`, describes searching Confluence and Jira via Rovo
  - `SearchGoogleWorkspace` — configured with `RETURN_CONTROL`, describes searching the internal corporate knowledge base stored in Google Drive
- Agent instructions: grounding rules, citation requirements, conflict handling, refusal when no information found, and guidance on when to use each source and how to blend results from both into a single answer

**Interface**:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentResponse:
    """Response from the Bedrock Agent."""

    answer: str  # Synthesized answer text
    source_urls: list[str]  # Cited source links
    tool_calls_made: list[str]  # Which action groups were invoked
    latency_ms: float


class BedrockAgentOrchestrator:
    """Manages interaction with the Amazon Bedrock Agent using return control."""

    def __init__(
        self,
        agent_id: str,
        agent_alias_id: str,
        rovo_backend: "RovoMCPBackend",
        google_drive_backend: "GoogleDriveBackend",
    ):
        """
        Args:
            agent_id: Bedrock Agent ID
            agent_alias_id: Bedrock Agent alias ID
            rovo_backend: Backend for executing Rovo MCP calls
            google_drive_backend: Backend for executing Google Drive searches
        """
        ...

    async def ask(self, question: str, session_id: str) -> AgentResponse:
        """Send a question to the Bedrock Agent and handle the return control loop.

        1. Call InvokeAgent with the question
        2. If agent returns control with tool invocation requests:
           a. Execute the requested backend calls locally
           b. Send results back via InvokeAgent with returnControlInvocationResults
           c. Repeat until agent returns a final answer
        3. Parse and return the synthesized answer

        Guardrails:
        - Max 5 return control iterations per question (prevents infinite loops)
        - 30s total timeout for the entire ask() call
        - Duplicate tool call detection: skip if same action group + parameters
          were already executed in this loop (prevents repeated searches)
        - If any guardrail is hit, return the best partial answer available
          or a "couldn't complete" message
        """
        ...

    async def _execute_tool(self, action_group: str, function_name: str, parameters: dict) -> "ToolOutput":
        """Execute a backend call and convert the result for the Bedrock Agent.

        1. Map action_group to the appropriate backend
        2. Call backend.query() which returns a BackendResult
        3. Convert BackendResult to ToolOutput for returnControlInvocationResults
        """
        ...

    def _parse_final_response(self, response: dict) -> AgentResponse:
        """Extract the synthesized answer and citations from the agent's final response."""
        ...
```

**Responsibilities**:
- Invoke the Bedrock Agent with user questions
- Handle the return control loop: receive tool requests, execute them locally, send results back
- Map action group names to backend implementations (`SearchConfluenceJira` → Rovo MCP, `SearchGoogleWorkspace` → Google Drive)
- Parse the agent's final synthesized response
- Handle agent errors (throttling, timeout, invalid response)
- Maintain session context per Slack thread (using `session_id` derived from `thread_ts`)

**Session ID Strategy**:
- Thread reply: `session_id` = `{channel_id}:{thread_ts}` — all replies in the same thread share a Bedrock Agent session for conversational context
- Channel mention without existing thread: `session_id` = `{channel_id}:{message_ts}` — the bot's reply starts a new thread, and the message timestamp becomes the thread anchor
- DM with no thread: `session_id` = `{channel_id}:{message_ts}` — each top-level DM starts a fresh session
- Session expiration: Bedrock Agent sessions expire after 1 hour of inactivity (Bedrock default). If a user returns to a thread after the session has expired, the orchestrator starts a fresh session — the user may need to re-state context. This is acceptable for MVP.

**Why return control instead of Lambda?**:
- Keeps backend execution in our application code — no separate Lambda functions to deploy and manage
- Backends (Rovo MCP, Google Drive) need credentials already loaded in the ECS task
- Simpler deployment — one container handles everything
- Easier to test — mock the Bedrock Agent API, test backend execution directly

### Backend Result Model

Both backends return a common `BackendResult` that the orchestrator converts to a `ToolOutput` (Model 3) before sending to the Bedrock Agent.

```python
@dataclass(frozen=True)
class BackendResult:
    """Internal result from a backend query. Not sent to Bedrock directly."""

    backend_name: str
    success: bool
    answer: str | None  # The answer text or search results
    source_urls: list[str]  # Links to source documents
    error_message: str | None  # Error details if success=False
    latency_ms: float
```

The `BedrockAgentOrchestrator._execute_tool` method calls the appropriate backend, receives a `BackendResult`, and converts it to a `ToolOutput` for serialization into the Bedrock Agent's `returnControlInvocationResults`.

### Component 3: Rovo MCP Backend

**Purpose**: Queries Atlassian's Rovo MCP Server to search and summarize Confluence and Jira content. Uses the MCP protocol over HTTPS with API token authentication.

**Interface**:
```python
class RovoMCPBackend:
    """Atlassian Rovo MCP Server integration."""

    def __init__(self, mcp_server_url: str, api_token: str, cloud_id: str):
        """
        Args:
            mcp_server_url: Rovo MCP endpoint (https://mcp.atlassian.com/v1/mcp)
            api_token: Atlassian API token (from Secrets Manager)
            cloud_id: Atlassian Cloud instance ID
        """
        ...

    @property
    def name(self) -> str:
        return "Atlassian Rovo (Confluence/Jira)"

    async def query(self, question: str) -> BackendResult:
        """Search and summarize Confluence/Jira content via Rovo MCP Server."""
        ...

    async def health_check(self) -> bool: ...
```

**Responsibilities**:
- Connect to the Rovo MCP Server at `https://mcp.atlassian.com/v1/mcp`
- Authenticate using Atlassian API token (stored in Secrets Manager)
- Use the official `mcp` Python SDK's `ClientSession` with Streamable HTTP transport to connect to the Rovo MCP Server
- Use MCP tool calls (`call_tool`) to search Confluence and Jira content
- Parse MCP responses and extract answer text and source links
- Handle MCP-specific errors (auth failures, rate limits, timeouts)
- Access is scoped to what the API token owner can see — use a dedicated service account for broad access

### Component 4: Google Drive Backend

**Purpose**: Searches the internal corporate knowledge base — Google Workspace content stored as Drive files (Docs, Sheets, Slides, PDFs) under a shared folder tree — and returns matching documents with snippets and source links. Used by the Bedrock Agent's `SearchGoogleWorkspace` action group.

**Why the Drive API directly, not a Google MCP server?**

Google publishes official remote MCP servers (Drive MCP, Universal Search MCP), but their documented authentication is an **interactive OAuth 2.0 consent flow** — a human signs in and pastes an authorization code. That does not fit an always-on, headless ECS service. The Drive MCP server has no documented service-account / domain-wide-delegation path, and it is currently in Google's Developer Preview Program.

For an unattended backend, the robust, generally-available approach is a **GCP service account with domain-wide delegation (DWD)** calling the **Drive API** (`files.list`) directly. The service account impersonates a dedicated Workspace user (e.g. `sage-kb-chatbot@sagebase.org`) and searches only what that user can see. This mirrors the Rovo model ("whatever the service account can access is what the bot searches"), is GA (no preview dependency), and avoids the operational fragility of long-lived OAuth refresh tokens.

The tradeoff: this backend is a thin Drive REST client rather than an MCP client. Everything downstream is identical — it returns a `BackendResult`, becomes the `SearchGoogleWorkspace` action group, and the Bedrock Agent blends its results with Atlassian into one cited answer.

**Why `files.list` with `fullText contains`?**

The Drive `files.list` query language supports `fullText contains '<terms>'`, which matches the title *and* body text of Docs/Sheets/Slides/PDFs. Each returned file provides `id`, `name`, `mimeType`, `modifiedTime`, and a `webViewLink` — the user-facing URL used for citations.

**Recursive folder scope**

The corporate knowledge base folder tree contains nested subfolders. Drive's `'<folderId>' in parents` clause matches *direct* children only, so it does not cover nested subfolders in a single query. Rather than walking the folder tree on every request (added latency, query-size limits, cache complexity), scope is controlled by **Drive sharing**: the root folder (and, by inheritance, all its subfolders) is shared with the impersonated Workspace user, and nothing broadly else is. The backend then issues `fullText contains '<terms>'` with no `in parents` clause, and Drive naturally confines results to what the impersonated user can see — the whole shared tree, nested subfolders included. This matches the existing "service account sees = bot sees" security model.

> An optional `root_folder_id` config may be added later as defense-in-depth (resolve the descendant folder set and add an `in parents` disjunction), but for MVP sharing-based scoping is the chosen approach.

**Interface**:
```python
class GoogleDriveBackend:
    """Google Drive knowledge-base search via the Drive API.

    Authenticates as a GCP service account using domain-wide
    delegation to impersonate a dedicated Workspace user, then
    searches Drive files with `files.list` + `fullText contains`.
    """

    def __init__(
        self,
        impersonate_user: str,
        service_account_info: dict | None = None,
        scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",),
        max_results: int = 10,
        timeout_seconds: float = 15.0,
    ):
        """
        Args:
            impersonate_user: Workspace user email the service account
                impersonates via domain-wide delegation (e.g. sage-kb-chatbot@sagebase.org).
            service_account_info: Parsed service-account key JSON (from
                Secrets Manager). For the MVP this is required and supplies
                the credentials. If None, credentials fall back to the
                ambient environment (ECS task role / workload identity
                federation) — reserved for future keyless hardening.
            scopes: OAuth scopes to request during delegation. Read-only
                Drive scope by default.
            max_results: Maximum number of files to return per search.
            timeout_seconds: Per-request timeout.
        """
        ...

    @property
    def name(self) -> str:
        return "Google Drive (Corporate KB)"

    async def query(self, question: str) -> BackendResult:
        """Search Drive files and return a BackendResult with answer text and source URLs."""
        ...

    async def health_check(self) -> bool: ...
```

**Responsibilities**:
- Obtain delegated credentials for the impersonated Workspace user using the service account (via `google-auth` `Credentials.with_subject(impersonate_user)`), refreshing access tokens automatically as they expire
- Build a Drive `files.list` request with `q = "fullText contains '<escaped terms>'"`, `fields` limited to `files(id,name,mimeType,modifiedTime,webViewLink)`, `pageSize = max_results`, `spaces = "drive"`, `includeItemsFromAllDrives=true`, `supportsAllDrives=true` (so shared-drive content is covered)
- Run the synchronous Google API client call in a worker thread (`asyncio.to_thread`) so it never blocks the event loop, wrapped in `asyncio.wait_for(timeout_seconds)`
- Parse the response into a `BackendResult`: `answer` is a concise text summary of the matched files (title + snippet), and `source_urls` is the list of `webViewLink` values
- Escape single quotes in the query terms to keep the Drive query syntax valid
- Handle Drive-specific errors and return `BackendResult(success=False, …)`:
  - Auth/delegation failure (misconfigured DWD, wrong scopes) → descriptive error
  - Timeout or HTTP 5xx → descriptive error
  - Rate limit (HTTP 429 / `userRateLimitExceeded`) → descriptive error
- `health_check()` performs a minimal `files.list` with `pageSize=1` (or a token check) to confirm credentials and reachability without consuming meaningful quota
- Access is scoped to what the impersonated user can see — grant that user access only to the corporate KB folder tree

**Authentication model (Path 1: service account + domain-wide delegation)**:
1. A GCP service account is created for the bot.
2. A Workspace super-admin authorizes that service account's client ID for the `drive.readonly` scope in the Admin console (domain-wide delegation). This is an admin-console action and is the gating external dependency.
3. A dedicated Workspace user (e.g. `sage-kb-chatbot@sagebase.org`) is granted read access to the corporate KB Drive folder tree.
4. At runtime the backend builds service-account credentials, calls `.with_subject("sage-kb-chatbot@sagebase.org")` to impersonate that user, and requests Drive read-only scope. All Drive API calls then run as that user.

Credential sourcing (MVP decision: **service-account JSON key**):
- **Service-account JSON key in Secrets Manager (MVP)** — the service account's key JSON is stored in Secrets Manager under `google_service_account_key`; the backend loads it and signs a JWT to obtain access tokens. Simplest to stand up. The key is a long-lived secret that must be protected and rotated periodically.
- **Keyless via the ECS task role (future hardening)** — the ECS task assumes an identity allowed to mint service-account credentials via workload identity federation, avoiding a long-lived key. Preferred for production hardening but out of scope for the MVP because it requires a one-time AWS↔GCP federation trust setup. The `GoogleDriveBackend` interface keeps `service_account_info` optional so this can be adopted later without changing the backend contract.

Note that the bot's own users authenticate to Slack, not to Google — the organization's Google Workspace SSO/IdP is not involved in the backend's authentication. The backend authenticates purely as the GCP service account.

### Component 5: Health Check

**Purpose**: Exposes a lightweight HTTP health endpoint for ECS container health checks. Reports whether the Socket Mode connection is active and backends are reachable.

**Interface**:
```python
from aiohttp import web


class HealthCheck:
    """HTTP health check server for ECS container health checks."""

    def __init__(
        self,
        app: "SlackAgentApp",
        backends: list,
        port: int = 8080,
    ): ...

    async def handle(self, request: web.Request) -> web.Response:
        """Health check endpoint.

        Returns 200 if:
        - Socket Mode WebSocket is connected
        Returns 503 if:
        - WebSocket is disconnected

        Response body includes backend health status (informational,
        does not affect the HTTP status code — a backend being down
        is a degraded state, not unhealthy).
        """
        ...

    async def start(self) -> None:
        """Start the health check HTTP server on the configured port."""
        ...
```

**Response Example** (HTTP 200):
```json
{
  "status": "healthy",
  "websocket": "connected",
  "backends": {
    "Atlassian Rovo": "ok",
    "Google Drive (Corporate KB)": "ok"
  }
}
```

**Responsibilities**:
- Run a lightweight HTTP server on port 8080 (configurable)
- Report WebSocket connection status (determines healthy/unhealthy)
- Report backend reachability via `health_check()` calls (informational only)
- Used by ECS container health check (`curl http://localhost:8080/health`)
- Keep the health check fast (<500ms) — use `asyncio.wait_for(backend.health_check(), timeout=0.5)` for each backend to enforce the timeout contract; report timed-out backends as `"timeout"` in the response body


### Component 6: Structured Logger and Audit Trail

**Purpose**: Provides structured logging for operational visibility and an audit trail of all questions, answers, and backend interactions, with logs shipped to CloudWatch using the application’s standard logging configuration.

**Interface**:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryAuditRecord:
    """Structured audit record for each question-answer cycle."""

    request_id: str
    user_id: str
    channel_id: str
    question: str
    backends_queried: list[str]
    backends_succeeded: list[str]
    backends_failed: list[str]
    agent_model: str | None  # Bedrock Agent model used for orchestration
    answer_length: int  # Character count of final answer
    total_latency_ms: float
    backend_latencies_ms: dict[str, float]
    agent_latency_ms: float | None  # Total Bedrock Agent orchestration time (all InvokeAgent calls)
    rate_limited: bool
    timestamp: str


class AuditLogger:
    """Structured logging and audit trail for all bot interactions."""

    def __init__(self) -> None: ...

    def log_question_received(self, request_id: str, user_id: str, question: str) -> None:
        """Log when a question is received (INFO level)."""
        ...

    def log_backend_result(self, request_id: str, backend_name: str, success: bool, latency_ms: float) -> None:
        """Log individual backend query result (INFO level)."""
        ...

    def log_agent_result(self, request_id: str, success: bool, latency_ms: float, iterations: int) -> None:
        """Log Bedrock Agent orchestration result (INFO level)."""
        ...

    def log_answer_posted(self, record: QueryAuditRecord) -> None:
        """Log the complete audit record when answer is posted (INFO level)."""
        ...

    def log_rate_limited(self, request_id: str, user_id: str, reason: str) -> None:
        """Log when a request is rate-limited (WARNING level)."""
        ...

    def log_error(self, request_id: str, component: str, error: Exception) -> None:
        """Log errors with full context (ERROR level)."""
        ...
```

**Logging Rules**:
- Use structured JSON logging (key-value pairs) for machine-parseable output
- Include `request_id` in every log entry for correlation
- Never log API tokens, secrets, or credentials
- Never log full backend response bodies (may contain sensitive content) — log metadata only
- Set log level to INFO in production, DEBUG in development

**CloudWatch Integration**:
- All logs go to a CloudWatch Log Group (`/ecs/slack-agent-router`)
- Use CloudWatch Logs Insights for querying audit records
- Create metric filters for: question count, error rate, rate-limited count, backend failure count
- Retention: 90 days (configurable)

**Responsibilities**:
- Emit structured JSON logs for every question-answer cycle
- Track per-backend latency and success/failure for operational dashboards
- Provide audit trail of who asked what and what was returned
- Support CloudWatch Logs Insights queries for debugging and analytics
- Log rate-limited requests for abuse detection
- Log WebSocket connection/disconnection events


### Component 7: Rate Limiter

**Purpose**: Enforces per-user and global rate limits to prevent abuse and control Bedrock costs. Uses in-memory counters (suitable for a single ECS task).

**Interface**:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit thresholds."""

    per_user_per_minute: int = 5
    per_user_per_hour: int = 30
    per_user_per_day: int = 100
    per_user_in_flight: int = 1
    global_per_minute: int = 50


class RateLimiter:
    """In-memory rate limiter with sliding window counters."""

    def __init__(self, config: RateLimitConfig | None = None): ...

    def check(self, user_id: str) -> tuple[bool, str | None]:
        """Check if a request is allowed.

        Returns:
            (True, None) if allowed.
            (False, reason) if rate-limited, with a user-friendly reason string.
        """
        ...

    def acquire(self, user_id: str) -> None:
        """Record that a request is being processed."""
        ...

    def release(self, user_id: str) -> None:
        """Record that a request has completed (decrement in-flight)."""
        ...
```

**Responsibilities**:
- Track per-user request counts using sliding window counters
- Enforce 1 in-flight request per user (prevent concurrent queries from same user)
- Enforce per-minute, per-hour, and per-day limits per user
- Enforce global per-minute limit across all users
- Return user-friendly messages when limits are exceeded
- Ensure counters naturally decay as time windows slide (logical reset of old buckets)
- Implement a cleanup strategy (e.g., TTL per user key or periodic eviction of inactive users) to avoid unbounded growth of in-memory state
- Note: in-memory counters also reset on task restart — acceptable for MVP but not a substitute for the cleanup strategy above

## Data Models

### Model 1: Parsed Question

```python
@dataclass(frozen=True)
class ParsedQuestion:
    """Normalized question from any Slack input method."""

    event_type: str
    user_id: str
    channel_id: str
    thread_ts: str | None
    question: str
    team_id: str
    event_ts: str
    request_id: str
```

### Model 2: Backend Configuration

```python
@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single backend."""

    name: str
    enabled: bool
    timeout_seconds: int
    secret_arn: str
```

### Model 3: Tool Output Structure

The structure returned by each backend and sent back to the Bedrock Agent via `returnControlInvocationResults`. This is what the agent uses to synthesize its final answer.

```python
@dataclass(frozen=True)
class ToolOutput:
    """Structured output from a backend tool execution."""

    success: bool
    content: str  # The answer text or search results (plain text)
    sources: list[dict]  # List of source references
    # Each source: {"title": str, "url": str, "system": str}
    error_message: str | None  # Error details if success=False


# Example: successful Rovo MCP result
ToolOutput(
    success=True,
    content="PTO policy allows 20 days per year for full-time employees...",
    sources=[
        {"title": "PTO Policy", "url": "https://confluence.example.com/wiki/pto", "system": "Confluence"},
        {"title": "Leave Tracker", "url": "https://jira.example.com/browse/HR-123", "system": "Jira"},
    ],
    error_message=None,
)

# Example: successful Google Drive result
ToolOutput(
    success=True,
    content="The onboarding checklist covers laptop setup, benefits enrollment, and first-week meetings...",
    sources=[
        {
            "title": "New Hire Onboarding Checklist",
            "url": "https://docs.google.com/document/d/abc123/edit",
            "system": "Google Drive",
        },
        {
            "title": "Benefits Overview 2026",
            "url": "https://docs.google.com/presentation/d/def456/edit",
            "system": "Google Drive",
        },
    ],
    error_message=None,
)
```

**Serialization for Bedrock Agent**: The `ToolOutput` is serialized to a JSON string and sent in the `returnControlInvocationResults` field of the `InvokeAgent` request. The `responseBody` contains the JSON-encoded `content` and `sources` so the agent can cite them in its synthesized answer. On failure, the `responseBody` contains the error message so the agent can note the unavailable source.

### Model 4: Slack Response Format

**Response Format Example** (Slack mrkdwn):
```
*Here's what I found:*

PTO policy allows 20 days per year for full-time employees. Requests should be submitted through Workday at least 2 weeks in advance. The full policy details, including carryover rules and blackout periods, are documented in the PTO Policy page on Confluence.

*Sources:*
1. <https://confluence.example.com/wiki/pto-policy|PTO Policy Page> (Confluence)
2. <https://confluence.example.com/wiki/employee-handbook|Employee Handbook> (Confluence)

_Synthesized from 2 sources in 5.1s_
```


## Error Handling

### Error Scenario 1: WebSocket Disconnection
**Condition**: WebSocket connection to Slack drops.
**Response**: `slack_bolt` automatically reconnects with exponential backoff. Events during the brief gap are lost.
**Recovery**: Automatic. Log disconnection events. CloudWatch alarm on repeated disconnections.

### Error Scenario 2: Single Backend Timeout
**Condition**: One backend doesn't respond within its configured timeout (default 15s).
**Response**: Cancel the timed-out request. Return results from backends that did respond, with a note.
**Recovery**: Next question will try the backend again.

### Error Scenario 3: All Backends Fail
**Condition**: Every backend returns an error or times out.
**Response**: Post: "I wasn't able to find an answer right now. Please try again in a few minutes."
**Recovery**: Log all errors with `request_id`. No automatic retry.

### Error Scenario 4: Slack API Rate Limit
**Condition**: Slack returns HTTP 429 when posting the response.
**Response**: Retry with exponential backoff using `Retry-After` header.
**Recovery**: Up to 3 retries.

### Error Scenario 5: Invalid/Empty Question
**Condition**: User mentions the bot with no question text.
**Response**: Ephemeral message: "Try asking me something like: `@bot What is our PTO policy?`"

### Error Scenario 7: Bedrock Agent Failure
**Condition**: Bedrock Agent returns an error, times out, or gets throttled during the InvokeAgent call or return control loop.
**Response**:
- If the failure occurs before any tool calls: post an error message to the user: "I'm having trouble processing your question right now. Please try again in a few minutes."
- If the failure occurs after one or more successful tool calls (i.e., the orchestrator has cached `ToolOutput` results): fall back to posting the raw tool outputs directly as a concatenated response with source links, prefixed with a note: "I had trouble synthesizing a complete answer, but here's what I found from each source:" This ensures the user still gets value from the backend calls that already succeeded.
**Recovery**: Log the error with `request_id`, which tool outputs were available at failure time, and the iteration count. Alert on repeated failures.

### Error Scenario 8: Rate Limit Exceeded
**Condition**: A user exceeds per-user limits (5/min, 30/hr, 100/day, 1 in-flight) or global limit (50/min).
**Response**: Post an ephemeral message: "You're asking questions a bit too fast. Please wait a moment and try again." No backend queries dispatched, no Bedrock invocation.
**Recovery**: Automatic — limits reset as time windows slide.

### Error Scenario 9: ECS Task Crash / Restart
**Condition**: ECS Fargate task crashes or is replaced during deployment.
**Response**: In-flight questions are lost. ECS restarts the task. New WebSocket connection established.
**Recovery**: ECS maintains desired count. Users can re-ask.


## Testing Strategy

### Unit Tests
- `SlackAgentApp`: Event parsing, empty question rejection, bot mention stripping
- `BedrockAgentOrchestrator`: Return control loop, tool execution dispatch, response parsing, error handling
- `RovoMCPBackend`: MCP response parsing, HTTP error handling
- `GoogleDriveBackend`: `files.list` response parsing (title/snippet/webViewLink → answer + source URLs), query escaping, delegation/auth failure handling, timeout handling, `health_check` returns boolean
- Use `pytest` with `pytest-asyncio`, target 80%+ coverage

### Property-Based Tests (`hypothesis`)
- `BedrockAgentOrchestrator` correctly maps action group names to backend implementations for any valid action group
- Response formatting never exceeds Slack's 3000-character block limit

### Integration Tests
- Backend integration against sandbox instances (Rovo MCP Server with test credentials; Google Drive API against a test folder with the delegated service account)
- Slack message posting with a dedicated test channel
- WebSocket reconnection behavior


## Application Entrypoint

The `main.py` entrypoint ties all components together on a single asyncio event loop: Socket Mode listener, health check server, and graceful shutdown via signal handlers.

```python
import asyncio
import signal
from functools import partial


async def main() -> None:
    # Load secrets from Secrets Manager
    secrets = await load_secrets()

    # Initialize backends
    rovo_backend = RovoMCPBackend(
        mcp_server_url="https://mcp.atlassian.com/v1/mcp",
        api_token=secrets["atlassian_api_token"],
        cloud_id=secrets["atlassian_cloud_id"],
    )
    google_drive_backend = GoogleDriveBackend(
        impersonate_user=config["google_impersonate_user"],
        # MVP: service-account key JSON stored in Secrets Manager.
        service_account_info=secrets["google_service_account_key"],
    )
    backends = [rovo_backend, google_drive_backend]

    # Initialize components
    orchestrator = BedrockAgentOrchestrator(
        agent_id=secrets["bedrock_agent_id"],
        agent_alias_id=secrets["bedrock_agent_alias_id"],
        rovo_backend=rovo_backend,
        google_drive_backend=google_drive_backend,
    )
    rate_limiter = RateLimiter()
    app = SlackAgentApp(
        bot_token=secrets["slack_bot_token"],
        app_token=secrets["slack_app_token"],
        orchestrator=orchestrator,
        rate_limiter=rate_limiter,
    )
    health = HealthCheck(app=app, backends=backends)

    # Register graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown = partial(_shutdown, app=app, health=health)
    loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(shutdown()))
    loop.add_signal_handler(signal.SIGINT, lambda: loop.create_task(shutdown()))

    # Start health check and Socket Mode concurrently
    await asyncio.gather(
        health.start(),
        app.start(),
    )


async def _shutdown(app: SlackAgentApp, health: HealthCheck) -> None:
    """Drain in-flight requests and disconnect."""
    await app.stop()
    # Health check server stops with the process


if __name__ == "__main__":
    asyncio.run(main())
```

This ensures the WebSocket listener, health check HTTP server, and signal handlers all share the same event loop.


## Performance Considerations

- **Backend query sequencing**: The Bedrock Agent controls tool call order via return control. Tool calls are executed sequentially as the agent returns them. Total latency = sum of all tool calls + agent reasoning time. Typical flow: ~1s agent reasoning + ~3-5s per tool call + ~3-5s final synthesis. Future optimization: if the agent returns multiple tool requests in one response, they could be executed concurrently with `asyncio.gather`.
- **ECS task sizing**: 0.25 vCPU, 0.5 GB memory (sufficient for HTTP calls)
- **Timeouts**: 15s per backend, 30s total for the Bedrock Agent return control loop
- **Performance budget**: Median < 10s, p95 < 18s (backend queries ~3-8s + LLM synthesis ~2-5s)
- **Connection stability**: `slack_bolt` handles keepalive and reconnection automatically
- **Graceful shutdown**: Register `SIGTERM` and `SIGINT` handlers via `asyncio.get_event_loop().add_signal_handler()` to drain in-flight requests before disconnecting the WebSocket. ECS sends `SIGTERM` with a configurable stop timeout (default 30s) before force-killing the task — ensure in-flight questions complete or are abandoned within that window.


## Security Considerations

- **Authorization model**: All Sage Bionetworks employees who can interact with the Slack bot have equal access to all content the service accounts can see. There is no per-user access filtering. The Rovo MCP backend uses a dedicated Atlassian service account, and the Google Drive backend impersonates a dedicated Workspace user via domain-wide delegation — whatever those identities can access is available to every user of the bot. Scope the Google Drive service account narrowly: the impersonated user should be granted access only to the corporate KB folder tree, and domain-wide delegation should be restricted to the single `drive.readonly` scope. External collaborators must be blocked from using the bot. The Socket Mode App shall check the user's Slack User Group membership (e.g., `sage-all`) before processing any question — if the user is not in an authorized group, respond with an ephemeral message ("Sorry, this bot is only available to Sage staff") and skip processing. This check runs after event deduplication and before rate limiting. This is a deliberate MVP simplification; per-user content-level access control is out of scope.
- **No public inbound HTTP endpoint**: Socket Mode uses an outbound WebSocket connection. Only an internal/container-local HTTP health check is exposed; no public URL is reachable from the internet.
- **Secret management**: All tokens in AWS Secrets Manager. Never in env vars or code. For the MVP the Google service-account key JSON is stored in Secrets Manager (`google_service_account_key`) alongside the Slack and Atlassian credentials. This is a long-lived credential — treat it as high-value, restrict access to the secret, and rotate it periodically. (Future hardening: keyless via ECS task role → workload identity federation removes this stored key entirely.)
- **Domain-wide delegation blast radius**: DWD lets the service account impersonate the configured user with `drive.readonly`. Restrict the authorized scope to exactly `drive.readonly`, impersonate only the dedicated KB user, and never grant the service account broader scopes. Because the MVP uses a downloaded service-account key, that key is the single highest-value secret in the system: anyone holding it can impersonate the KB user and read all shared content until the key is rotated. Rotate it on a schedule and revoke immediately on suspected exposure.
- **Least privilege IAM**: ECS task role gets only `secretsmanager:GetSecretValue`, `bedrock:InvokeAgent`, and `logs:PutLogEvents`.
- **Audit logging**: Structured JSON logs to CloudWatch include question text, backend results metadata, and latency. Logs retained for 90 days. No full backend response bodies logged. No secrets or credentials in logs.
- **No persistent data store**: No database. Audit records live in CloudWatch Logs only. Questions and answers not stored beyond log retention.
- **Input sanitization**: Strip Slack formatting before sending to backends. Sanitize responses before posting.


## Dependencies

### Python Packages
- `slack-bolt` — Slack Bolt framework with Socket Mode support
- `slack-sdk` — Slack Web API client
- `httpx` — Async HTTP client for MCP and API calls
- `aiohttp` — Lightweight HTTP server for health check endpoint
- `mcp` — Official MCP Python SDK (includes `ClientSession` for connecting to MCP servers as a client)
- `pydantic` — Input validation and settings management
- `boto3` — AWS SDK (Secrets Manager, Bedrock Runtime)
- `google-api-python-client` — Google Drive API client (`files.list`)
- `google-auth` — Service-account credentials with domain-wide delegation (`.with_subject()`)

### AWS Services
- ECS Fargate — Socket Mode listener (always-on, 1 task minimum)
- Amazon Bedrock — Managed Agent with return control (orchestration, routing, synthesis)
- Secrets Manager — API tokens and credentials
- CloudWatch — Logs, metrics, alarms
- CDK (Python) — Infrastructure as code

### External Services
- Slack API — Socket Mode WebSocket, Web API for posting messages
- Atlassian Rovo MCP Server (`mcp.atlassian.com`) — Search and summarize Confluence/Jira content via MCP protocol
- Google Drive API (`drive.googleapis.com`) — Search corporate knowledge-base files via `files.list`, authenticated as a GCP service account using domain-wide delegation

### External Configuration Dependencies
- **Google Cloud project** with the Drive API (`drive.googleapis.com`) enabled
- **GCP service account** for the bot, with **domain-wide delegation** authorized for the `drive.readonly` scope by a Google Workspace super-admin (Admin console action — gating dependency)
- **Dedicated Workspace user** (e.g. `sage-kb-chatbot@sagebase.org`) that the service account impersonates, granted read access to the corporate KB Drive folder tree

> ⚠️ **PREREQUISITE / BLOCKER — must be completed before the Google Drive backend can function.**
>
> Domain-wide delegation (DWD) authorization is a manual **Google Workspace super-admin** action performed in the Admin console; it cannot be provisioned by CDK or application code. Until it is in place, `GoogleDriveBackend` cannot authenticate and every Google Drive query will fail with a delegation/auth error. The backend degrades gracefully (returns `BackendResult(success=False, …)` so the agent can still answer from Confluence/Jira), but no Google Workspace content is searchable until this is done.
>
> Required manual steps (owner: Google Workspace super-admin + GCP project admin):
> 1. Create the GCP service account and note its **client ID** (OAuth2 client ID / unique ID).
> 2. In the Google Workspace Admin console → **Security → Access and data control → API controls → Domain-wide delegation**, add the service account's client ID and authorize **exactly** the scope `https://www.googleapis.com/auth/drive.readonly` (no broader scopes).
> 3. Create/choose the dedicated impersonated Workspace user (e.g. `sage-kb-chatbot@sagebase.org`).
> 4. Share the corporate KB root Drive folder (which cascades to nested subfolders) with that user, read-only. Nothing broader should be shared with it — this defines the bot's Google search scope.
> 5. Create a service-account **key** (JSON) for the service account, store it in Secrets Manager under `google_service_account_key`, and record the impersonated user email as the `google_impersonate_user` config value.


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do.*

### Property 1: Event deduplication prevents reprocessing

*For any* event ID (event_id, envelope_id, or trigger_id) submitted to the deduplication cache, checking the same ID again within the 60-second TTL window SHALL return "duplicate" and the event SHALL be skipped, while any previously unseen ID SHALL be accepted for processing.

**Validates: Requirements 1.5, 1.6**

### Property 2: Bot mention prefix stripping

*For any* app_mention event text containing a bot mention prefix (e.g., `<@BOT_ID>`), the extracted question text SHALL not contain the bot mention prefix and SHALL preserve the remainder of the message text unchanged.

**Validates: Requirement 1.1**

### Property 3: Per-user rate limit window enforcement

*For any* user and any rate limit window (5/minute, 30/hour, 100/day), if the user has made N requests equal to the window limit within the window period, the next request SHALL be rejected with a non-empty reason string identifying the exceeded limit.

**Validates: Requirements 3.1, 3.2, 3.3, 3.6**

### Property 4: Per-user in-flight concurrency limit

*For any* user, if a request is currently in-flight (acquired but not released), a subsequent request from the same user SHALL be rejected. After the in-flight request is released, the next request SHALL be accepted.

**Validates: Requirement 3.4**

### Property 5: Global rate limit enforcement

*For any* set of users, if the total number of requests across all users within a one-minute window reaches 50, the next request from any user SHALL be rejected.

**Validates: Requirement 3.5**

### Property 6: Return control loop iteration bound

*For any* sequence of return control responses from the Bedrock Agent, the orchestrator SHALL execute at most 5 iterations of the return control loop before terminating, regardless of whether the agent continues requesting tool calls.

**Validates: Requirement 5.3**

### Property 7: Return control loop duplicate tool call detection

*For any* sequence of tool invocation requests within a single return control loop, if the same (action_group, parameters) pair is requested more than once, the duplicate request SHALL be skipped and the previously cached result SHALL be used.

**Validates: Requirement 5.5**

### Property 8: Action group to backend mapping correctness

*For any* valid action group name returned by the Bedrock Agent, the orchestrator SHALL dispatch to the correct backend implementation: SearchConfluenceJira maps to Rovo_Backend, and SearchGoogleWorkspace maps to GoogleDrive_Backend.

**Validates: Requirement 5.7**

### Property 9: Session ID derivation from Slack thread context

*For any* ParsedQuestion, the derived session_id SHALL follow the format "{channel_id}:{thread_ts}" when thread_ts is present, or "{channel_id}:{message_ts}" when thread_ts is absent, ensuring all messages in the same thread share a Bedrock Agent session.

**Validates: Requirements 6.1, 6.2, 6.3**

### Property 10: Rovo MCP response parsing completeness

*For any* valid MCP response from the Rovo MCP Server, the Rovo_Backend SHALL produce a BackendResult where success is True, answer contains the extracted text content, and source_urls contains all document links from the MCP response.

**Validates: Requirement 7.2**

### Property 11: Google Drive response parsing completeness

*For any* valid `files.list` response from the Google Drive API, the GoogleDrive_Backend SHALL produce a BackendResult where success is True, answer contains text derived from every returned file (title and, when present, content snippet), and source_urls contains the `webViewLink` of every returned file.

**Validates: Requirement 8.2**

### Property 12: Answer formatting includes all required components

*For any* AgentResponse with non-empty answer text and source URLs, the formatted Slack mrkdwn string SHALL contain the answer text, every source URL as a numbered link with its system label, and a latency footer showing elapsed time.

**Validates: Requirement 9.1**

### Property 13: Partial failure fallback includes all successful tool outputs

*For any* set of successful ToolOutputs collected before a Bedrock Agent failure, the fallback response SHALL include the content and source links from every successful ToolOutput, ensuring no successfully retrieved information is lost.

**Validates: Requirement 10.7**

### Property 14: Audit log structure and completeness

*For any* QueryAuditRecord, the emitted log entry SHALL be valid JSON containing all required fields: request_id, user_id, channel_id, question, backends_queried, backends_succeeded, backends_failed, answer_length, total_latency_ms, backend_latencies_ms, and timestamp.

**Validates: Requirements 12.1, 12.2, 12.4**

### Property 15: No secrets in log output

*For any* log entry emitted by the Audit_Logger, the output SHALL not contain API tokens, secrets, credentials, or full backend response bodies, even if such values are present in the input data being logged.

**Validates: Requirement 12.6**

### Property 16: Slack formatting markup stripping

*For any* Slack-formatted message text containing markup (bold, italic, strikethrough, links, user mentions, channel mentions, code blocks, or emoji shortcodes), the stripping function SHALL remove all markup syntax and return plain text preserving the readable content.

**Validates: Requirement 15.1**

### Property 17: Backend response content sanitization

*For any* backend response content string, the sanitization function SHALL neutralize potentially dangerous content (e.g., Slack mrkdwn injection, excessive formatting) before the content is posted to Slack.

**Validates: Requirement 15.2**
