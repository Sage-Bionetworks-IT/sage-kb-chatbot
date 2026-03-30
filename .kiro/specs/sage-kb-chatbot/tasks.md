# Implementation Tasks — Sage Internal Knowledge Slack Chatbot

## Task 0: Prerequisites and Account Setup

- [ ] Create the Slack App in the Sage workspace: configure bot scopes (`app_mentions:read`, `chat:write`, `commands`, `channels:history`, `channels:read`, `groups:read`, `im:history`, `im:read`, `im:write`, `users:read`, `users:read.email`, `usergroups:read`), enable Events API subscription, register slash command `/sage-ask`, configure Interactivity Request URL
- [ ] Request Amazon Bedrock model access in the target AWS account/region for: Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) and the chosen generation model (e.g., Anthropic Claude 3 Sonnet/Haiku)
- [ ] Ensure the OpenSearch Service Linked Role exists in the AWS account (`aws iam create-service-linked-role --aws-service-name es.amazonaws.com` — idempotent, safe to run if already exists)
- [ ] Confirm AWS CDK bootstrap has been run in the target account/region (`cdk bootstrap aws://ACCOUNT/REGION`)
- [ ] Document the `DEPLOY_ENV` values and corresponding AWS account/region mappings for dev, staging, and prod

**References**: requirements.md §8.1; design.md §Dependencies

---

## Task 1: Project Scaffolding and CDK Bootstrap

- [ ] Initialize a Python CDK project using `cdk init app --language python`
- [ ] Configure directory structure: `app.py` (entry point), `stacks/`, `constructs/`, `stages/`, `tests/`, `lambda/`, `containers/`
- [ ] Set up `pyproject.toml` or `requirements.txt` with CDK dependencies: `aws-cdk-lib`, `constructs`, `cdk-iam-floyd`
- [ ] Add LlamaIndex dependencies: `llama-index`, `llama-index-vector-stores-opensearch`, `llama-index-embeddings-bedrock`, `llama-index-llms-bedrock`
- [ ] Add LlamaHub reader dependencies: `llama-index-readers-confluence`, `llama-index-readers-slack`, `llama-index-readers-jira`, `llama-index-readers-github`, `llama-index-readers-web`
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
- [ ] Wire migration execution into the deployment pipeline (e.g., CDK custom resource Lambda or ECS RunTask pre-deploy step) so migrations run automatically on `cdk deploy`
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
- [ ] Set vector dimension to 1024 (Amazon Titan Text Embeddings V2: `amazon.titan-embed-text-v2:0`)
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
- [ ] Create DLQ notification Lambda (`lambda/dlq_notifier/handler.py`):
  - Triggered by SQS DLQ (event source mapping)
  - Parse original message (user_id, channel_id, thread_ts)
  - Send a "Sorry, I wasn't able to process your question. Please try again later." Slack message to the user in the original channel/thread
  - Log the failure details (query text, error context) for operator review
  - Grant Lambda read access to Slack bot token in Secrets Manager and `sqs:ReceiveMessage/DeleteMessage` on the DLQ
- [ ] Create `constructs/dlq_notifier.py` CDK construct wrapping the Lambda and SQS event source mapping
- [ ] Write unit tests `tests/test_dlq_notifier.py` for message parsing, Slack notification, and error logging
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
- [ ] Handle Slack interactive payloads (button clicks from feedback controls): parse `payload` JSON from `application/x-www-form-urlencoded` body, extract `action_id`, `user`, `trigger_id`, and route through SQS to the RAG orchestrator
- [ ] Reject unsigned or malformed requests with 401/400
- [ ] Grant Lambda read access to Slack signing secret in Secrets Manager
- [ ] Grant Lambda `sqs:SendMessage` on the query queue
- [ ] Write unit tests `tests/test_slack_ingress.py` for signature validation, event parsing, SQS message formatting

**References**: requirements.md §5.1, §17; design.md §Component 1

---

## Task 9: API Gateway for Slack Webhook

- [ ] Create `constructs/slack_api.py` construct
- [ ] Create REST API Gateway with a `POST /slack/events` endpoint
- [ ] Create `POST /slack/interactions` endpoint for Slack interactive payloads (button clicks, modals)
- [ ] Integrate both endpoints with the Slack Ingress Lambda (proxy integration)
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
- [ ] Configure ECS Service with environment-specific `desired_count` (1 for dev/staging, 2 for prod), private subnet placement
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

## Task 12: Group-to-Source-Scope Mapping Configuration Table and IaC Seeder

- [ ] Add `group_source_mapping` table to the SQL migration files (Task 3):
  - `id` (UUID PK)
  - `slack_group_handle` (string, unique — stored as bare handle without `@` prefix, matching Slack API format)
  - `authorization_group` (string)
  - `permitted_source_scopes` (JSONB — list of source scope identifiers)
  - `enabled` (boolean, default true)
  - `created_at`, `updated_at`
- [ ] Create `config/group_source_mapping.yaml` as the single source of truth for group-to-source-scope mappings:
  ```yaml
  mappings:
    - slack_group_handle: "sage-all"  # bare handle — no @ prefix (matches Slack API format)
      authorization_group: "general"
      permitted_source_scopes: ["confluence:public", "github:public", "intranet:all"]
      enabled: true
    - slack_group_handle: "sage-it"  # bare handle — no @ prefix
      authorization_group: "it-staff"
      permitted_source_scopes: ["confluence:all", "github:all", "jira:it-projects", "powerdms:all", "slack:it-channels"]
      enabled: true
    - slack_group_handle: "sage-hr-access"  # bare handle — no @ prefix
      authorization_group: "hr-content"
      permitted_source_scopes: ["powerdms:hr", "confluence:hr-space"]
      enabled: true
  ```
- [ ] Implement CDK custom resource Lambda (`lambda/seed_group_mapping/handler.py`) that:
  1. Reads `config/group_source_mapping.yaml` (bundled with the Lambda)
  2. Connects to PostgreSQL via Secrets Manager credentials
  3. Upserts rows into `group_source_mapping` for each entry in the YAML (strip any leading `@` from `slack_group_handle` before upsert to ensure canonical bare-handle format)
  4. Disables rows in the table whose `slack_group_handle` no longer appears in the YAML (soft-delete)
  5. Runs on every `cdk deploy` (triggered by content hash of the YAML file)
- [ ] Create `constructs/group_mapping_seeder.py` CDK construct wrapping the custom resource Lambda
- [ ] Write unit tests `tests/test_group_mapping_seeder.py` for: YAML parsing, upsert logic, soft-disable of removed entries, idempotency
- [ ] Write fine-grained assertions test for the migration

**References**: requirements.md §12.1.1; design.md §Component 4e, §Component 6

---

## Task 13: Identity Sync Worker — ECS Fargate Task and EventBridge Schedule

- [ ] Create `constructs/identity_sync.py` construct
- [ ] Create ECS Fargate task definition for the identity sync worker (CPU/memory: 0.25 vCPU, 0.5 GB)
- [ ] Create container image project structure at `containers/identity_sync/` (Dockerfile, `main.py`, `requirements.txt`)
- [ ] Implement sync logic:
  1. Read `group_source_mapping` config from PostgreSQL (seeded by Task 12)
  2. Call Slack `usergroups.list` to enumerate configured Slack User Groups
  3. Call `usergroups.users.list` for each group to resolve current members
  4. For each user, compute the set of authorization groups from group membership
  5. Upsert `users` records: update `identity_groups` (JSONB) and `groups_synced_at`
  6. Auto-create user records for new Slack users (populate email, display_name from Slack profile via `users.info`)
- [ ] Respect Slack API rate limits (Tier 2: ~20 req/min) with exponential backoff
- [ ] Log sync results: users updated, groups changed, errors encountered
- [ ] Create EventBridge rule to trigger the task every 15 minutes via ECS RunTask
- [ ] Grant task role permissions: Secrets Manager read (Slack bot token), RDS connect (read/write `users` and `group_source_mapping` tables), `logs:PutLogEvents`
- [ ] Write unit tests `tests/test_identity_sync.py` for sync logic, rate limit handling, upsert behavior using `pytest`

**References**: requirements.md §12.1, §12.1.1; design.md §Component 4e

---

## Task 14: RAG Orchestrator — Identity Mapping and Authorization

- [ ] Implement user lookup by `slack_user_id` in PostgreSQL `users` table using `psycopg2` or `asyncpg`
- [ ] Create user record on first interaction if not found (with Slack user info)
- [ ] Read `identity_groups` for the user (populated by the Slack User Groups sync job — see Task 13)
- [ ] Resolve permitted source scopes by joining `identity_groups` against `group_source_mapping` config table (see Task 12)
- [ ] Build authorization filter (list of permitted `visibility_scope` and `acl_tags` values) from resolved source scopes
- [ ] Pass authorization context to the retrieval step

**References**: requirements.md §12.1, §12.1.1, §12.2, §5.6; design.md §Component 3, §Component 4e

---

## Task 15: RAG Orchestrator — Rate Limiting

- [ ] Implement per-user rate limits: 5/min, 30/hr, 100/day, 1 in-flight
- [ ] Implement per-channel rate limit: 10 per 5 minutes
- [ ] Implement global concurrent RAG job limit: 25
- [ ] Use PostgreSQL or in-memory counters (with ECS task count awareness) for tracking
- [ ] Check rate limits immediately after dequeuing the SQS message; if exceeded, skip all heavy downstream processing (Bedrock, OpenSearch), send user-friendly Slack response, and delete/ack the message from the queue
- [ ] Return user-friendly Slack message when rate-limited
- [ ] Write unit tests for each rate limit tier using `pytest`

**References**: requirements.md §18; design.md §Error Scenario 3

---

## Task 16: RAG Orchestrator — Query Retrieval via LlamaIndex

- [ ] Configure LlamaIndex retriever backed by `OpensearchVectorStore` with `BedrockEmbedding` for automatic query embedding
- [ ] Configure hybrid search mode (keyword + vector) on the retriever
- [ ] Implement retry with exponential backoff (up to 3 attempts) on Bedrock/OpenSearch errors
- [ ] Write unit tests with mocked retriever using `pytest` and `unittest.mock`

**References**: requirements.md §9.2, §9.5; design.md §Component 3, §Component 7

---

## Task 17: RAG Orchestrator — Hybrid Search and Post-Processing

- [ ] Configure `OpensearchVectorStore` retriever with hybrid mode combining BM25 keyword search and k-NN vector search
- [ ] Apply authorization filters via `MetadataFilters` (`visibility_scope`, `acl_tags`) from the user's identity context
- [ ] Implement custom `NodePostprocessor` for source authority ranking boost based on `authoritative_rank`
- [ ] Implement custom `NodePostprocessor` for freshness boost based on `last_updated`
- [ ] Implement custom `NodePostprocessor` for authorization filtering
- [ ] Return top-K ranked nodes with metadata
- [ ] Write unit tests for each `NodePostprocessor` and retriever configuration using `pytest`

**References**: requirements.md §9.2, §10.2, §10.3; design.md §Component 3, §Component 5

---

## Task 18: RAG Orchestrator — Prompt Assembly

- [ ] Define LlamaIndex `PromptTemplate` with: grounding rules, citation instructions, refusal rules, confidence assessment instructions, and prompt injection defenses
- [ ] Configure the template to include the user question and top retrieved nodes with source metadata (title, source_system, source_url, last_updated)
- [ ] Enforce token budget: truncate or drop lowest-ranked nodes if prompt exceeds model context window
- [ ] Write unit tests for prompt template output and sanitization using `pytest`

**References**: requirements.md §9.3, §15.1, §15.2, §12.5; design.md §Component 3

---

## Task 19: RAG Orchestrator — LLM Answer Generation

- [ ] Configure LlamaIndex's `Bedrock` LLM class for the generation model (e.g., Claude 3 Sonnet/Haiku via Bedrock)
- [ ] Invoke the LLM via LlamaIndex with the assembled `PromptTemplate`
- [ ] Parse LLM response: extract answer text, cited source references, uncertainty notes
- [ ] Assign confidence level (High/Medium/Low) based on: relevance scores, source agreement, source authority, answer completeness
- [ ] Implement retry with exponential backoff on Bedrock errors
- [ ] Write unit tests with mocked LLM class using `pytest`

**References**: requirements.md §9.4, §15.3; design.md §Component 7, §Error Scenario 5

---

## Task 20: RAG Orchestrator — Slack Response Formatting and Posting

- [ ] Format answer as Slack Block Kit message: answer block, confidence block, numbered sources block (title, system, URL), notes block
- [ ] Add interactive buttons: "Helpful", "Not helpful", "View sources", "Report issue"
- [ ] Post message to the correct channel/DM and thread using Slack `chat.postMessage` API (via `slack_sdk`)
- [ ] Handle Slack API errors gracefully (retry once, log failure)
- [ ] Write unit tests for message formatting using `pytest`

**References**: requirements.md §14.1, §14.2, §14.3; design.md §Component 3

---

## Task 21: RAG Orchestrator — Audit Logging

- [ ] After each query, insert a record into `queries` table: user_id, question, answer, confidence, latency_ms
- [ ] Insert records into `query_sources` for each retrieved chunk used: query_id, document_id, chunk_id, rank
- [ ] Ensure logging does not block the response path (fire-and-forget or async)
- [ ] Write unit tests for audit log insertion using `pytest`

**References**: requirements.md §5.5; design.md §Component 6

---

## Task 22: RAG Orchestrator — Feedback Handler

- [ ] Handle Slack interactive payload (button clicks) routed through the SQS consumer
- [ ] Parse feedback type: helpful, not_helpful, report_issue
- [ ] Parse optional user comment (from modal if "Report issue")
- [ ] Insert record into `feedback` table: query_id, user_id, feedback_type, comment
- [ ] Acknowledge feedback to user in Slack (ephemeral message)
- [ ] Write unit tests for feedback parsing and storage using `pytest`

**References**: requirements.md §5.4, §14.3; design.md §Feedback Flow

---

## Task 23: Ingestion Pipeline — EventBridge Schedules

- [ ] Create `constructs/ingestion_scheduler.py` construct
- [ ] Create EventBridge rules for each source with freshness-aligned schedules:
  - Confluence: every 6 hours
  - Slack (public channels): every 1 hour
  - Jira (all projects): every 1 hour
  - GitHub: every 1 hour
  - Intranet: every 12 hours
  - PowerDMS: every 12 hours
- [ ] Each rule targets the corresponding connector ECS Fargate task directly via ECS RunTask target
- [ ] Write fine-grained assertions test `tests/test_ingestion_scheduler.py`

**References**: requirements.md §6.4; design.md §Component 4a

---

## Task 24: Ingestion Pipeline — Connector Self-Management

- [ ] Implement self-management logic in the base connector framework (`containers/connectors/base_connector/`)
- [ ] At startup: create `ingestion_runs` record (status: running), determine run type (full vs. incremental based on `last_cursor`)
- [ ] On success: update `ingestion_runs` status to `succeeded`, update `connector_status` (last_success_at, reset consecutive_failures, update lag/counts)
- [ ] On failure: update `ingestion_runs` status to `failed`, increment `consecutive_failures` on `connector_status`, log error details
- [ ] Support run types: full, incremental (based on `last_cursor`)
- [ ] Grant ECS connector worker task role permissions to write to RDS
- [ ] Write unit tests for self-management lifecycle (start, success, failure) using `pytest`

**References**: requirements.md §13.0, §13.1 (connector_status, ingestion_runs); design.md §Component 4b, §Ingestion Flow

---

## Task 25: Connector — Base Connector Framework (LlamaIndex IngestionPipeline)

- [ ] Create shared connector base in `containers/connectors/base_connector/`
- [ ] Configure LlamaIndex `IngestionPipeline` with:
  - Node parser for chunking (semantic/structure-first per requirements §9.6): `MarkdownNodeParser` for Markdown/Confluence/GitHub, `SentenceSplitter` for general text, optionally `SemanticSplitterNodeParser` for unstructured content
  - `BedrockEmbedding` (Amazon Titan Text Embeddings V2, 1024 dimensions) for embedding generation
  - `OpensearchVectorStore` for indexing
  - Content-hash deduplication to skip unchanged documents
- [ ] Configure node parser settings: target ~450–650 tokens, hard max ~900 tokens, ~75 token overlap for narrative splits
- [ ] Implement S3 snapshot storage (custom step, outside LlamaIndex pipeline) using `boto3`
- [ ] Implement PostgreSQL document/chunk metadata upsert (custom step) using `psycopg2` or `SQLAlchemy`
- [ ] Define abstract base class with methods: `fetch()` (returns LlamaIndex `Document` objects), `store_snapshot()`, `update_metadata()`
- [ ] Write unit tests for `IngestionPipeline` configuration, node parser settings, and change detection using `pytest`

**References**: requirements.md §9.0, §9.1, §9.6; design.md §Component 4c

---

## Task 26: Connector — Confluence

- [ ] Create `containers/connectors/confluence_connector/` with handler
- [ ] Configure `ConfluenceReader` from LlamaHub with Confluence credentials from Secrets Manager
- [ ] Fetch pages across all spaces using the reader; reader returns `Document` objects with body, title, URL, and metadata
- [ ] Implement incremental sync by filtering on `lastModified` (custom logic wrapping the reader)
- [ ] Extract permission metadata where available and attach to document metadata
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Store normalized snapshots in S3 (custom step)
- [ ] Create ECS Fargate task definition for this connector (on-demand, cold start OK)
- [ ] Write unit tests for reader configuration, incremental sync logic, and metadata extraction using `pytest`

**References**: requirements.md §11.1; design.md §Component 4c

---

## Task 27: Connector — Slack

- [ ] Create `containers/connectors/slack_connector/` with handler
- [ ] Configure `SlackReader` from LlamaHub with Slack bot token from Secrets Manager
- [ ] Enumerate public channels using `conversations.list` (exclude archived channels) — custom logic wrapping the reader
- [ ] Use `SlackReader` to fetch message history per channel; supplement with custom logic for threaded replies (`conversations.replies`)
- [ ] Filter out bot messages, ephemeral messages, and system messages (custom post-processing)
- [ ] Group threaded conversations into single `Document` objects (parent message + replies) — custom logic
- [ ] Implement incremental sync using `oldest` timestamp parameter (track `last_cursor` per channel) — custom logic
- [ ] Respect Slack API rate limits (Tier 3: ~50 req/min) with backoff
- [ ] Normalize message content: resolve user mentions, channel references, emoji shortcodes
- [ ] Set `authoritative_rank` to 6 (below intranet, above "other supporting content")
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Support channel-scoped ingestion and status tracking (one `connector_status` row per channel)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for reader configuration, thread grouping, incremental sync, message normalization using `pytest`

**References**: requirements.md §11.5; design.md §Component 4c

---

## Task 28: Connector — Jira

- [ ] Create `containers/connectors/jira_connector/` with handler
- [ ] Configure `JiraReader` from LlamaHub with Jira credentials from Secrets Manager
- [ ] Use `JiraReader` to fetch issues with summary, description, and metadata
- [ ] Implement incremental sync using JQL `updated >= "last_cursor"` filter — custom logic wrapping the reader
- [ ] Implement comment aggregation: concatenate issue description + comments into a single `Document` per issue — custom logic
- [ ] Normalize Jira wiki markup / ADF to markdown/text (supplement reader output if needed)
- [ ] Extract permission metadata where available (project-level visibility)
- [ ] Set `authoritative_rank` to 3 (below Confluence, above GitHub)
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Support project-scoped ingestion and status tracking (one `connector_status` row per project)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for reader configuration, incremental sync, comment aggregation using `pytest`

**References**: requirements.md §11.6; design.md §Component 4c

---

## Task 29: Connector — GitHub

- [ ] Create `containers/connectors/github_connector/` with handler
- [ ] Configure `GithubRepositoryReader` from LlamaHub with GitHub App credentials from Secrets Manager
- [ ] Enumerate repos under `Sage-Bionetworks` and `Sage-Bionetworks-IT` orgs — custom logic
- [ ] Use `GithubRepositoryReader` per repo to fetch README, Markdown files in `docs/` directories, and repository metadata
- [ ] Implement revision tracking (compare commit SHA or file hash) — custom logic
- [ ] Support repository-scoped ingestion and status tracking (one `connector_status` row per repo)
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for reader configuration, repo enumeration, revision detection using `pytest`

**References**: requirements.md §11.2; design.md §Component 4c

---

## Task 30: Connector — Intranet

- [ ] Create `containers/connectors/intranet_connector/` with handler
- [ ] Configure `BeautifulSoupWebReader` or `SimpleWebPageReader` from LlamaHub for HTML content fetching
- [ ] Implement custom post-processing to remove boilerplate (nav, footer, sidebar) from reader output
- [ ] Extract canonical URL, content ownership metadata and attach to document metadata
- [ ] Implement update detection (content hash or last-modified header) — custom logic
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for reader configuration, boilerplate removal, update detection using `pytest`

**References**: requirements.md §11.3; design.md §Component 4c
---

## Task 31: Connector — PowerDMS

- [ ] Create `containers/connectors/powerdms_connector/` with handler
- [ ] Implement custom `BaseReader` (LlamaIndex interface) for PowerDMS since no LlamaHub reader exists
- [ ] Authenticate via Secrets Manager credentials; fetch title, version, approval date, document type, canonical link, document content
- [ ] Return `Document` objects with text and metadata from the custom reader
- [ ] Implement update detection (version comparison or content hash) — custom logic
- [ ] Set `authoritative_rank` to highest tier (PowerDMS SOPs are rank 1)
- [ ] Feed documents into the shared `IngestionPipeline` (base connector framework)
- [ ] Create ECS Fargate task definition for this connector
- [ ] Write unit tests for custom reader, PowerDMS API parsing, version detection using `pytest`

**References**: requirements.md §11.4, §10.3; design.md §Component 4c

---

## Task 32: Observability — CloudWatch Metrics, Logs, and Alarms

- [ ] Create `constructs/observability.py` construct
- [ ] Configure CloudWatch log groups for: Lambda, ECS tasks (RAG orchestrator + connectors)
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
- [ ] Create CloudWatch dashboard with widgets for: request volume, median/p95 latency, no-answer rate, connector health, ingestion freshness lag, SQS queue depth, Bedrock invocation metrics, OpenSearch cluster health
- [ ] Enable CloudTrail for AWS API audit logging
- [ ] Write fine-grained assertions test `tests/test_observability.py`

**References**: requirements.md §6.5, §19.1, §19.2; design.md §Security Considerations

---

## Task 33: IAM Roles and Least-Privilege Policies

- [ ] Review and tighten all IAM roles created in previous tasks
- [ ] Lambda ingress role: only `sqs:SendMessage`, `secretsmanager:GetSecretValue` (scoped to Slack secret)
- [ ] ECS RAG orchestrator role: `sqs:ReceiveMessage/DeleteMessage`, `secretsmanager:GetSecretValue`, `bedrock:InvokeModel`, OpenSearch data access, RDS connect, `logs:PutLogEvents`
- [ ] ECS connector worker roles: source-specific secrets, `bedrock:InvokeModel`, OpenSearch data write, `s3:PutObject`, RDS connect, `logs:PutLogEvents`
- [ ] EventBridge role: `ecs:RunTask` (scoped to connector task definitions)
- [ ] ECS identity sync worker role: `secretsmanager:GetSecretValue` (scoped to Slack bot token), RDS connect (read/write `users` and `group_source_mapping`), `logs:PutLogEvents`
- [ ] EventBridge identity sync role: `ecs:RunTask` (scoped to identity sync task definition)
- [ ] Use `cdk-iam-floyd` for policy generation where applicable
- [ ] Validate no `*` resource wildcards in production policies
- [ ] Write fine-grained assertions test `tests/test_iam_policies.py`

**References**: requirements.md §6.1, §12.2; design.md §Security Considerations

---

## Task 34: End-to-End Integration Test

- [ ] Create integration test that exercises the full query flow: Slack event → API Gateway → Lambda → SQS → ECS RAG orchestrator → OpenSearch + Bedrock → Slack response
- [ ] Pre-index a small set of known test documents into OpenSearch
- [ ] Send a test query and verify: answer is grounded, citations are present, confidence is assigned, audit log is written
- [ ] Verify feedback flow: send feedback interaction, confirm it's stored in PostgreSQL
- [ ] Verify rate limiting: send requests exceeding per-user limit, confirm rejection message
- [ ] Verify connector disable: disable a connector, confirm ingestion stops and queries still work for other sources

**References**: requirements.md §21 (MVP Acceptance Criteria); design.md §Testing Strategy

---

## Task 35: CDK Stack Composition and Deployment Configuration

- [ ] Create `stacks/sage_kb_chatbot_stack.py` that composes all constructs
- [ ] Wire cross-construct references (VPC, security groups, secrets ARNs, queue URLs, DB endpoints, OpenSearch domain)
- [ ] Create environment-specific configurations (dev, staging, prod) with appropriate sizing
- [ ] Configure environment-specific values via constructor props and environment variables (do not use CDK context)
- [ ] Verify `cdk synth` produces valid templates for each environment
- [ ] Write snapshot test `tests/test_sage_kb_chatbot_stack.py`

**References**: design.md §Dependencies
