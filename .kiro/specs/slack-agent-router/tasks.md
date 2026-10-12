# Implementation Plan: Slack Agent Router

## Overview

Implement a Slack chatbot that receives questions via Socket Mode and uses an Amazon Bedrock Agent (return control pattern) to route queries to two knowledge backends — the Rovo MCP backend (Confluence/Jira) and the Google Drive backend (internal corporate knowledge base) — synthesize a single answer across both, and post cited responses. The system runs as a single ECS Fargate service deployed via AWS CDK (Python).

## Tasks

- [x] 1. Set up project structure, data models, and shared utilities
  - [x] 1.1 Create project directory structure and configuration files
    - Create `src/slack_agent_router/` package with `__init__.py`
    - Create `pyproject.toml` with dependencies: slack-bolt, slack-sdk, httpx, aiohttp, mcp, pydantic, boto3, hypothesis, pytest, pytest-asyncio
    - Create `Dockerfile` for the ECS Fargate container
    - _Requirements: 14.1_

  - [x] 1.2 Implement data models (ParsedQuestion, BackendResult, ToolOutput, AgentResponse, BackendConfig, RateLimitConfig, QueryAuditRecord)
    - Create `src/slack_agent_router/models.py` with all frozen dataclasses
    - ParsedQuestion: event_type, user_id, channel_id, thread_ts, question, team_id, event_ts, request_id
    - BackendResult: backend_name, success, answer, source_urls, error_message, latency_ms
    - ToolOutput: success, content, sources (list of dicts), error_message
    - AgentResponse: answer, source_urls, tool_calls_made, latency_ms
    - BackendConfig: name, enabled, timeout_seconds, secret_arn
    - RateLimitConfig: per_user_per_minute, per_user_per_hour, per_user_per_day, per_user_in_flight, global_per_minute
    - QueryAuditRecord: all audit fields as defined in design
    - _Requirements: 9.1, 12.4_

  - [x] 1.3 Write property tests for input sanitization (RED)
    - **Property 16: Slack formatting markup stripping** — for any Slack-formatted text, stripping removes all markup and preserves readable content
    - **Property 17: Backend response content sanitization** — for any backend response, sanitization neutralizes dangerous content
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 15.1, 15.2**

  - [x] 1.4 Implement input sanitization utilities (GREEN)
    - Create `src/slack_agent_router/sanitize.py`
    - Implement `strip_slack_formatting(text: str) -> str` to remove Slack mrkdwn markup (bold, italic, strikethrough, links, user/channel mentions, code blocks, emoji shortcodes) and return plain text
    - Implement `sanitize_backend_response(content: str) -> str` to neutralize dangerous content before posting to Slack
    - Run property tests from 1.3 — all must pass
    - _Requirements: 15.1, 15.2_

  - [x] 1.5 Write property tests for answer formatting (RED)
    - **Property 12: Answer formatting includes all required components** — for any AgentResponse with non-empty answer and sources, the formatted string contains the answer, every source URL as a numbered link, and a latency footer
    - **Property 13: Partial failure fallback includes all successful tool outputs** — for any set of successful ToolOutputs, the fallback response includes content and source links from every one
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 9.1, 10.7**

  - [x] 1.6 Implement answer formatting utility (GREEN)
    - Create `src/slack_agent_router/formatter.py`
    - Implement `format_answer(response: AgentResponse, elapsed_seconds: float) -> str` that produces Slack mrkdwn with answer text, numbered source links with system labels, and latency footer
    - Implement `format_fallback_answer(tool_outputs: list[ToolOutput]) -> str` for partial failure fallback
    - Run property tests from 1.5 — all must pass
    - _Requirements: 9.1, 10.7_

- [x] 2. Implement structured logging and audit trail
  - [x] 2.1 Write property tests for audit logging (RED)
    - **Property 14: Audit log structure and completeness** — for any QueryAuditRecord, the emitted log is valid JSON with all required fields
    - **Property 15: No secrets in log output** — for any log entry, the output does not contain API tokens, secrets, or credentials even if present in input data
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 12.1, 12.2, 12.4, 12.6**

  - [x] 2.2 Implement AuditLogger (GREEN)
    - Create `src/slack_agent_router/audit_logger.py`
    - Configure structured JSON logging using Python `logging` module
    - Implement log_question_received, log_backend_result, log_agent_result, log_answer_posted, log_rate_limited, log_error methods
    - Include request_id in every log entry
    - Log WebSocket connection/disconnection events
    - Never log secrets, credentials, or full backend response bodies
    - Run property tests from 2.1 — all must pass
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

- [x] 3. Implement rate limiter
  - [x] 3.1 Write property tests for rate limiter (RED)
    - **Property 3: Per-user rate limit window enforcement** — for any user at the window limit, the next request is rejected with a non-empty reason
    - **Property 4: Per-user in-flight concurrency limit** — if a request is in-flight, subsequent requests are rejected; after release, next request is accepted
    - **Property 5: Global rate limit enforcement** — when total requests across all users reach 50/min, the next request is rejected
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

  - [x] 3.2 Implement RateLimiter with sliding window counters (GREEN)
    - Create `src/slack_agent_router/rate_limiter.py`
    - Implement sliding window counters for per-user per-minute (5), per-hour (30), per-day (100) limits
    - Implement per-user in-flight concurrency limit (1)
    - Implement global per-minute limit (50)
    - Implement check(), acquire(), release() methods
    - Implement cleanup strategy (TTL per user key or periodic eviction) to prevent unbounded memory growth
    - Return user-friendly reason strings when limits are exceeded
    - Run property tests from 3.1 — all must pass
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement Rovo MCP backend
  - [x] 5.1 Write tests for RovoMCPBackend (RED)
    - **Property 10: Rovo MCP response parsing completeness** — for any valid MCP response, the backend produces a BackendResult with success=True, answer text, and all source URLs
    - Unit tests: auth failure returns BackendResult with success=False, timeout returns BackendResult with success=False, health_check returns boolean
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 7.2, 7.3, 7.4**

  - [x] 5.2 Implement RovoMCPBackend (GREEN)
    - Create `src/slack_agent_router/backends/rovo.py`
    - Connect to Rovo MCP Server using the `mcp` Python SDK's ClientSession with Streamable HTTP transport
    - Authenticate using Atlassian API token from Secrets Manager
    - Implement query() method that calls MCP tools and returns BackendResult with answer text and source URLs
    - Implement health_check() method
    - Handle MCP-specific errors: auth failures, rate limits, timeouts
    - Run tests from 5.1 — all must pass
    - _Requirements: 7.1, 7.2, 7.3, 7.4_


- [x] 7. Implement Bedrock Agent orchestrator
  - [x] 7.1 Write tests for orchestrator (RED)
    - **Property 6: Return control loop iteration bound** — the orchestrator executes at most 5 iterations regardless of agent behavior
    - **Property 7: Return control loop duplicate tool call detection** — duplicate (action_group, parameters) pairs are skipped and cached results reused
    - **Property 8: Action group to backend mapping correctness** — SearchConfluenceJira maps to Rovo_Backend
    - **Property 9: Session ID derivation from Slack thread context** — session_id follows "{channel_id}:{thread_ts}" or "{channel_id}:{message_ts}" format
    - Unit tests: agent failure before tool calls returns error message, agent failure after successful tool calls returns fallback with raw outputs, timeout enforcement
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 5.3, 5.4, 5.5, 5.6, 5.7, 6.1, 6.2, 6.3, 10.6, 10.7**

  - [x] 7.2 Implement BedrockAgentOrchestrator (GREEN)
    - Create `src/slack_agent_router/orchestrator.py`
    - Implement ask() method with the return control loop: invoke agent → receive tool requests → execute locally → send results back → repeat until final answer
    - Map action group names to backends: SearchConfluenceJira → RovoMCPBackend
    - Enforce max 5 return control iterations
    - Enforce 30-second total timeout for ask()
    - Detect and skip duplicate tool calls (same action_group + parameters)
    - On guardrail trigger, return best partial answer or "couldn't complete" message
    - Implement _execute_tool() to dispatch to correct backend and convert BackendResult to ToolOutput
    - Implement _parse_final_response() to extract answer and citations
    - Cache successful ToolOutputs for fallback on agent failure
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 10.7_

  - [x] 7.3 Implement session ID derivation (GREEN)
    - Implement session_id logic: thread reply → "{channel_id}:{thread_ts}", channel mention → "{channel_id}:{message_ts}", DM → "{channel_id}:{message_ts}"
    - Run all tests from 7.1 — all must pass
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement Slack Socket Mode application
  - [x] 9.1 Write tests for SlackAgentApp (RED)
    - **Property 1: Event deduplication prevents reprocessing** — submitting the same event ID within 60s returns duplicate; unseen IDs are accepted
    - **Property 2: Bot mention prefix stripping** — for any text with bot mention prefix, the extracted question does not contain the prefix and preserves the rest
    - Unit tests: event parsing for app_mention, DM, and slash command; empty question rejection with ephemeral message; unauthorized user receives ephemeral rejection; rate-limited user receives ephemeral message
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 2.2, 3.7, 10.5**

  - [x] 9.2 Implement SlackAgentApp with event handlers (GREEN)
    - Create `src/slack_agent_router/slack_app.py`
    - Initialize AsyncApp with bot_token and AsyncSocketModeHandler with app_token
    - Register handlers for app_mention, message (DM filtered by channel_type="im"), and /sage-ask slash command
    - Strip bot mention prefix from app_mention text
    - Acknowledge slash commands explicitly within 3 seconds
    - Parse events into ParsedQuestion model
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 9.3 Wire rate limiting, orchestrator, formatting, and error handling into event handlers (GREEN)
    - Check rate limits before dispatching (post ephemeral on limit exceeded)
    - Validate non-empty question text (post ephemeral hint for empty questions)
    - Call orchestrator.ask() and format response
    - Handle all error scenarios: all backends fail, Slack 429 retry, agent failure fallback
    - Post answer as thread reply in Slack mrkdwn format
    - Run all tests from 9.1 — all must pass
    - _Requirements: 3.7, 9.1, 9.2, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

- [x] 10. Implement application entrypoint for end-to-end testing
  - [x] 10.1 Implement main.py entrypoint
    - Create `src/slack_agent_router/main.py`
    - Load secrets from AWS Secrets Manager
    - Initialize all components: backends, orchestrator, rate limiter, SlackAgentApp, HealthCheck
    - Start health check and Socket Mode concurrently with asyncio.gather
    - _Requirements: 14.5_

- [x] 11. End-to-end manual testing checkpoint
  - Run the bot locally with real Slack credentials and verify question → answer flow works end-to-end
  - Test app_mention, DM, and /sage-ask slash command inputs
  - Verify answers are posted as thread replies with citations

- [x] 12. Implement event deduplication, authorization, and progressive UX
  - [x] 12.1 Implement event deduplication (GREEN)
    - Add in-memory TTL cache (60-second window) for event_id/envelope_id deduplication
    - Deduplicate slash commands on trigger_id
    - Skip processing silently for duplicate events
    - _Requirements: 1.5, 1.6_

  - [x] 12.2 Implement authorization check (GREEN)
    - Check user membership in authorized Slack User Group (sage-all) before processing
    - Respond with ephemeral message for unauthorized users
    - Run authorization after deduplication and before rate limiting
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 12.3 Implement progressive UX feedback (GREEN)
    - Add 👀 reaction immediately on question receipt
    - Post "⏳ Thinking..." placeholder message in thread
    - Update placeholder as each backend is searched (e.g., "⏳ Searching Confluence and Jira...")
    - Update placeholder with final answer via chat.update
    - Remove 👀 and add ✅ when answer is posted
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 13. Implement health check server
  - [x] 13.1 Write unit tests for HealthCheck (RED)
    - Test returns 200 when WebSocket connected
    - Test returns 503 when WebSocket disconnected
    - Test backend timeout handling reports "timeout" in response
    - Tests should fail initially (no implementation yet)
    - _Requirements: 11.2, 11.3, 11.5_

  - [x] 13.2 Implement HealthCheck HTTP server (GREEN)
    - Create `src/slack_agent_router/health.py`
    - Run aiohttp server on port 8080 with /health endpoint
    - Return HTTP 200 when WebSocket is connected, HTTP 503 when disconnected
    - Include backend health status in response body (informational, does not affect HTTP status)
    - Enforce 500ms timeout per backend health check using asyncio.wait_for
    - Run tests from 13.1 — all must pass
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 14. Set up the Google service account with domain-wide delegation (PREREQUISITE — manual)
  - **Blocker:** This is a manual Google Workspace super-admin + GCP admin task. It cannot be automated by CDK or application code, and the Google Drive backend (task 15) cannot authenticate until it is complete. Do this first.
  - [ ] 14.1 Provision the GCP service account and authorize domain-wide delegation
    - Enable the Google Drive API (`drive.googleapis.com`) in the Google Cloud project
    - Create a dedicated GCP service account for the bot and record its client ID (OAuth2 client / unique ID)
    - In Google Workspace Admin console → Security → Access and data control → API controls → Domain-wide delegation, add the service account's client ID and authorize **exactly** the scope `https://www.googleapis.com/auth/drive.readonly` (no broader scopes)
    - _Requirements: 8.3_

  - [ ] 14.2 Provision the impersonated Workspace user and scope its Drive access
    - Create or choose the dedicated impersonated Workspace user (e.g. `sage-kb-chatbot@sagebase.org`)
    - Share the corporate KB root Drive folder (cascades to nested subfolders), read-only, with that user — and nothing broader, since this defines the bot's Google search scope
    - Create a service-account key (JSON) and store it in Secrets Manager under `google_service_account_key` (MVP credential path)
    - Record the user email as the `google_impersonate_user` config value
    - _Requirements: 8.3, 8.4_

- [ ] 15. Implement Google Drive backend
  - [ ] 15.1 Add Google dependencies
    - Add `google-api-python-client` and `google-auth` to `pyproject.toml`
    - _Requirements: 8.1_

  - [ ] 15.2 Write tests for GoogleDriveBackend (RED)
    - **Property 11: Google Drive content-fetch completeness** — for any valid `files.list` result paired with per-file export/download content, the backend produces a BackendResult with success=True, an `answer` that includes each matched file's title followed by its retrieved (bounded) content excerpt, and `source_urls` containing every file's `webViewLink`
    - **Property 11b: excerpt byte bound** — for any file content of arbitrary length, the excerpt included in the answer for that file never exceeds `max_content_bytes_per_file`
    - Unit tests: `fullText contains` query is built with the `in parents` allowlist conjunction and single quotes escaped; Google-native files are fetched via `files.export(mimeType="text/plain")` and binary/PDF files via `files.get(alt="media")`; auth/delegation failure returns success=False; timeout (list or fetch phase) returns success=False; HTTP 5xx / rate-limit returns success=False; a single file's export/download failure is skipped (file still listed by title + `webViewLink` with a "content unavailable" note) while other files succeed; empty result set returns success=True with a "no matching documents" answer; `health_check` returns a boolean
    - Mock the Drive API client (`files().list().execute()`, `files().export().execute()` / `files().get_media()`) and `google-auth` delegated credentials — no real network calls
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.6, 8.7, 8.8, 8.9**

  - [ ] 15.3 Implement GoogleDriveBackend (GREEN)
    - Create `src/slack_agent_router/backends/google_drive.py`
    - Build parsed service-account credentials from `service_account_info` (required, loaded from Secrets Manager), then `.with_subject(impersonate_user)` to impersonate the dedicated Workspace user with the `drive.readonly` scope. **Require `service_account_info` for the MVP** — raise a clear configuration error if it is missing rather than falling back to ambient/ADC credentials. AWS external-account credentials returned by ADC (ECS task role / workload identity federation) do not implement `service_account.Credentials.with_subject()`, so the keyless path cannot perform domain-wide delegation as written; defer it until a supported DWD signing/token flow is designed
    - Implement `query()` **phase 1 (find)**: build `files.list` with `q="fullText contains '<escaped terms>' and (<in-parents disjunction over the allowlisted folder set>)"`, `fields="files(id,name,mimeType,modifiedTime,webViewLink)"`, `pageSize=max_results`, `spaces="drive"`, `includeItemsFromAllDrives=true`, `supportsAllDrives=true`; batch the disjunction across calls if it exceeds Drive's query-length limit
    - Implement `query()` **phase 2 (fetch bounded content)**: for each matched file, export Google-native types (Docs/Sheets/Slides) to `text/plain` via `files.export`, download binary/PDF types via `files.get(alt="media")` (reducing PDFs to text), and truncate each excerpt to `max_content_bytes_per_file`; run all sync client calls via `asyncio.to_thread`, per-file fetches concurrently (`asyncio.gather`), wrapped in `asyncio.wait_for(timeout_seconds)`; compose the BackendResult (answer = title + content excerpt per file, source_urls from `webViewLink`)
    - Confine both the search and the content fetch/export to the enforced `root_folder_id` allowlist (resolved/cached descendant folder set); never issue a bare `fullText contains` query without the `in parents` clause
    - Handle auth/delegation, timeout, HTTP 5xx, and rate-limit errors → BackendResult(success=False, …); skip-and-continue on a single file's fetch/export failure rather than failing the whole request
    - Implement `health_check()` with a minimal `files.list` (`pageSize=1`)
    - Run tests from 15.2 — all must pass
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9_

  - [ ] 15.4 Wire GoogleDriveBackend into orchestrator, Slack app, and entrypoint (GREEN)
    - Orchestrator: add `google_drive_backend` constructor param and register `SearchGoogleWorkspace` → GoogleDrive_Backend in the action-group→backend map; extend the Property 8 mapping test to cover it
    - Slack app: add a progress message for the new action group ("⏳ Searching Google Drive...") in the action-group→message map
    - `main.py`: construct `GoogleDriveBackend`, add it to the backends list, and pass it to the orchestrator
    - Config/secrets: add required `google_impersonate_user` and `google_root_folder_id` config values plus the `google_service_account_key` secret; document them in `config.yaml.example`; add `google_service_account_key` to the required secret keys in `main.py` (`_REQUIRED_SECRET_KEYS`) and load the JSON for the backend (MVP credential path)
    - Config/secrets: add `google_impersonate_user` config and the `google_service_account_key` secret; document in `config.yaml.example`; add `google_service_account_key` to the required secret keys in `main.py` (`_REQUIRED_SECRET_KEYS`) and load the JSON for the backend (MVP credential path)
    - _Requirements: 5.7, 8.8_

- [ ] 16. Implement CDK infrastructure stack
  - [x] 16.1 Create CDK app and stacks (infra repo: sage-kb-chatbot-infra)
    - CDK Python app in `sage-kb-chatbot-infra` with Network, ECS, Service, BedrockAgent, and Monitoring stacks
    - ECS Fargate service defined at 256 CPU / 512 MB, single task
    - _Requirements: 14.1, 14.4_

  - [x] 16.2 Configure IAM, secrets, and logging (infra repo: sage-kb-chatbot-infra)
    - ECS task role granted secretsmanager:GetSecretValue (scoped to the app secret) and bedrock:InvokeAgent; logging to CloudWatch
    - App reads a single Secrets Manager secret (`SLACK_AGENT_ROUTER_SECRET_ID`) at runtime containing the Slack tokens and Atlassian API token; Bedrock Agent IDs passed as env vars
    - _Requirements: 14.2, 14.3, 14.5_

  - [ ] 16.3 Re-enable the ECS container health check (infra repo: sage-kb-chatbot-infra)
    - The `container_healthcheck` block in `app.py` is currently commented out ("health check endpoint not implemented yet"); the /health endpoint is now implemented (task 13), so re-enable it against port 8080
    - _Requirements: 14.4_

  - [ ] 16.4 Add the SearchGoogleWorkspace action group to the Bedrock Agent (infra repo: sage-kb-chatbot-infra)
    - In `bedrock_agent_stack.py`, add a second `RETURN_CONTROL` action group `SearchGoogleWorkspace` with a `find_content` function (single `query` string parameter), mirroring `SearchConfluenceJira`
    - Update the agent `instruction` so it knows to use `SearchGoogleWorkspace` for internal corporate knowledge-base questions, and to synthesize a single blended, cited answer when both sources return results
    - Provision the Google service-account key secret plus the `google_impersonate_user` and required `google_root_folder_id` values for the ECS service
    - Update infra unit tests to assert both action groups are configured
    - _Requirements: 5.7, 8.3, 8.8, 14.5_

- [ ] 17. Implement graceful shutdown
  - [ ] 17.1 Implement graceful shutdown signal handling
    - Register SIGTERM and SIGINT handlers via asyncio event loop
    - Drain in-flight requests before disconnecting WebSocket
    - Complete or abandon in-flight questions within ECS stop timeout (30s)
    - _Requirements: 13.1, 13.2_

- [ ] 18. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 19. Implement integration tests
  - [ ] 19.1 Write integration test for full question-to-answer flow
    - Test the complete pipeline: ParsedQuestion → orchestrator.ask() → formatted Slack response
    - Mock Bedrock Agent API responses (return control loop with tool requests and final answer)
    - Mock backend HTTP calls (Rovo MCP) with realistic response fixtures
    - Verify progressive UX calls are made in correct order (reaction → placeholder → update → final)
    - _Requirements: 5.1, 5.2, 9.1, 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ] 19.2 Write integration test for backend error scenarios
    - Test single backend timeout with other backend succeeding — verify partial answer is returned
    - Test all backends failing — verify "unable to find an answer" message is posted
    - Test Bedrock Agent failure after successful tool calls — verify fallback response with raw outputs
    - Test blended synthesis: both Rovo and Google Drive return results — verify the agent receives both tool outputs (mock Drive `files.list` plus per-file `files.export` / `files.get(alt="media")` content alongside Rovo MCP fixtures)
    - _Requirements: 10.2, 10.3, 10.6, 10.7, 8.8_

  - [ ] 19.3 Write integration test for rate limiting and authorization flow
    - Test authorized user flow end-to-end: event → dedup → auth → rate limit → orchestrator → response
    - Test unauthorized user is rejected with ephemeral message before any backend calls
    - Test rate-limited user receives ephemeral message and no backend calls are made
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.7_

  - [ ] 19.4 Write integration test for health check endpoint
    - Start the aiohttp health server and make real HTTP requests to /health
    - Test healthy response when WebSocket mock reports connected
    - Test unhealthy response when WebSocket mock reports disconnected
    - Test backend health timeout handling with slow mock backends
    - _Requirements: 11.1, 11.2, 11.3, 11.5_

- [ ] 20. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- All tasks follow TDD workflow: write tests first (RED), then implement until tests pass (GREEN)
- Test subtasks are labeled (RED) and implementation subtasks are labeled (GREEN)
- All tests (property and unit) are required — none are optional
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The design uses Python throughout — all implementation tasks use Python
