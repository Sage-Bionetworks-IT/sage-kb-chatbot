# Sage Internal Knowledge Slack Chatbot — Specification

## Document Control
- **Project**: Sage Internal Knowledge Slack Chatbot
- **Target Environment**: AWS
- **Primary Interface**: Slack
- **Architecture Pattern**: Retrieval-Augmented Generation (RAG)
- **RAG Framework**: LlamaIndex
- **Search Engine**: Amazon OpenSearch Service
- **LLM Platform**: Amazon Bedrock
- **Development Language**: Python
- **IaC Framework**: AWS CDK (Python)
- **Version**: 1.0
- **Status**: Draft

---

## 1. Overview

### 1.1 Purpose
Build an internal Slack chatbot that accepts employee questions and returns answers derived from Sage Bionetworks internal knowledge sources, including:

- Confluence (all spaces in scope)
- Slack (public channels and threads)
- Jira (all projects in scope)
- intranet
- GitHub (all repositories under `Sage-Bionetworks` and `Sage-Bionetworks-IT` are in scope)
- PowerDMS
- Synapse (Phase 2)
- Leapsome (deferred / later phase)

The chatbot must provide:

- grounded answers based on approved internal content
- source citations and canonical links
- permission-aware retrieval
- auditable interactions
- phased rollout with governance controls

### 1.2 Problem Statement
Sage knowledge is distributed across multiple internal systems. Employees currently spend too much time locating policies, SOPs, runbooks, project documentation, and data access guidance. A Slack-native assistant will reduce search friction while preserving security and source-of-truth behavior.

### 1.3 Success Criteria
The solution is successful if it:

- returns useful, cited answers in Slack
- improves discoverability of approved internal documentation
- does not expose unauthorized content
- clearly indicates source provenance and answer confidence
- supports operational monitoring and content governance

---

## 2. Goals and Non-Goals

### 2.1 Goals
- Allow employees to ask questions in Slack and receive grounded answers.
- Use internal documentation and metadata as the source of truth.
- Support source citations and links in every substantive response.
- Enforce source-level and, where feasible, document-level access control.
- Log and audit queries, retrieved evidence, and feedback.
- Support incremental source onboarding.

### 2.2 Non-Goals (v1)
- No write-back actions to source systems.
- No autonomous workflows or task execution.
- No unrestricted indexing of HR/performance systems.
- No uncited freeform answers for policy/process questions.
- No exposure of raw sensitive datasets through Slack.

---

## 3. Users and Roles

### 3.1 End Users
- Sage employees
- approved contractors, if authorized

### 3.2 Administrative Roles
- platform owner
- security/privacy reviewer
- governance/content owner
- DevOps/operations owner

---

## 4. Scope

### 4.1 In Scope for MVP
- Slack bot interface
- RAG pipeline
- Confluence connector (all spaces in scope)
- Slack connector (public channels and threads)
- Jira connector (all projects in scope)
- GitHub connector for all repositories under `Sage-Bionetworks` and `Sage-Bionetworks-IT`
- intranet connector
- PowerDMS connector
- permission-aware retrieval at source/category level
- citations and confidence in responses
- logging, audit, feedback

### 4.2 Deferred
- Synapse raw sensitive content
- Leapsome
- document editing or ticket creation
- agentic execution
- advanced per-paragraph ACL sync for all systems

---

## 5. Functional Requirements

### 5.1 Slack Interaction
The chatbot shall support:
- app mentions in channels
- direct messages
- slash command `/sage-ask`

Examples:
- `@sage-bot where is the SOP for requesting Synapse data access?`
- `/sage-ask what is the current external data sharing policy?`

### 5.2 Answer Generation
The chatbot shall:
- retrieve relevant internal content
- generate answers grounded only in retrieved content
- cite sources used to support the answer
- identify uncertainty or conflicting documents
- refuse to answer when evidence is insufficient

### 5.3 Source Citations
Each answer shall include:
- source title
- source system
- canonical URL
- optionally last updated date

### 5.4 Feedback
The chatbot shall support:
- helpful / not helpful feedback
- report issue / escalate
- optional user comment

### 5.5 Audit Logging
The system shall log:
- user identity
- timestamp
- question
- retrieved sources
- answer
- confidence
- latency
- feedback

### 5.6 Access Enforcement
The system shall:
- retrieve only from approved sources
- filter responses based on user authorization
- support phased rollout where only broadly shareable content is indexed first

---

## 6. Non-Functional Requirements

### 6.1 Security
- Encrypt data at rest and in transit.
- Store secrets in AWS Secrets Manager.
- Enforce IAM least privilege.
- Maintain auditable logs.
- Support quick connector disablement.

### 6.2 Reliability
- Target availability: 99.5% monthly for the chatbot API.
- Graceful degradation if a connector is unavailable.
- Partial operation acceptable when one source fails.

### 6.3 Performance
- Target median response time: < 8 seconds
- Target p95 response time: < 15 seconds

### 6.4 Freshness Targets
- Confluence (all spaces): within 6 hours
- Slack (public channels): within 1 hour
- Jira (all projects): within 1 hour
- GitHub repos under `Sage-Bionetworks` and `Sage-Bionetworks-IT`: within 1 hour
- intranet: within 12 hours
- PowerDMS: within 12 hours
- Synapse docs/metadata: within 12 hours when added

### 6.5 Observability
- application logs
- request tracing
- latency metrics
- ingestion lag metrics
- retrieval quality signals
- low-confidence rate

---

## 7. Architecture

### 7.1 High-Level Architecture
```text
Slack
  ↓
API Gateway
  ↓
AWS Lambda (thin Slack event receiver / signature validator / immediate acknowledgment)
  ↓
Asynchronous handoff using Amazon SQS
  ↓
Amazon ECS on Fargate Chatbot Backend (always-on, minimum 1–2 warm tasks)
  ├─ identity mapping
  ├─ authorization checks
  ├─ retrieval orchestration
  ├─ prompt assembly
  ├─ Amazon Bedrock invocation for generation
  └─ audit logging
        ↓
Data Plane
  ├─ Amazon OpenSearch Service
  ├─ Amazon RDS for PostgreSQL
  ├─ Amazon S3
  └─ Amazon Bedrock

Ingestion Plane
  ├─ EventBridge
  └─ Amazon ECS on Fargate connector/ingestion tasks (cold start on demand)
```

### 7.2 Architectural Principles
- Retrieval first, generation second
- Citations required
- Source authority ranking
- Permission-aware retrieval
- Managed AWS services where practical
- Connector isolation
- Thin AWS Lambda for Slack ingress only
- Heavy RAG orchestration on Amazon ECS on Fargate
- Async handoff between Slack ingress and answer generation
- Keep at least 1–2 warm Amazon ECS on Fargate tasks for the main chatbot service
- Use LlamaIndex as the RAG framework for ingestion, chunking, embedding, indexing, and retrieval where practical; custom code only where LlamaIndex abstractions are insufficient (authorization filtering, source authority ranking, audit logging, confidence scoring)

---

## 8. AWS Services

### 8.1 Core Services
- Amazon API Gateway
- AWS Lambda (Slack event receiver / fast acknowledgment)
- Amazon ECS on Fargate (RAG-heavy orchestration and main chatbot service)
- Amazon OpenSearch Service
- Amazon RDS for PostgreSQL
- Amazon S3
- Amazon Bedrock
- Amazon SQS (async handoff between AWS Lambda and Amazon ECS on Fargate)
- Amazon EventBridge
- AWS Secrets Manager
- AWS KMS
- Amazon CloudWatch
- AWS CloudTrail

### 8.2 Optional Later Services
- AWS Step Functions (complex multi-step orchestration if needed in future)
- Amazon ElastiCache for Redis (query/embedding caching)
- AWS WAF
- Amazon Macie
- AWS X-Ray
- AWS Security Hub
- Amazon GuardDuty
---

## 9. RAG Design

### 9.0 RAG Framework
The system shall use **LlamaIndex** as the RAG framework for ingestion, retrieval, and query orchestration.

LlamaIndex provides:
- Pre-built data connectors (Readers) for Confluence, Slack, Jira, GitHub, and web crawling
- Configurable chunking via node parsers (`MarkdownNodeParser`, `SentenceSplitter`, `SemanticSplitterNodeParser`)
- Embedding integration via `BedrockEmbedding`
- Vector store integration via `OpensearchVectorStore`
- Retrieval with `VectorIndexRetriever` and hybrid search support
- LLM integration via `Bedrock` LLM class
- `IngestionPipeline` with built-in document deduplication via content hash
- Extension points: custom `BaseReader` for unsupported sources, `NodePostprocessor` for custom ranking/filtering, `PromptTemplate` for prompt customization

Custom code is required for:
- Authorization filtering (custom `NodePostprocessor`)
- Source authority ranking (custom `NodePostprocessor`)
- Confidence scoring (post-generation logic)
- Audit logging to PostgreSQL
- S3 snapshot storage
- Slack bot interaction layer
- Rate limiting
- Identity mapping

For sources without a LlamaHub reader (PowerDMS), a custom reader shall be built on LlamaIndex's `BaseReader` interface.

### 9.1 Ingestion
For each source, the pipeline shall use LlamaIndex's `IngestionPipeline`:
1. fetch content using a LlamaIndex Reader (or custom `BaseReader` for PowerDMS)
2. Reader returns normalized `Document` objects with text and metadata
3. apply node parser for chunking (semantic/structure-first strategy)
4. compute embeddings using `BedrockEmbedding` (Amazon Titan Text Embeddings V2)
5. index nodes into Amazon OpenSearch Service via `OpensearchVectorStore`
6. use `IngestionPipeline` deduplication (content hash) to skip unchanged documents
7. store metadata in PostgreSQL (document and chunk records)
8. store normalized snapshots in S3 (custom step, outside LlamaIndex pipeline)

### 9.2 Retrieval
For each user query, the system shall:
1. normalize the query
2. use LlamaIndex's retriever to generate query embeddings and execute hybrid search against `OpensearchVectorStore`
3. apply metadata filters for authorization (`visibility_scope`, `acl_tags`) via `MetadataFilters`
4. apply custom `NodePostprocessor` for source authority ranking boost
5. apply custom `NodePostprocessor` for freshness boost
6. apply custom `NodePostprocessor` for authorization filtering
7. return top-K ranked nodes with metadata

### 9.3 Augmentation
The system shall use LlamaIndex's `PromptTemplate` to build a prompt containing:
- the user question
- top retrieved chunks (nodes) with source metadata
- grounding rules, citation instructions, and refusal rules
- prompt injection defenses (sanitize node content before prompt assembly)

### 9.4 Generation
The system shall use **Anthropic Claude Sonnet** via LlamaIndex's `Bedrock` LLM class for answer generation. The LLM shall:
- answer only from provided context
- cite evidence
- note uncertainty
- refuse unsupported claims

### 9.5 Embedding Model
The system shall use **Amazon Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`, 1024 dimensions, 8K token input) via LlamaIndex's `BedrockEmbedding` for:
- document chunk embeddings (during ingestion)
- query embeddings (during retrieval)

### 9.5.1 Amazon OpenSearch Service Vector Dimensions
The OpenSearch vector dimension shall be **1024**, matching the output dimension of Amazon Titan Text Embeddings V2.
The Amazon OpenSearch Service index vector dimension must exactly match the embedding output dimension (1024).

### 9.6 Chunking Strategy
The system shall use LlamaIndex node parsers configured for **semantic/document structure first**, then size limits.

Preferred node parsers by content type:
- Markdown/Confluence/GitHub: `MarkdownNodeParser` (splits on headings and structure)
- General text: `SentenceSplitter` with structure-aware settings
- Optionally: `SemanticSplitterNodeParser` for content without clear structural markers

Chunking rules:
- split first on natural document boundaries such as title, heading, subsection, FAQ item, procedure block, bullet group, or code/explanation block
- if a structural section is too large, split further by paragraph groups or sentence-safe boundaries
- preserve heading path and parent metadata on every chunk (via LlamaIndex node metadata inheritance)
- keep semantically related explanatory text and short code/command examples together when applicable

Default targets (configured on the node parser):
- target chunk size: approximately 450–650 tokens
- hard maximum: approximately 900 tokens
- overlap: approximately 75 tokens only when splitting long continuous narrative sections

The retrieval layer should prefer structure-aware chunking over fixed-size character chunking.

---

## 10. Search and Ranking

### 10.1 Amazon OpenSearch Service Index Requirements
Each indexed chunk shall include:
- `document_id`
- `chunk_id`
- `source_system`
- `source_url`
- `title`
- `content`
- `last_updated`
- `authoritative_rank`
- `visibility_scope`
- `acl_tags`
- `content_type`
- `owner`
- `embedding`

### 10.2 Ranking Signals
Ranking shall consider:
- lexical match score
- semantic similarity score
- document freshness
- source authority
- exact title/header match
- content type
- user authorization

### 10.3 Source Authority Order
Default weighting:
1. PowerDMS approved SOP/policy
2. official Confluence spaces
3. Jira issues and project documentation
4. GitHub docs/runbooks
5. approved intranet pages
6. Slack messages (public channels and threads)
7. other approved supporting content

Governance owners may override this ordering.

---

## 11. Source Connectors

A connector is a service responsible for ingesting content from a source system (e.g., Confluence, GitHub) into the RAG index. Each connector uses a LlamaIndex Reader (from LlamaHub where available, or a custom `BaseReader`) to fetch and normalize content into `Document` objects, which are then processed by the shared `IngestionPipeline`.


### 11.1 Confluence
All Confluence spaces are in scope. Uses `ConfluenceReader` from LlamaHub.

Must support:
- page body extraction
- title extraction
- canonical URL
- last modified timestamp
- space metadata
- update detection
- permission metadata where available

### 11.2 GitHub
All repositories under the `Sage-Bionetworks` and `Sage-Bionetworks-IT` GitHub organizations are in scope. Uses `GithubRepositoryReader` from LlamaHub.

Must support:
- Markdown docs
- README files
- docs directories
- repository metadata
- revision tracking
- repository-scoped ingestion and status tracking

### 11.3 Intranet
Uses `BeautifulSoupWebReader` or `SimpleWebPageReader` from LlamaHub.

Must support:
- HTML crawl or API fetch
- boilerplate removal
- canonical URL extraction
- content ownership metadata
- update detection

### 11.4 PowerDMS
No LlamaHub reader available. Uses a custom reader built on LlamaIndex's `BaseReader` interface.

Must support:
- title
- version
- approval date
- document type
- canonical link
- update detection

### 11.5 Slack
Public Slack channels and threads are in scope. Private channels and DMs are excluded. Uses `SlackReader` from LlamaHub for basic message fetching; custom logic wraps the reader for thread grouping, incremental sync, and filtering.

Must support:
- message text extraction (including threaded replies)
- channel name and topic metadata
- author (Slack user ID)
- message permalink (canonical URL)
- timestamp for freshness and update detection
- incremental sync using Slack Conversations API cursor-based pagination
- filtering out bot messages and ephemeral content
- respecting Slack API rate limits

### 11.6 Jira
All Jira projects are in scope. Uses `JiraReader` from LlamaHub for basic issue fetching; custom logic wraps the reader for JQL-based incremental sync and comment aggregation.

Must support:
- issue summary, description, and comments extraction
- project key and project name metadata
- issue type, status, priority, and labels
- assignee and reporter metadata
- canonical issue URL (permalink)
- last updated timestamp for freshness and update detection
- incremental sync using JQL `updated` filter
- attachment text extraction where feasible (PDF, DOCX)
- respecting Jira API rate limits

### 11.7 Synapse (Phase 2)
Must support:
- documentation
- approved metadata
- project/dataset descriptions
- source link
- access metadata

### 11.8 Leapsome (Deferred)
Requires explicit security/governance approval before indexing.

---

## 12. Security and Compliance

### 12.1 Identity Mapping
The application shall use Slack as the identity anchor. Users authenticate to Slack via multiple federated identity providers; the system treats the Slack user ID as the common identity key regardless of upstream IdP.

The application shall map:
- Slack user ID (primary identity key)
- internal email/identity (from Slack profile)
- authorization groups (derived from Slack User Group membership)
- permitted source scopes (derived from authorization groups)

#### 12.1.1 Authorization Group Sync via Slack User Groups
Slack User Groups (e.g., `sage-all`, `sage-engineering`, `sage-hr-access`) shall serve as the source of truth for authorization group membership.

The system shall:
- Maintain a mapping from Slack User Groups to chatbot authorization groups (e.g., Slack group `sage-hr-access` → authorization group `hr-content`)
- Run a scheduled sync job (EventBridge → ECS Fargate task) every 15 minutes that:
  1. Enumerates configured Slack User Groups via `usergroups.list` and `usergroups.users.list` APIs
  2. For each user, resolves their current group memberships
  3. Updates the `identity_groups` JSONB field on the `users` table in PostgreSQL
- Auto-create user records on first sync if not already present (with Slack profile info: email, display name)
- Log sync results (users updated, groups changed) for auditability
- Respect Slack API rate limits (Tier 2: ~20 req/min for usergroups endpoints) with backoff

Admins manage access by adding/removing users from Slack User Groups — no separate admin interface is required for MVP.

The group-to-source-scope mapping shall be defined in a version-controlled YAML configuration file (`config/group_source_mapping.yaml`) and loaded into the `group_source_mapping` PostgreSQL table during deployment via a CDK custom resource (Lambda). The YAML file is the single source of truth — rows not present in the file are disabled on deploy. Changes go through the normal PR review process, giving governance owners visibility before any access changes land.

### 12.2 Access Controls
The application shall enforce:
- source-level authorization
- document/category filtering where supported
- restricted-source onboarding by approval only

### 12.3 Secret Management
All credentials shall be stored in AWS Secrets Manager, including:
- Slack tokens
- Confluence credentials
- GitHub app credentials
- Jira credentials
- PowerDMS credentials
- Synapse credentials
- Leapsome credentials if later enabled

### 12.4 Encryption
- S3 SSE-KMS
- Amazon RDS for PostgreSQL encryption enabled
- Amazon OpenSearch Service encryption enabled
- TLS for all service communication

### 12.5 Prompt Injection Defenses
The system shall:
- treat retrieved content as data, not instructions
- prevent source text from altering system rules
- sanitize hidden markup or malicious prompt artifacts
- disable agentic execution in v1

### 12.6 Sensitive Data Policy
The bot shall not expose:
- raw sensitive datasets
- restricted HR/performance content
- secret values
- credentials or tokens
- content outside the user's permitted scope

---

## 13. Data Model

### 13.0 Ingestion Operations Strategy
The ingestion subsystem shall separate:
- **`connector_status`**: current operational state of each connector in each environment/source scope
- **`ingestion_runs`**: append-only run log for every ingestion execution

`connector_status` is updated in place and provides the latest health/checkpoint view.
`ingestion_runs` is insert-only and provides operational history and auditability.


### 13.1 PostgreSQL Tables

#### `users`
- `id`
- `slack_user_id`
- `email`
- `display_name`
- `identity_groups` (JSONB — list of authorization group names derived from Slack User Group membership, synced every 15 minutes)
- `groups_synced_at` (timestamp — last time identity_groups was refreshed from Slack)
- `created_at`
- `updated_at`

#### `documents`
- `id`
- `source_system`
- `source_document_id`
- `title`
- `source_url`
- `owner`
- `content_type`
- `last_updated`
- `authoritative_rank`
- `visibility_scope`
- `acl_tags`
- `hash`
- `version`
- `created_at`
- `updated_at`

#### `chunks`
- `id`
- `document_id`
- `chunk_index`
- `chunk_hash`
- `opensearch_id`
- `created_at`

#### `connector_status`
Current operational state of each connector in each environment/source scope.

Recommended fields:
- `id`
- `connector_key`
- `connector_scope`
- `scope_identifier` represents the specific unit of ingestion (e.g., Confluence space key, GitHub repository name)
- `environment`
- `enabled`
- `status` (`healthy`, `running`, `degraded`, `failed`, `disabled`, `paused`)
- `last_run_id`
- `last_run_started_at`
- `last_run_finished_at`
- `last_success_at`
- `last_failure_at`
- `last_failure_code`
- `last_failure_message`
- `last_cursor`
- `last_full_sync_at`
- `last_incremental_sync_at`
- `freshness_target_minutes`
- `lag_minutes`
- `docs_seen_total`
- `docs_indexed_total`
- `docs_failed_total`
- `consecutive_failures`
- `version`
- `metadata`
- `created_at`
- `updated_at`

Uniqueness should be enforced across:
- `connector_key`
- `connector_scope`
- `scope_identifier` represents the specific unit of ingestion (e.g., Confluence space key, GitHub repository name)
- `environment`

#### `ingestion_runs`
Append-only run log for every ingestion execution.

Recommended fields:
- `id`
- `connector_status_id`
- `connector_key`
- `connector_scope`
- `scope_identifier` represents the specific unit of ingestion (e.g., Confluence space key, GitHub repository name)
- `environment`
- `run_type` (`full`, `incremental`, `backfill`, `retry`, `manual`)
- `trigger_type` (`schedule`, `webhook`, `manual`, `retry`, `deploy`)
- `trigger_actor`
- `requested_at`
- `started_at`
- `finished_at`
- `status` (`queued`, `running`, `succeeded`, `partial_success`, `failed`, `cancelled`, `timed_out`)
- `cursor_in`
- `cursor_out`
- `docs_discovered`
- `docs_fetched`
- `docs_processed`
- `docs_indexed`
- `docs_skipped`
- `docs_deleted`
- `docs_failed`
- `chunks_created`
- `chunks_updated`
- `chunks_deleted`
- `bytes_processed`
- `error_code`
- `error_message`
- `error_details`
- `warnings`
- `metrics`
- `metadata`
- `created_at`

#### `group_source_mapping`
Configuration table mapping Slack User Groups to chatbot authorization groups and permitted source scopes.

- `id`
- `slack_group_handle` (unique — e.g., `sage-hr-access`; stored without `@` prefix — the Slack API returns bare handles, so the system stores and matches on the bare form; any leading `@` must be stripped before lookup/upsert)
- `authorization_group` (e.g., `hr-content`)
- `permitted_source_scopes` (JSONB — list of source scope identifiers using `system:scope` format, e.g., `["powerdms:hr", "confluence:hr-space"]`)
- `enabled`
- `created_at`
- `updated_at`

#### `queries`
- `id`
- `user_id`
- `question`
- `answer`
- `confidence`
- `latency_ms`
- `created_at`

#### `query_sources`
- `query_id`
- `document_id`
- `chunk_id`
- `rank`

#### `feedback`
- `id`
- `query_id`
- `user_id`
- `feedback_type`
- `comment`
- `created_at`

---

## 14. Slack UX

### 14.1 Response Format
Each response should include:
- answer
- confidence
- numbered sources
- notes when relevant

### 14.2 Example Response
```text
Answer
The current process for requesting access to restricted Synapse datasets is documented in the Data Governance SOP and the Synapse Access Request guide. Requests must be submitted through the approved internal process described below.

Confidence
High

Sources
1. PowerDMS — Data Governance SOP v4
2. Confluence — Synapse Access Request Process
3. GitHub — synapse-admin/docs/access-request.md

Notes
An older Confluence page has outdated wording. The PowerDMS SOP appears to be the newest authoritative source.
```

### 14.3 Feedback Controls
Slack message actions:
- Helpful
- Not helpful
- View sources
- Report issue

---

## 15. LLM Behavior

### 15.1 Grounding Rules
The model must:
- answer only from provided context
- not invent missing facts
- prefer the newest authoritative sources
- explicitly state uncertainty
- include source-based reasoning where useful

### 15.2 Refusal Rules
The model must refuse or defer when:
- evidence is insufficient
- sources conflict without a clear authority winner
- the request would disclose unauthorized content
- the user asks for sensitive internal data outside policy

### 15.3 Confidence Levels
The backend shall assign a confidence label:
- High
- Medium
- Low

Confidence should be based on:
- relevance score quality
- source agreement
- source authority
- answer completeness

---

## 16. Administration

### 16.1 Admin Capabilities
Admins shall be able to:
- enable/disable connectors
- view ingestion status
- review failed ingestions
- review low-confidence answers
- review top unanswered questions
- review user feedback

#### 16.2 Governance Controls
Governance owners shall define:
- approved source list
- source authority rankings
- excluded content classes
- retention requirements (audit logs rotated every 90 days)
- escalation procedures

---

## 17. Runtime and Cold Start Strategy
The system shall use a thin, fast AWS Lambda to acknowledge Slack immediately, hand work off asynchronously, and run the main chatbot backend as an always-on Amazon ECS on Fargate service with at least 1–2 warm tasks, while allowing ingestion workers to cold start on demand.

Implementation requirements:
- AWS Lambda is the Slack event receiver
- AWS Lambda must validate Slack requests and return acknowledgment quickly
- AWS Lambda must not perform heavy retrieval or generation work inline
- Heavy RAG orchestration must run on Amazon ECS on Fargate
- The main Amazon ECS on Fargate chatbot service must maintain at least 1–2 warm tasks in production
- Ingestion workers may be run on-demand and are allowed to cold start
- Async handoff is implemented using Amazon SQS

---

## 18. Rate Limiting Policy

The system shall enforce the following limits:

Per user:
- 5 requests per minute
- 30 requests per hour
- 100 requests per day
- 1 in-flight request at a time

Per channel:
- 10 requests per 5 minutes

Global:
- 25 concurrent RAG jobs

The system should:
- check rate limits in the RAG orchestrator immediately after dequeuing; rate-limited messages must not trigger heavy downstream processing (Bedrock, OpenSearch) and must be deleted/acknowledged from the queue after sending the user a friendly limit-exceeded response
- provide user-friendly Slack responses when limits are exceeded
- use queue-based backpressure via Amazon SQS

---

## 19. Observability and Alerting

### 19.1 Metrics
Track:
- request volume
- median/p95 latency
- no-answer rate
- low-confidence rate
- retrieval hit count
- source failure count
- ingestion freshness lag
- user feedback ratio

### 19.2 Alerts
Alert on:
- connector failures
- Amazon SQS dead-letter queue events
- queue backlog threshold exceeded
- ingestion lag threshold exceeded
- Slack webhook failures
- Amazon Bedrock invocation failures
- Amazon OpenSearch Service health issues
- elevated low-confidence rates

---

## 20. Rollout Plan

### Phase 0: Governance and Design
- confirm in-scope sources
- define excluded content
- define identity mapping approach
- define audit and retention requirements

### Phase 1: MVP
Sources:
- Confluence (all spaces)
- Slack (public channels and threads)
- Jira (all projects)
- intranet
- GitHub repos under `Sage-Bionetworks` and `Sage-Bionetworks-IT`
- PowerDMS

Capabilities:
- Slack interaction
- RAG retrieval
- citations
- feedback
- audit logging

### Phase 2: Expanded Knowledge
- Synapse metadata/docs
- better ranking
- stronger permission metadata
- admin dashboard

### Phase 3: Advanced Governance
- document-level ACL sync where feasible
- source quality scoring
- issue triage workflow

### Phase 4: Sensitive Source Review
- evaluate Leapsome and other restricted sources

---

## 21. Acceptance Criteria

### MVP Acceptance Criteria
- A user can ask a question in Slack and receive a cited answer.
- The system retrieves from all Confluence spaces, Slack public channels, Jira projects, GitHub repositories under `Sage-Bionetworks` and `Sage-Bionetworks-IT`, intranet, and PowerDMS.
- Median response time is under 8 seconds.
- Secrets are stored only in Secrets Manager.
- Queries and feedback are logged.
- The bot refuses to answer when evidence is insufficient.
- A connector can be disabled without redeploying the whole system.

---

## 22. Risks and Mitigations

### Risk: Unauthorized data exposure
Mitigation:
- source scoping
- ACL filtering
- phased onboarding
- sensitive-source review

### Risk: Hallucinated answers
Mitigation:
- strict grounding
- citation requirement
- low-confidence refusal

### Risk: Stale information
Mitigation:
- scheduled ingestion
- freshness metadata
- source-date display

### Risk: Prompt injection from source content
Mitigation:
- sanitize content
- do not allow source text to override instructions
- no agentic tools in v1

### Risk: Low user trust
Mitigation:
- citations
- confidence labels
- source links
- feedback loop

---
