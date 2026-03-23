# Sage Internal Knowledge Slack Chatbot

An internal Slack chatbot that answers Sage Bionetworks employee questions using a Retrieval-Augmented Generation (RAG) architecture. It ingests content from multiple internal knowledge sources, indexes it into a hybrid search engine, and generates grounded, cited answers delivered through Slack.

## Architecture

```
Slack → API Gateway → Lambda (ingress) → SQS → ECS Fargate (RAG orchestrator)
                                                      ├── OpenSearch (hybrid search)
                                                      ├── RDS PostgreSQL (metadata & audit)
                                                      ├── Amazon Bedrock (embeddings + LLM)
                                                      └── Slack API (response)

Ingestion: EventBridge → Step Functions → ECS Fargate (connector workers)
                                              ├── Confluence
                                              ├── Jira
                                              ├── GitHub
                                              ├── Intranet
                                              └── PowerDMS
```
![Sage KB Chatbot Architecture](docs/sage-kb-chatbot-architecture.png)


## Knowledge Sources (MVP)

- Confluence (all spaces)
- Slack (public channels and threads)
- Jira (all projects)
- GitHub (all repos under `Sage-Bionetworks` and `Sage-Bionetworks-IT`)
- Intranet
- PowerDMS

## Key Capabilities

- Grounded answers with source citations and confidence levels
- Hybrid search (keyword + vector) with source authority ranking
- Permission-aware retrieval
- Auditable queries, feedback, and ingestion history
- Per-user and per-channel rate limiting
- Independent connector enable/disable without redeployment

## Documentation

- [Contributing](CONTRIBUTING.md) — setup instructions, project structure, coding standards, development workflow
