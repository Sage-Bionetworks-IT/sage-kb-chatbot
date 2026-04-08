# Implementation Plan: Slack Agent Router

## Overview

Implement a Slack chatbot that receives questions via Socket Mode and uses an Amazon Bedrock Agent (return control pattern) to route queries to Rovo MCP and Vertex AI Search backends, synthesize answers, and post cited responses. The system runs as a single ECS Fargate service deployed via AWS CDK (Python).

## Tasks

- [ ] 1. Set up project structure, data models, and shared utilities
  - [x] 1.1 Create project directory structure and configuration files
    - Create `src/slack_agent_router/` package with `__init__.py`
    - Create `pyproject.toml` with dependencies: slack-bolt, slack-sdk, httpx, aiohttp, mcp, google-cloud-discoveryengine, google-auth, pydantic, boto3, hypothesis, pytest, pytest-asyncio
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

  - [ ] 1.4 Implement input sanitization utilities (GREEN)
    - Create `src/slack_agent_router/sanitize.py`
    - Implement `strip_slack_formatting(text: str) -> str` to remove Slack mrkdwn markup (bold, italic, strikethrough, links, user/channel mentions, code blocks, emoji shortcodes) and return plain text
    - Implement `sanitize_backend_response(content: str) -> str` to neutralize dangerous content before posting to Slack
    - Run property tests from 1.3 — all must pass
    - _Requirements: 15.1, 15.2_

  - [ ] 1.5 Write property tests for answer formatting (RED)
    - **Property 12: Answer formatting includes all required components** — for any AgentResponse with non-empty answer and sources, the formatted string contains the answer, every source URL as a numbered link, and a latency footer
    - **Property 13: Partial failure fallback includes all successful tool outputs** — for any set of successful ToolOutputs, the fallback response includes content and source links from every one
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 9.1, 10.7**

  - [ ] 1.6 Implement answer formatting utility (GREEN)
    - Create `src/slack_agent_router/formatter.py`
    - Implement `format_answer(response: AgentResponse, elapsed_seconds: float) -> str` that produces Slack mrkdwn with answer text, numbered source links with system labels, and latency footer
    - Implement `format_fallback_answer(tool_outputs: list[ToolOutput]) -> str` for partial failure fallback
    - Run property tests from 1.5 — all must pass
    - _Requirements: 9.1, 10.7_

- [ ] 2. Implement structured logging and audit trail
  - [ ] 2.1 Write property tests for audit logging (RED)
    - **Property 14: Audit log structure and completeness** — for any QueryAuditRecord, the emitted log is valid JSON with all required fields
    - **Property 15: No secrets in log output** — for any log entry, the output does not contain API tokens, secrets, or credentials even if present in input data
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 12.1, 12.2, 12.4, 12.6**

  - [ ] 2.2 Implement AuditLogger (GREEN)
    - Create `src/slack_agent_router/audit_logger.py`
    - Configure structured JSON logging using Python `logging` module
    - Implement log_question_received, log_backend_result, log_agent_result, log_answer_posted, log_rate_limited, log_error methods
    - Include request_id in every log entry
    - Log WebSocket connection/disconnection events
    - Never log secrets, credentials, or full backend response bodies
    - Run property tests from 2.1 — all must pass
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

- [ ] 3. Implement rate limiter
  - [ ] 3.1 Write property tests for rate limiter (RED)
    - **Property 3: Per-user rate limit window enforcement** — for any user at the window limit, the next request is rejected with a non-empty reason
    - **Property 4: Per-user in-flight concurrency limit** — if a request is in-flight, subsequent requests are rejected; after release, next request is accepted
    - **Property 5: Global rate limit enforcement** — when total requests across all users reach 50/min, the next request is rejected
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

  - [ ] 3.2 Implement RateLimiter with sliding window counters (GREEN)
    - Create `src/slack_agent_router/rate_limiter.py`
    - Implement sliding window counters for per-user per-minute (5), per-hour (30), per-day (100) limits
    - Implement per-user in-flight concurrency limit (1)
    - Implement global per-minute limit (50)
    - Implement check(), acquire(), release() methods
    - Implement cleanup strategy (TTL per user key or periodic eviction) to prevent unbounded memory growth
    - Return user-friendly reason strings when limits are exceeded
    - Run property tests from 3.1 — all must pass
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8_

- [ ] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement Rovo MCP backend
  - [ ] 5.1 Write tests for RovoMCPBackend (RED)
    - **Property 10: Rovo MCP response parsing completeness** — for any valid MCP response, the backend produces a BackendResult with success=True, answer text, and all source URLs
    - Unit tests: auth failure returns BackendResult with success=False, timeout returns BackendResult with success=False, health_check returns boolean
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 7.2, 7.3, 7.4**

  - [ ] 5.2 Implement RovoMCPBackend (GREEN)
    - Create `src/slack_agent_router/backends/rovo.py`
    - Connect to Rovo MCP Server using the `mcp` Python SDK's ClientSession with Streamable HTTP transport
    - Authenticate using Atlassian API token from Secrets Manager
    - Implement query() method that calls MCP tools and returns BackendResult with answer text and source URLs
    - Implement health_check() method
    - Handle MCP-specific errors: auth failures, rate limits, timeouts
    - Run tests from 5.1 — all must pass
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [ ] 6. Implement Vertex AI Search backend
  - [ ] 6.1 Write tests for VertexAISearchBackend (RED)
    - **Property 11: Vertex AI Search response parsing completeness** — for any valid API response, the backend produces a BackendResult with success=True, answer text with AI summary, and all source URLs
    - Unit tests: API error returns BackendResult with success=False, health_check returns boolean
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 8.2, 8.3**

  - [ ] 6.2 Implement VertexAISearchBackend (GREEN)
    - Create `src/slack_agent_router/backends/vertex.py`
    - Query Vertex AI Search API with configured project, location, and data store
    - Authenticate using GCP service account credentials from Secrets Manager
    - Implement query() method that returns BackendResult with answer text, AI summary, and source URLs
    - Implement health_check() method
    - Handle API errors and return BackendResult with success=False
    - Run tests from 6.1 — all must pass
    - _Requirements: 8.1, 8.2, 8.3_

- [ ] 7. Implement Bedrock Agent orchestrator
  - [ ] 7.1 Write tests for orchestrator (RED)
    - **Property 6: Return control loop iteration bound** — the orchestrator executes at most 5 iterations regardless of agent behavior
    - **Property 7: Return control loop duplicate tool call detection** — duplicate (action_group, parameters) pairs are skipped and cached results reused
    - **Property 8: Action group to backend mapping correctness** — SearchConfluenceJira maps to Rovo_Backend, SearchGoogleSites maps to Vertex_Backend
    - **Property 9: Session ID derivation from Slack thread context** — session_id follows "{channel_id}:{thread_ts}" or "{channel_id}:{message_ts}" format
    - Unit tests: agent failure before tool calls returns error message, agent failure after successful tool calls returns fallback with raw outputs, timeout enforcement
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 5.3, 5.4, 5.5, 5.6, 5.7, 6.1, 6.2, 6.3, 10.6, 10.7**

  - [ ] 7.2 Implement BedrockAgentOrchestrator (GREEN)
    - Create `src/slack_agent_router/orchestrator.py`
    - Implement ask() method with the return control loop: invoke agent → receive tool requests → execute locally → send results back → repeat until final answer
    - Map action group names to backends: SearchConfluenceJira → RovoMCPBackend, SearchGoogleSites → VertexAISearchBackend
    - Enforce max 5 return control iterations
    - Enforce 30-second total timeout for ask()
    - Detect and skip duplicate tool calls (same action_group + parameters)
    - On guardrail trigger, return best partial answer or "couldn't complete" message
    - Implement _execute_tool() to dispatch to correct backend and convert BackendResult to ToolOutput
    - Implement _parse_final_response() to extract answer and citations
    - Cache successful ToolOutputs for fallback on agent failure
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 10.7_

  - [ ] 7.3 Implement session ID derivation (GREEN)
    - Implement session_id logic: thread reply → "{channel_id}:{thread_ts}", channel mention → "{channel_id}:{message_ts}", DM → "{channel_id}:{message_ts}"
    - Run all tests from 7.1 — all must pass
    - _Requirements: 6.1, 6.2, 6.3_

- [ ] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Implement Slack Socket Mode application
  - [ ] 9.1 Write tests for SlackAgentApp (RED)
    - **Property 1: Event deduplication prevents reprocessing** — submitting the same event ID within 60s returns duplicate; unseen IDs are accepted
    - **Property 2: Bot mention prefix stripping** — for any text with bot mention prefix, the extracted question does not contain the prefix and preserves the rest
    - Unit tests: event parsing for app_mention, DM, and slash command; empty question rejection with ephemeral message; unauthorized user receives ephemeral rejection; rate-limited user receives ephemeral message
    - Tests should fail initially (no implementation yet)
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 2.2, 3.7, 10.5**

  - [ ] 9.2 Implement SlackAgentApp with event handlers (GREEN)
    - Create `src/slack_agent_router/slack_app.py`
    - Initialize AsyncApp with bot_token and AsyncSocketModeHandler with app_token
    - Register handlers for app_mention, message (DM filtered by channel_type="im"), and /sage-ask slash command
    - Strip bot mention prefix from app_mention text
    - Acknowledge slash commands explicitly within 3 seconds
    - Parse events into ParsedQuestion model
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [ ] 9.3 Implement event deduplication (GREEN)
    - Add in-memory TTL cache (60-second window) for event_id/envelope_id deduplication
    - Deduplicate slash commands on trigger_id
    - Skip processing silently for duplicate events
    - _Requirements: 1.5, 1.6_

  - [ ] 9.4 Implement authorization check (GREEN)
    - Check user membership in authorized Slack User Group (sage-all) before processing
    - Respond with ephemeral message for unauthorized users
    - Run authorization after deduplication and before rate limiting
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ] 9.5 Implement progressive UX feedback (GREEN)
    - Add 👀 reaction immediately on question receipt
    - Post "⏳ Thinking..." placeholder message in thread
    - Update placeholder as each backend is searched (e.g., "⏳ Searching Confluence and Jira...")
    - Update placeholder with final answer via chat.update
    - Remove 👀 and add ✅ when answer is posted
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ] 9.6 Wire rate limiting, orchestrator, formatting, and error handling into event handlers (GREEN)
    - Check rate limits before dispatching (post ephemeral on limit exceeded)
    - Validate non-empty question text (post ephemeral hint for empty questions)
    - Call orchestrator.ask() and format response
    - Handle all error scenarios: all backends fail, Slack 429 retry, agent failure fallback
    - Post answer as thread reply in Slack mrkdwn format
    - Run all tests from 9.1 — all must pass
    - _Requirements: 3.7, 9.1, 9.2, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

- [ ] 10. Implement health check server
  - [ ] 10.1 Write unit tests for HealthCheck (RED)
    - Test returns 200 when WebSocket connected
    - Test returns 503 when WebSocket disconnected
    - Test backend timeout handling reports "timeout" in response
    - Tests should fail initially (no implementation yet)
    - _Requirements: 11.2, 11.3, 11.5_

  - [ ] 10.2 Implement HealthCheck HTTP server (GREEN)
    - Create `src/slack_agent_router/health.py`
    - Run aiohttp server on port 8080 with /health endpoint
    - Return HTTP 200 when WebSocket is connected, HTTP 503 when disconnected
    - Include backend health status in response body (informational, does not affect HTTP status)
    - Enforce 500ms timeout per backend health check using asyncio.wait_for
    - Run tests from 10.1 — all must pass
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 11. Implement application entrypoint and graceful shutdown
  - [ ] 11.1 Implement main.py entrypoint
    - Create `src/slack_agent_router/main.py`
    - Load secrets from AWS Secrets Manager
    - Initialize all components: backends, orchestrator, rate limiter, SlackAgentApp, HealthCheck
    - Start health check and Socket Mode concurrently with asyncio.gather
    - _Requirements: 14.5_

  - [ ] 11.2 Implement graceful shutdown signal handling
    - Register SIGTERM and SIGINT handlers via asyncio event loop
    - Drain in-flight requests before disconnecting WebSocket
    - Complete or abandon in-flight questions within ECS stop timeout (30s)
    - _Requirements: 13.1, 13.2_

- [ ] 12. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement integration tests
  - [ ] 13.1 Write integration test for full question-to-answer flow
    - Test the complete pipeline: ParsedQuestion → orchestrator.ask() → formatted Slack response
    - Mock Bedrock Agent API responses (return control loop with tool requests and final answer)
    - Mock backend HTTP calls (Rovo MCP, Vertex AI Search) with realistic response fixtures
    - Verify progressive UX calls are made in correct order (reaction → placeholder → update → final)
    - _Requirements: 5.1, 5.2, 9.1, 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ] 13.2 Write integration test for backend error scenarios
    - Test single backend timeout with other backend succeeding — verify partial answer is returned
    - Test all backends failing — verify "unable to find an answer" message is posted
    - Test Bedrock Agent failure after successful tool calls — verify fallback response with raw outputs
    - _Requirements: 10.2, 10.3, 10.6, 10.7_

  - [ ] 13.3 Write integration test for rate limiting and authorization flow
    - Test authorized user flow end-to-end: event → dedup → auth → rate limit → orchestrator → response
    - Test unauthorized user is rejected with ephemeral message before any backend calls
    - Test rate-limited user receives ephemeral message and no backend calls are made
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.7_

  - [ ] 13.4 Write integration test for health check endpoint
    - Start the aiohttp health server and make real HTTP requests to /health
    - Test healthy response when WebSocket mock reports connected
    - Test unhealthy response when WebSocket mock reports disconnected
    - Test backend health timeout handling with slow mock backends
    - _Requirements: 11.1, 11.2, 11.3, 11.5_

- [ ] 14. Implement CDK infrastructure stack
  - [ ] 14.1 Create CDK app and stack
    - Create `infra/` directory with CDK Python app
    - Define ECS Fargate service: 0.25 vCPU, 0.5 GB memory, single task
    - Configure container health check using /health endpoint on port 8080
    - _Requirements: 14.1, 14.4_

  - [ ] 14.2 Configure IAM, secrets, and logging
    - Define least-privilege ECS task role: secretsmanager:GetSecretValue, bedrock:InvokeAgent, logs:PutLogEvents
    - Define Secrets Manager secrets for Slack tokens, Atlassian API token, GCP service account credentials, Bedrock Agent IDs
    - Define CloudWatch Log Group at /ecs/slack-agent-router with 90-day retention
    - _Requirements: 14.2, 14.3, 14.5_

- [ ] 15. Final checkpoint - Ensure all tests pass
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
