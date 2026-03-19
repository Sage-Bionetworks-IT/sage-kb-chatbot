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
                                              ├── GitHub
                                              ├── Intranet
                                              └── PowerDMS
```

## Knowledge Sources (MVP)

- Confluence (all spaces)
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

## Tech Stack

- **IaC**: AWS CDK (Python)
- **Runtime**: Python
- **Compute**: AWS Lambda (Slack ingress), ECS Fargate (RAG orchestrator + connectors)
- **Search**: Amazon OpenSearch Service
- **Database**: Amazon RDS PostgreSQL
- **LLM/Embeddings**: Amazon Bedrock (Titan Text Embeddings + generation model)
- **Orchestration**: Step Functions, EventBridge, SQS
- **Security**: Secrets Manager, KMS, IAM least-privilege, VPC private subnets

## Project Structure

```
app.py                  # CDK app entry point
stacks/                 # CDK stacks
cdk_constructs/         # CDK constructs
stages/                 # CDK stages
lambda/
  slack_ingress/        # Slack event receiver Lambda
containers/
  rag_orchestrator/     # RAG orchestrator ECS service
  connectors/           # Source connector workers
tests/                  # Unit and integration tests
```

## Getting Started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Synthesize CloudFormation template (defaults to dev)
cdk synth

# Target a specific environment
DEPLOY_ENV=staging cdk synth
DEPLOY_ENV=prod cdk synth
```

## Running Tests

```bash
pytest -q
```

## Environments

Set `DEPLOY_ENV` to target a specific environment:

| Value     | Stack Name              |
|-----------|-------------------------|
| `dev`     | SageKbChatbot-Dev       |
| `staging` | SageKbChatbot-Staging   |
| `prod`    | SageKbChatbot-Prod      |
