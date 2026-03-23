# Implementation Tasks — Sage Internal Knowledge Slack Chatbot

## Task 1: Project Scaffolding and CDK Bootstrap

- [ ] Initialize a Python CDK project using `cdk init app --language python`
- [ ] Configure directory structure: `app.py` (entry point), `stacks/`, `constructs/`, `stages/`, `tests/`, `lambda/`, `containers/`
- [ ] Set up `pyproject.toml` or `requirements.txt` with CDK dependencies: `aws-cdk-lib`, `constructs`, `cdk-iam-floyd`
- [ ] Create a Python virtual environment and install dependencies
- [ ] Create the CDK app entry point in `app.py` with environment-specific stack instantiation
- [ ] Configure `.gitignore` for CDK artifacts, `__pycache__`, `.venv`, `cdk.out`
- [ ] Verify `cdk synth` produces a valid empty template

**References**: design.md §Architecture; requirements.md §7, §8

---

## Task 2: VPC and Networking Stack

- [ ] Create `stacks/networking_stack.py` with a VPC construct
- [ ] Configure public, private (with NAT), and isolated subnets across 2 AZs
- [ ] Create VPC endpoints for S3, SQS, Secrets Manager, Bedrock, CloudWatch Logs, ECR (to reduce NAT costs and improve security)
- [ ] Create security groups for: ECS tasks, RDS, OpenSearch, Lambda
- [ ] Export VPC, subnet, and security group references for downstream stacks
- [ ] Write snapshot test `tests/test_networking_stack.py`

**References**: design.md §Security Considerations (private subnets, VPC endpoints); requirements.md §6.1, §12.4

---

## Task 3: PostgreSQL (RDS) Stack and Schema Migrations

- [ ] Create `constructs/database.py` construct for RDS PostgreSQL
- [ ] Configure: Multi-AZ (or single for dev), encryption enabled, private subnet placement, automated backups
- [ ] Store database credentials in Secrets Manager (auto-generated)
- [ ] Create initial SQL migration files for all tables: `users`, `documents`, `chunks`, `connector_status`, `ingestion_runs`, `queries`, `query_sources`, `feedback`
- [ ] Implement unique constraint on `connector_status` (`connector_key`, `connector_scope`, `scope_identifier`, `environment`)
- [ ] Add foreign key relationships: `chunks.document_id → documents.id`, `queries.user_id → users.id`, `query_sources.query_id → queries.id`, `query_sources.document_id → documents.id`, `query_sources.chunk_id → chunks.id`, `feedback.query_id → queries.id`, `feedback.user_id → users.id`, `ingestion_runs.connector_status_id → connector_status.id`
- [ ] Add indexes on: `users.slack_user_id`, `users.email`, `documents.source_system`, `documents.source_document_id`, `connector_status` unique composite, `ingestion_runs.connector_status_id`, `queries.user_id`, `queries.created_at`
- [ ] Choose and configure a migration runner (e.g., Alembic with SQLAlchemy, or Flyway via Lambda custom resource)
- [ ] Write fine-grained assertions test `tests/test_database.py`

**References**: requirements.md §13 (full data model); design.md §Component 6, §Data Models

---

## Task 4: S3 Buckets

- [ ] Create `constructs/document_store.py` construct
- [ ] Create S3 bucket for normalized document snapshots with SSE-KMS encryption
- [ ] Configure lifecycle rules for retention management
- [ ] Block public access
- [ ] Organize key prefix structure: `{source_system}/{document_id}/{version}`
- [ ] Write fine-grained assertions test `tests/test_document_store.py`

**References**: requirements.md §9.1 (step 8), §12.4; design.md §Component 8

---

## Task 5: OpenSearch Service Cluster and Index Setup

- [ ] Create `constructs/search_index.py` construct for OpenSearch Service domain
- [ ] Configure: encryption at rest, node-to-node encryption, private subnet placement, fine-grained access control
- [ ] Size cluster for MVP (data nodes, instance type, EBS volume)
- [ ] Create index template/mapping with all required fields: `document_id`, `chunk_id`, `source_system`, `source_url`, `title`, `content`, `last_updated`, `authoritative_rank`, `visibility_scope`, `acl_tags`, `content_type`, `owner`, `embedding` (k-NN vector field)
- [ ] Set vector dimension to match Amazon Titan Text Embeddings output (1024 for v2, or 1536 for v1 — confirm model choice)
- [ ] Configure k-NN settings (engine: nmslib or faiss, space_type: cosinesimil)
- [ ] Write fine-grained assertions test `tests/test_search_index.py`

**References**: requirements.md §9.5, §9.5.1, §10.1; design.md §Component 5

---

## Task 6: SQS Queues (Async Handoff + DLQ)

- [ ] Create `constructs/query_queue.py` construct
- [ ] Create main SQS queue for async handoff between Lambda and ECS Fargate
- [ ] Create dead-letter queue (DLQ) with `max_receive_count` of 3
- [ ] Configure visibility timeout aligned with RAG processing SLA (~30s)
- [ ] Enable SSE-SQS or SSE-KMS encryption
- [ ] Write fine-grained assertions test `tests/test_query_queue.py`

**References**: requirements.md §7.1, §8.1, §17; design.md §Component 2

---

## Task 7: Secrets Manager Entries

- [ ] Create `constructs/app_secrets.py` construct
- [ ] Define Secrets Manager secrets for: Slack signing secret, Slack bot token, Confluence credentials, Jira credentials (API token or OAuth client credentials), GitHub app credentials, PowerDMS credentials
- [ ] Use KMS CMK for encryption
- [ ] Output secret ARNs for IAM policy grants to Lambda and ECS tasks
- [ ] Write fine-grained assertions test `tests/test_app_secrets.py`

**References**: requirements.md §12.3; design.md §Security Considerations

---

## Task 8: Slack Ingress Lambda Function

- [ ] Create `constructs/slack_ingress.py` construct using `PythonFunction` (or `aws_lambda.Function` with a bundled Python handler)
- [ ] Create handler at `lambda/slack_ingress/handler.py`
- [ ] Implement Slack request signature validation using HMAC-SHA256
- [ ] Parse event types: `app_mention`, `message` (DM), slash command `/sage-ask`
- [ ] Extract: `user_id`, `channel_id`, `text`, `thread_ts`, `event_type`
- [ ] Construct SQS message payload and enqueue to the async handoff queue
- [ ] Return HTTP 200 immediately (within 3 seconds)
- [ ] Handle Slack URL verification challenge (`url_verification` event)
- [ ] Reject unsigned or malformed requests with 401/400
- [ ] Grant Lambda read access to Slack signing secret in Secrets Manager
- [ ] Grant Lambda `sqs:SendMessage` on the query queue
- [ ] Write unit tests `tests/test_slack_ingress.py` for signature validation, event parsing, SQS message formatting

**References**: requirements.md §5.1, §17; design.md §Component 1

---

## Task 9: API Gateway for Slack Webhook

- [ ] Create `constructs/slack_api.py` construct
- [ ] Create REST API Gateway with a `POST /slack/events` endpoint
- [ ] Integrate with the Slack Ingress Lambda (proxy integration)
- [ ] Configure throttling defaults
- [ ] Enable CloudWatch access logging
- [ ] Output the webhook URL for Slack app configuration
- [ ] Write fine-grained assertions test `tests/test_slack_api.py`

**References**: requirements.md §7.1; design.md §Architecture (Ingress Layer)

---

## Task 10: ECS Fargate Cluster and RAG Orchestrator Service

- [ ] Create `constructs/rag_orchestrator.py` construct
- [ ] Create ECS Cluster in the VPC
- [ ] Define Fargate task definition with appropriate CPU/memory (e.g., 1 vCPU, 4 GB)
- [ ] Create container image project structure at `containers/rag_orchestrator/` (Dockerfile, `main.py`, `requirements.txt`)
- [ ] Configure ECS Service with `desired_count=2` (warm tasks), private subnet placement
- [ ] Configure SQS-based auto-scaling (scale on `ApproximateNumberOfMessagesVisible`)
- [ ] Grant task role permissions: SQS consume/delete, Secrets Manager read, OpenSearch data access, RDS connect, Bedrock invoke, Slack API (outbound HTTPS)
- [ ] Configure environment variables / secrets injection for DB connection, OpenSearch endpoint, SQS queue URL, Slack bot token ARN
- [ ] Write fine-grained assertions test `tests/test_rag_orchestrator.py`

**References**: requirements.md §7.1, §17; design.md §Component 3

---

## Task 11: RAG Orchestrator — SQS Consumer and Message Router

- [ ] Implement SQS long-polling consumer in the ECS container application (`containers/rag_orchestrator/`) using `boto3`
- [ ] Parse incoming query messages (user_id, channel_id, text, thread_ts, event_type)
- [ ] Route to appropriate handler: query processing vs. feedback interaction
- [ ] Implement graceful shutdown (drain in-flight messages on SIGTERM)
- [ ] Implement message deletion on successful processing
- [ ] Implement error handling: let visibility timeout expire for retries, log failures

**References**: design.md §Component 3 (Responsibilities); requirements.md §17

---

## Task 12: RAG Orchestrator — Identity Mapping and Authorization

- [ ] Implement user lookup by `slack_user_id` in PostgreSQL `users` table using `psycopg2` or `asyncpg`
- [ ] Create user record on first interaction if not found (with Slack user info)
- [ ] Resolve `identity_groups` for the user
- [ ] Build authorization filter (list of permitted `visibility_scope` and `acl_tags` values)
- [ ] Pass authorization context to the retrieval step

**References**: requirements.md §12.1, §12.2, §5.6; design.md §Component 3

---

## Task 13: RAG Orchestrator — Rate Limiting

- [ ] Implement per-user rate limits: 5/min, 30/hr, 100/day, 1 in-flight
- [ ] Implement per-channel rate limit: 10 per 5 minutes
- [ ] Implement global concurrent RAG job limit: 25
- [ ] Use PostgreSQL or in-memory counters (with ECS task count awareness) for tracking
- [ ] Check rate limits immediately after dequeuing the SQS message; if exceeded, skip all heavy downstream processing (Bedrock, OpenSearch), send user-friendly Slack response, and delete/ack the message from the queue
- [ ] Return user-friendly Slack message when rate-limited
- [ ] Write unit tests for each rate limit tier using `pytest`

**References**: requirements.md §18; design.md §Error Scenario 3

---

## Task 14: RAG Orchestrator — Query Embedding Generation

- [ ] Implement Bedrock client for Amazon Titan Text Embeddings using `boto3`
- [ ] Normalize user query text (trim, lowercase, remove excess whitespace)
- [ ] Call Bedrock `invoke_model` with the Titan Text Embeddings model to generate query vector
- [ ] Implement retry with exponential backoff (up to 3 attempts) on Bedrock errors
- [ ] Return embedding vector for use in hybrid search
- [ ] Write unit tests with mocked Bedrock client using `pytest` and `unittest.mock`

**References**: requirements.md §9.2, §9.5; design.md §Component 7

---

## Task 15: RAG Orchestrator — Hybrid Search (OpenSearch)

- [ ] Implement OpenSearch client using `opensearch-py` with IAM-based or fine-grained access auth
- [ ] Build hybrid query combining: BM25 keyword search on `content`, k-NN vector search on `embedding`, metadata filters (`source_system`, `visibility_scope`, `acl_tags`)
- [ ] Apply authorization filters from the user's identity context
- [ ] Combine lexical and semantic scores (e.g., weighted sum or RRF)
- [ ] Apply source authority ranking boost based on `authoritative_rank`
- [ ] Apply freshness boost based on `last_updated`
- [ ] Return top-K ranked chunks with metadata
- [ ] Write unit tests with mocked OpenSearch client using `pytest`

**References**: requirements.md §9.2, §10.2, §10.3; design.md §Component 5

---

## Task 16: RAG Orchestrator — Prompt Assembly

- [ ] Build system prompt with grounding rules, citation instructions, refusal rules, and confidence assessment instructions
- [ ] Assemble user prompt with: the question, top retrieved chunks (with source metadata), answering rules
- [ ] Include source metadata per chunk: title, source_system, source_url, last_updated
- [ ] Enforce token budget: truncate or drop lowest-ranked chunks if prompt exceeds model context window
- [ ] Implement prompt injection defense: sanitize chunk content (strip hidden markup, suspicious patterns)
- [ ] Write unit tests for prompt construction and sanitization using `pytest`

**References**: requirements.md §9.3, §15.1, §15.2, §12.5; design.md §Component 3

---

## Task 17: RAG Orchestrator — LLM Answer Generation

- [ ] Implement Bedrock client for the generation model using `boto3` (model TBD — e.g., Claude 3 Sonnet/Haiku via Bedrock)
- [ ] Call Bedrock `invoke_model` with the assembled prompt
- [ ] Parse LLM response: extract answer text, cited source references, uncertainty notes
- [ ] Assign confidence level (High/Medium/Low) based on: relevance scores, source agreement, source authority, answer completeness
- [ ] Implement retry with exponential backoff on Bedrock errors
- [ ] Write unit tests with mocked Bedrock client using `pytest`

**References**: requirements.md §9.4, §15.3; design.md §Component 7, §Error Scenario 5

---

## Task 18: RAG Orchestrator — Slack Response Formatting and Posting

- [ ] Format answer as Slack Block Kit message: answer block, confidence block, numbered sources block (title, system, URL), notes block
- [ ] Add interactive buttons: "Helpful", "Not helpful", "View sources", "Report issue"
- [ ] Post message to the correct channel/DM and thread using Slack `chat.postMessage` API (via `slack_sdk`)
- [ ] Handle Slack API errors gracefully (retry once, log failure)
- [ ] Write unit tests for message formatting using `pytest`

**References**: requirements.md §14.1, §14.2, §14.3; design.md §Component 3

---

## Task 19: RAG Orchestrator — Audit Logging

- [ ] After each query, insert a record into `queries` table: user_id, question, answer, confidence, latency_ms
- [ ] Insert records into `query_sources` for each retrieved chunk used: query_id, document_id, chunk_id, rank
- [ ] Ensure logging does not block the response path (fire-and-forget or async)
- [ ] Write unit tests for audit log insertion using `pytest`

**References**: requirements.md §5.5; design.md §Component 6

---

## Task 20: RAG Orchestrator — Feedback Handler

- [ ] Handle Slack interactive payload (button clicks) routed through the SQS consumer
- [ ] Parse feedback type: helpful, not_helpful, report_issue
- [ ] Parse optional user comment (from modal if "Report issue")
- [ ] Insert record into `feedback` table: query_id, user_id, feedback_type, comment
- [ ] Acknowledge feedback to user in Slack (ephemeral message)
- [ ] Write unit tests for feedback parsing and storage using `pytest`

**References**: requirements.md §5.4, §14.3; design.md §Feedback Flow

---

## Task 21: Ingestion Pipeline — EventBridge Schedules

- [ ] Create `constructs/ingestion_scheduler.py` construct
- [ ] Create EventBridge rules for each source with freshness-aligned schedules:
  - Confluence: every 6 hours
  - Slack (public channels): every 1 hour
  - Jira (all projects): every 1 hour
  - GitHub: every 1 hour
  - Intranet: every 12 hours
  - PowerDMS: every 12 hours
- [ ] Each rule targets the corresponding Step Functions state machine
- [ ] Write fine-grained assertions test `tests/test_ingestion_scheduler.py`

**References**: requirements.md §6.4; design.md §Component 4a

---

## Task 22: Ingestion Pipeline — Step Functions Orchestrator

- [ ] Create `constructs/ingestion_orchestrator.py` construct
- [ ] Define Step Functions state machine per connector type
- [ ] Steps: create `ingestion_runs` record (status: running) → launch connector worker ECS task → wait for completion → update `ingestion_runs` status → update `connector_status`
- [ ] Handle worker failures: mark run as failed, increment `consecutive_failures`, log error details
- [ ] Support run types: full, incremental (based on `last_cursor`)
- [ ] Grant Step Functions role permissions to run ECS tasks, write to RDS (via Lambda helper or direct SDK)
- [ ] Write fine-grained assertions test `tests/test_ingestion_orchestrator.py`

**References**: requirements.md §13.0, §13.1 (connector_status, ingestion_runs); design.md §Component 4b, §Ingestion Flow

---

## Task 23: Connector — Base Connector Framework

- [ ] Create shared connector base in `containers/connectors/base_connector/`
- [ ] Define abstract base class with methods: `fetch()`, `normalize()`, `extract_metadata()`, `chunk()`, `embed()`, `index()`, `store_snapshot()`
- [ ] Implement semantic/structure-first chunking logic per requirements §9.6:
  - Split on document boundaries (headings, sections, FAQ items, procedure blocks)
  - Sub-split large sections by paragraph/sentence-safe boundaries
  - Preserve heading path metadata on every chunk
  - Target ~450–650 tokens, hard max ~900 tokens, ~75 token overlap for narrative splits
- [ ] Implement embedding generation via Bedrock Titan Text Embeddings using `boto3` (batch where possible)
- [ ] Implement OpenSearch bulk indexing using `opensearch-py`
- [ ] Implement S3 snapshot storage using `boto3`
- [ ] Implement PostgreSQL document/chunk metadata upsert using `psycopg2` or `SQLAlchemy`
- [ ] Implement change detection via document `hash` comparison
- [ ] Write unit tests for chunking, embedding call, indexing, and change detection using `pytest`

**References**: requirements.md §9.1, §9.6; design.md §Component 4c

---

## Task 24: Connector — Confluence

- [ ] Create `containers/connectors/confluence_connector/` with handler
- [ ] Implement Confluence REST API client (authenticated via Secrets Manager credentials) using `requests` or `httpx`
- [ ] Fetch pages across all spaces: body (storage format), title, canonical URL, last modified, space metadata
- [ ] Implement update detection (compare `last_updated` or content hash)
- [ ] Extract permission metadata where available
- [ ] Normalize Confluence storage format HTML to markdown/text using `beautifulsoup4` or `markdownify`
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Support incremental sync using Confluence CQL `lastModified` filter
- [ ] Create ECS Fargate task definition for this connector (on-demand, cold start OK)
- [ ] Write unit tests for Confluence API parsing, normalization, update detection using `pytest`

**References**: requirements.md §11.1; design.md §Component 4c

---

## Task 25: Connector — Slack

- [ ] Create `containers/connectors/slack_connector/` with handler
- [ ] Implement Slack Conversations API client (authenticated via Slack bot token from Secrets Manager) using `slack_sdk`
- [ ] Enumerate public channels using `conversations.list` (exclude archived channels)
- [ ] For each channel: fetch message history and threaded replies using `conversations.history` and `conversations.replies`
- [ ] Filter out bot messages, ephemeral messages, and system messages (join/leave/topic changes)
- [ ] Extract per-message: text, author (Slack user ID), channel name, thread parent, permalink, timestamp
- [ ] Group threaded conversations into single documents (parent message + replies)
- [ ] Implement incremental sync using cursor-based pagination and `oldest` timestamp parameter (track `last_cursor` per channel)
- [ ] Respect Slack API rate limits (Tier 3: ~50 req/min for conversations.history) with backoff
- [ ] Normalize message content: resolve user mentions (`<@U123>` → display name), channel references, emoji shortcodes
- [ ] Set `authoritative_rank` to 6 (below intranet, above "other supporting content")
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Support channel-scoped ingestion and status tracking (one `connector_status` row per channel)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for Slack API parsing, message normalization, thread grouping, incremental sync using `pytest`

**References**: requirements.md §11.5; design.md §Component 4c

---

## Task 26: Connector — Jira

- [ ] Create `containers/connectors/jira_connector/` with handler
- [ ] Implement Jira REST API client (authenticated via API token or OAuth from Secrets Manager) using `requests` or `httpx`
- [ ] Enumerate all projects using Jira REST API
- [ ] For each project: fetch issues with summary, description, comments, status, priority, labels, assignee, reporter
- [ ] Implement incremental sync using JQL `updated >= "last_cursor"` filter
- [ ] Extract canonical issue URL (permalink) for each issue
- [ ] Normalize Jira wiki markup / ADF (Atlassian Document Format) to markdown/text
- [ ] Concatenate issue description + comments into a single document per issue for chunking
- [ ] Implement update detection via `updated` timestamp comparison
- [ ] Extract permission metadata where available (project-level visibility)
- [ ] Set `authoritative_rank` to 3 (below Confluence, above GitHub)
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Support project-scoped ingestion and status tracking (one `connector_status` row per project)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for Jira API parsing, wiki markup normalization, incremental sync, comment aggregation using `pytest`

**References**: requirements.md §11.6; design.md §Component 4c

---

## Task 27: Connector — GitHub

- [ ] Create `containers/connectors/github_connector/` with handler
- [ ] Implement GitHub API client (GitHub App auth via Secrets Manager) using `PyGithub` or `httpx`
- [ ] Enumerate repos under `Sage-Bionetworks` and `Sage-Bionetworks-IT` orgs
- [ ] For each repo: fetch README, Markdown files in `docs/` directories, repository metadata
- [ ] Implement revision tracking (compare commit SHA or file hash)
- [ ] Support repository-scoped ingestion and status tracking (one `connector_status` row per repo)
- [ ] Normalize Markdown content, preserve code blocks with context
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for GitHub API parsing, repo enumeration, revision detection using `pytest`

**References**: requirements.md §11.2; design.md §Component 4c

---

## Task 28: Connector — Intranet

- [ ] Create `containers/connectors/intranet_connector/` with handler
- [ ] Implement HTML crawl or API fetch (depending on intranet technology) using `requests`/`httpx` and `beautifulsoup4`
- [ ] Remove boilerplate (nav, footer, sidebar) to extract main content
- [ ] Extract canonical URL, content ownership metadata
- [ ] Implement update detection (content hash or last-modified header)
- [ ] Normalize HTML to markdown/text
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for HTML parsing, boilerplate removal, normalization using `pytest`

**References**: requirements.md §11.3; design.md §Component 4c
---

## Task 29: Connector — PowerDMS

- [ ] Create `containers/connectors/powerdms_connector/` with handler
- [ ] Implement PowerDMS API client (authenticated via Secrets Manager) using `requests` or `httpx`
- [ ] Fetch: title, version, approval date, document type, canonical link, document content
- [ ] Implement update detection (version comparison or content hash)
- [ ] Normalize document content to text/markdown
- [ ] Set `authoritative_rank` to highest tier (PowerDMS SOPs are rank 1)
- [ ] Use base connector framework for chunking, embedding, indexing, snapshot storage
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for PowerDMS API parsing, version detection using `pytest`

**References**: requirements.md §11.4, §10.3; design.md §Component 4c

---

## Task 30: Observability — CloudWatch Metrics, Logs, and Alarms

- [ ] Create `constructs/observability.py` construct
- [ ] Configure CloudWatch log groups for: Lambda, ECS tasks (RAG orchestrator + connectors), Step Functions
- [ ] Publish custom metrics: request volume, median/p95 latency, no-answer rate, low-confidence rate, retrieval hit count, ingestion freshness lag, user feedback ratio
- [ ] Create CloudWatch alarms for:
  - Connector failures (consecutive_failures threshold)
  - SQS DLQ message count > 0
  - SQS main queue backlog threshold exceeded
  - Ingestion lag threshold exceeded per source
  - Slack webhook failures (Lambda errors)
  - Bedrock invocation failures
  - OpenSearch cluster health (red/yellow)
  - Elevated low-confidence rate
- [ ] Configure SNS topic for alarm notifications
- [ ] Enable CloudTrail for AWS API audit logging
- [ ] Write fine-grained assertions test `tests/test_observability.py`

**References**: requirements.md §6.5, §19.1, §19.2; design.md §Security Considerations

---

## Task 31: IAM Roles and Least-Privilege Policies

- [ ] Review and tighten all IAM roles created in previous tasks
- [ ] Lambda ingress role: only `sqs:SendMessage`, `secretsmanager:GetSecretValue` (scoped to Slack secret)
- [ ] ECS RAG orchestrator role: `sqs:ReceiveMessage/DeleteMessage`, `secretsmanager:GetSecretValue`, `bedrock:InvokeModel`, OpenSearch data access, RDS connect, `logs:PutLogEvents`
- [ ] ECS connector worker roles: source-specific secrets, `bedrock:InvokeModel`, OpenSearch data write, `s3:PutObject`, RDS connect, `logs:PutLogEvents`
- [ ] Step Functions role: `ecs:RunTask`, `logs:PutLogEvents`
- [ ] Use `cdk-iam-floyd` for policy generation where applicable
- [ ] Validate no `*` resource wildcards in production policies
- [ ] Write fine-grained assertions test `tests/test_iam_policies.py`

**References**: requirements.md §6.1, §12.2; design.md §Security Considerations

---

## Task 32: End-to-End Integration Test

- [ ] Create integration test that exercises the full query flow: Slack event → API Gateway → Lambda → SQS → ECS RAG orchestrator → OpenSearch + Bedrock → Slack response
- [ ] Pre-index a small set of known test documents into OpenSearch
- [ ] Send a test query and verify: answer is grounded, citations are present, confidence is assigned, audit log is written
- [ ] Verify feedback flow: send feedback interaction, confirm it's stored in PostgreSQL
- [ ] Verify rate limiting: send requests exceeding per-user limit, confirm rejection message
- [ ] Verify connector disable: disable a connector, confirm ingestion stops and queries still work for other sources

**References**: requirements.md §21 (MVP Acceptance Criteria); design.md §Testing Strategy

---

## Task 33: CDK Stack Composition and Deployment Configuration

- [ ] Create `stacks/sage_kb_chatbot_stack.py` that composes all constructs
- [ ] Wire cross-construct references (VPC, security groups, secrets ARNs, queue URLs, DB endpoints, OpenSearch domain)
- [ ] Create environment-specific configurations (dev, staging, prod) with appropriate sizing
- [ ] Configure CDK context for account/region values (no `cdk.context.json` for secrets)
- [ ] Verify `cdk synth` produces valid templates for each environment
- [ ] Write snapshot test `tests/test_sage_kb_chatbot_stack.py`

**References**: design.md §Dependencies
