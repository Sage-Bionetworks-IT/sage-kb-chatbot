# Requirements Document

## Introduction

The Slack Agent Router is a chatbot for Sage Bionetworks employees that receives questions via Slack and uses an Amazon Bedrock Agent to route them to external knowledge sources (Atlassian Confluence/Jira via Rovo MCP Server and Google Sites via Vertex AI Search), synthesize results, and return cited answers. The system uses Slack Socket Mode for secure, endpoint-free event reception and runs as a single ECS Fargate service.

## Glossary

- **Socket_Mode_App**: The Slack Bolt application that maintains a WebSocket connection to Slack, receives events, and dispatches questions for processing.
- **Bedrock_Orchestrator**: The component that interacts with the Amazon Bedrock Agent using the return control pattern to route questions, execute tool calls, and obtain synthesized answers.
- **Rovo_Backend**: The backend that queries Atlassian's Rovo MCP Server to search and summarize Confluence and Jira content.
- **Vertex_Backend**: The backend that queries Google Vertex AI Search to search the company Google Sites website.
- **Health_Check_Server**: A lightweight HTTP server that exposes a health endpoint for ECS container health checks.
- **Rate_Limiter**: An in-memory component that enforces per-user and global rate limits using sliding window counters.
- **Audit_Logger**: The structured logging component that emits JSON logs for operational visibility and audit trail.
- **Return_Control_Loop**: The iterative process where the Bedrock Agent requests tool calls, the application executes them locally, and sends results back until a final answer is produced.
- **Progressive_UX**: The feedback pattern of eyes emoji → thinking message → updated answer that keeps users informed during processing.
- **Parsed_Question**: A normalized data model representing a user question from any Slack input method (mention, DM, slash command).

## Requirements

### Requirement 1: Slack Event Reception

**User Story:** As a Sage Bionetworks employee, I want to ask questions via Slack mentions, direct messages, or slash commands, so that I can get answers without leaving my workflow.

#### Acceptance Criteria

1. WHEN a user mentions the bot in a channel, THE Socket_Mode_App SHALL parse the app_mention event and extract the question text with the bot mention prefix stripped.
2. WHEN a user sends a direct message to the bot, THE Socket_Mode_App SHALL parse the message event filtered by channel_type "im" and extract the question text.
3. WHEN a user invokes the /sage-ask slash command, THE Socket_Mode_App SHALL acknowledge the command within 3 seconds and extract the question text from the command payload.
4. WHEN a Slack event envelope is received for app_mention or message events, THE Socket_Mode_App SHALL acknowledge the envelope immediately before handler execution.
5. WHEN a duplicate event is received with a previously seen event_id or envelope_id within a 60-second window, THE Socket_Mode_App SHALL skip processing silently.
6. WHEN a duplicate slash command is received with a previously seen trigger_id, THE Socket_Mode_App SHALL skip processing silently.

### Requirement 2: Authorization

**User Story:** As a system administrator, I want only authorized Sage Bionetworks employees to use the bot, so that external collaborators cannot access internal knowledge sources.

#### Acceptance Criteria

1. WHEN a question is received from a user, THE Socket_Mode_App SHALL check the user's membership in the authorized Slack User Group (sage-all) before processing.
2. IF a user is not a member of the authorized Slack User Group, THEN THE Socket_Mode_App SHALL respond with an ephemeral message ("Sorry, this bot is only available to Sage staff") and skip further processing.
3. THE Socket_Mode_App SHALL perform the authorization check after event deduplication and before rate limiting.

### Requirement 3: Rate Limiting

**User Story:** As a system operator, I want to enforce rate limits on bot usage, so that I can prevent abuse and control Bedrock costs.

#### Acceptance Criteria

1. THE Rate_Limiter SHALL enforce a limit of 5 requests per minute per user using sliding window counters.
2. THE Rate_Limiter SHALL enforce a limit of 30 requests per hour per user using sliding window counters.
3. THE Rate_Limiter SHALL enforce a limit of 100 requests per day per user using sliding window counters.
4. THE Rate_Limiter SHALL enforce a limit of 1 in-flight request per user, preventing concurrent queries from the same user.
5. THE Rate_Limiter SHALL enforce a global limit of 50 requests per minute across all users.
6. IF a user exceeds any rate limit, THEN THE Rate_Limiter SHALL return a user-friendly reason string indicating which limit was exceeded.
7. WHEN a rate limit is exceeded, THE Socket_Mode_App SHALL post an ephemeral message to the user and skip backend queries and Bedrock invocation.
8. THE Rate_Limiter SHALL implement a cleanup strategy for inactive user keys to avoid unbounded growth of in-memory state.

### Requirement 4: Progressive UX Feedback

**User Story:** As a Slack user, I want visual feedback while my question is being processed, so that I know the bot received my question and is working on it.

#### Acceptance Criteria

1. WHEN a question is accepted for processing, THE Socket_Mode_App SHALL immediately add a 👀 (eyes) reaction to the user's message.
2. WHEN processing begins, THE Socket_Mode_App SHALL post a placeholder message with "⏳ Thinking..." in the thread.
3. WHILE the Return_Control_Loop is executing tool calls, THE Socket_Mode_App SHALL update the placeholder message to indicate which backend is being searched (e.g., "⏳ Searching Confluence and Jira...").
4. WHEN the final answer is ready, THE Socket_Mode_App SHALL update the placeholder message with the synthesized answer via chat.update.
5. WHEN the answer is posted, THE Socket_Mode_App SHALL remove the 👀 reaction and add a ✅ reaction.

### Requirement 5: Bedrock Agent Orchestration

**User Story:** As a developer, I want the Bedrock Agent to handle question routing and answer synthesis, so that routing logic and prompt engineering are managed by Bedrock rather than custom code.

#### Acceptance Criteria

1. WHEN a question is dispatched, THE Bedrock_Orchestrator SHALL invoke the Bedrock Agent with the question and a session_id derived from the Slack thread context.
2. WHEN the Bedrock Agent returns control with a tool invocation request, THE Bedrock_Orchestrator SHALL execute the requested backend call locally and send results back via returnControlInvocationResults.
3. THE Bedrock_Orchestrator SHALL enforce a maximum of 5 return control iterations per question to prevent infinite loops.
4. THE Bedrock_Orchestrator SHALL enforce a 30-second total timeout for the entire ask() call.
5. WHEN the same action group and parameters are requested again within the same Return_Control_Loop, THE Bedrock_Orchestrator SHALL skip the duplicate tool call.
6. WHEN any guardrail (max iterations, timeout, duplicate detection) is triggered, THE Bedrock_Orchestrator SHALL return the best partial answer available or a "couldn't complete" message.
7. THE Bedrock_Orchestrator SHALL map action group names to the correct backend implementations (Rovo_Backend for SearchConfluenceJira, Vertex_Backend for SearchGoogleSites).

### Requirement 6: Session Management

**User Story:** As a Slack user, I want the bot to maintain conversational context within a thread, so that I can ask follow-up questions without restating context.

#### Acceptance Criteria

1. WHEN a question is a thread reply, THE Bedrock_Orchestrator SHALL use a session_id of "{channel_id}:{thread_ts}" to maintain conversational context.
2. WHEN a question is a channel mention without an existing thread, THE Bedrock_Orchestrator SHALL use a session_id of "{channel_id}:{message_ts}" to start a new session.
3. WHEN a question is a top-level DM with no thread, THE Bedrock_Orchestrator SHALL use a session_id of "{channel_id}:{message_ts}" to start a fresh session.

### Requirement 7: Rovo MCP Backend

**User Story:** As a Sage Bionetworks employee, I want the bot to search Confluence and Jira content, so that I can find answers from our internal documentation and project tracking.

#### Acceptance Criteria

1. WHEN the Bedrock Agent requests a SearchConfluenceJira tool call, THE Rovo_Backend SHALL query the Rovo MCP Server at the configured endpoint using the MCP Python SDK's ClientSession with Streamable HTTP transport.
2. WHEN the Rovo MCP Server returns results, THE Rovo_Backend SHALL parse the MCP response and return a BackendResult with answer text and source URLs.
3. IF the Rovo MCP Server returns an authentication error, THEN THE Rovo_Backend SHALL return a BackendResult with success=False and a descriptive error message.
4. IF the Rovo MCP Server times out or returns an HTTP error, THEN THE Rovo_Backend SHALL return a BackendResult with success=False and a descriptive error message.

### Requirement 8: Vertex AI Search Backend

**User Story:** As a Sage Bionetworks employee, I want the bot to search the company Google Sites website, so that I can find answers from our public-facing internal documentation.

#### Acceptance Criteria

1. WHEN the Bedrock Agent requests a SearchGoogleSites tool call, THE Vertex_Backend SHALL query the Vertex AI Search API with the configured project, location, and data store.
2. WHEN Vertex AI Search returns results, THE Vertex_Backend SHALL parse the response and return a BackendResult with answer text, AI summary, and source URLs.
3. IF Vertex AI Search returns an API error, THEN THE Vertex_Backend SHALL return a BackendResult with success=False and a descriptive error message.

### Requirement 9: Answer Formatting

**User Story:** As a Slack user, I want answers formatted with citations and source links, so that I can verify the information and explore further.

#### Acceptance Criteria

1. WHEN a synthesized answer is posted, THE Socket_Mode_App SHALL format the response in Slack mrkdwn with the answer text, a numbered list of source links with system labels, and a latency footer.
2. THE Socket_Mode_App SHALL post the answer as a thread reply to the original message.

### Requirement 10: Error Handling

**User Story:** As a Slack user, I want graceful error handling, so that I get useful feedback even when something goes wrong.

#### Acceptance Criteria

1. IF the WebSocket connection to Slack drops, THEN THE Socket_Mode_App SHALL reconnect automatically with exponential backoff.
2. IF a single backend times out within its configured timeout (15 seconds), THEN THE Bedrock_Orchestrator SHALL cancel the timed-out request and continue with results from other backends.
3. IF all backends fail, THEN THE Socket_Mode_App SHALL post a message: "I wasn't able to find an answer right now. Please try again in a few minutes."
4. IF Slack returns HTTP 429 when posting a response, THEN THE Socket_Mode_App SHALL retry with exponential backoff using the Retry-After header, up to 3 retries.
5. IF a user mentions the bot with no question text, THEN THE Socket_Mode_App SHALL respond with an ephemeral message: "Try asking me something like: `@bot What is our PTO policy?`"
6. IF the Bedrock Agent fails before any tool calls are made, THEN THE Socket_Mode_App SHALL post: "I'm having trouble processing your question right now. Please try again in a few minutes."
7. IF the Bedrock Agent fails after one or more successful tool calls, THEN THE Bedrock_Orchestrator SHALL fall back to posting raw tool outputs as a concatenated response with source links, prefixed with: "I had trouble synthesizing a complete answer, but here's what I found from each source:"

### Requirement 11: Health Check

**User Story:** As a system operator, I want a health check endpoint, so that ECS can monitor container health and replace unhealthy tasks.

#### Acceptance Criteria

1. THE Health_Check_Server SHALL expose an HTTP endpoint on port 8080 at /health.
2. WHEN the WebSocket connection is active, THE Health_Check_Server SHALL return HTTP 200 with status "healthy".
3. WHEN the WebSocket connection is disconnected, THE Health_Check_Server SHALL return HTTP 503 with status "unhealthy".
4. THE Health_Check_Server SHALL include backend health status in the response body as informational data that does not affect the HTTP status code.
5. THE Health_Check_Server SHALL complete health check responses within 500 milliseconds, using asyncio.wait_for with a 500ms timeout for each backend health check.

### Requirement 12: Structured Logging and Audit Trail

**User Story:** As a system operator, I want structured logging and an audit trail, so that I can troubleshoot issues, monitor usage, and track who asked what.

#### Acceptance Criteria

1. THE Audit_Logger SHALL emit structured JSON logs with key-value pairs for every question-answer cycle.
2. THE Audit_Logger SHALL include a request_id in every log entry for correlation.
3. THE Audit_Logger SHALL log question received events, backend results, agent results, answer posted events, rate-limited requests, and errors at appropriate log levels (INFO, WARNING, ERROR).
4. THE Audit_Logger SHALL log a complete QueryAuditRecord when an answer is posted, including user_id, channel_id, question, backends queried/succeeded/failed, latencies, and rate_limited status.
5. THE Audit_Logger SHALL log WebSocket connection and disconnection events.
6. THE Audit_Logger SHALL never log API tokens, secrets, credentials, or full backend response bodies.

### Requirement 13: Graceful Shutdown

**User Story:** As a system operator, I want the service to shut down gracefully, so that in-flight questions are completed before the process exits.

#### Acceptance Criteria

1. WHEN a SIGTERM or SIGINT signal is received, THE Socket_Mode_App SHALL drain in-flight requests before disconnecting the WebSocket.
2. WHEN a SIGTERM signal is received, THE Socket_Mode_App SHALL complete or abandon in-flight questions within the ECS stop timeout window (default 30 seconds).

### Requirement 14: Infrastructure Deployment

**User Story:** As a developer, I want the infrastructure defined as code using CDK (Python), so that deployments are repeatable and version-controlled.

#### Acceptance Criteria

1. THE CDK stack SHALL define an ECS Fargate service with 0.25 vCPU and 0.5 GB memory running a single task.
2. THE CDK stack SHALL configure the ECS task role with least-privilege IAM permissions: secretsmanager:GetSecretValue, bedrock:InvokeAgent, and logs:PutLogEvents.
3. THE CDK stack SHALL define a CloudWatch Log Group at /ecs/slack-agent-router with 90-day retention.
4. THE CDK stack SHALL configure the ECS container health check to use the /health endpoint on port 8080.
5. THE CDK stack SHALL store all secrets (Slack tokens, Atlassian API token, GCP service account credentials, Bedrock Agent IDs) in AWS Secrets Manager.

### Requirement 15: Input Validation and Sanitization

**User Story:** As a developer, I want all inputs validated and sanitized, so that the system handles edge cases safely and prevents injection attacks.

#### Acceptance Criteria

1. WHEN a question is extracted from a Slack event, THE Socket_Mode_App SHALL strip Slack formatting markup before sending to backends.
2. WHEN a response is received from a backend, THE Socket_Mode_App SHALL sanitize the content before posting to Slack.
