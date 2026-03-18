#!/usr/bin/env python3
"""CDK app entry point for the Sage Internal Knowledge Slack Chatbot.

Instantiates environment-specific stacks based on the DEPLOY_ENV environment
variable. Supported environments: dev, staging, prod.

Usage:
    DEPLOY_ENV=dev cdk synth
    DEPLOY_ENV=prod cdk synth
"""
import os

import aws_cdk as cdk

from stacks.sage_kb_chatbot_stack import SageKbChatbotStack

app = cdk.App()

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
# Account and region are resolved from standard CDK environment variables.
# Set CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION, or override per-environment
# below when deploying to explicit targets.
# ---------------------------------------------------------------------------

DEPLOY_ENV = os.getenv("DEPLOY_ENV", "dev")

ENV_CONFIG: dict[str, dict] = {
    "dev": {
        "env": cdk.Environment(
            account=os.getenv("CDK_DEFAULT_ACCOUNT"),
            region=os.getenv("CDK_DEFAULT_REGION", "us-east-1"),
        ),
        "description": "Sage KB Chatbot – Development",
    },
    "staging": {
        "env": cdk.Environment(
            account=os.getenv("CDK_DEFAULT_ACCOUNT"),
            region=os.getenv("CDK_DEFAULT_REGION", "us-east-1"),
        ),
        "description": "Sage KB Chatbot – Staging",
    },
    "prod": {
        "env": cdk.Environment(
            account=os.getenv("CDK_DEFAULT_ACCOUNT"),
            region=os.getenv("CDK_DEFAULT_REGION", "us-east-1"),
        ),
        "description": "Sage KB Chatbot – Production",
    },
}

if DEPLOY_ENV not in ENV_CONFIG:
    raise ValueError(
        f"Unknown DEPLOY_ENV '{DEPLOY_ENV}'. "
        f"Must be one of: {', '.join(ENV_CONFIG)}"
    )

config = ENV_CONFIG[DEPLOY_ENV]

SageKbChatbotStack(
    app,
    f"SageKbChatbot-{DEPLOY_ENV.capitalize()}",
    env=config["env"],
    description=config["description"],
)

app.synth()
