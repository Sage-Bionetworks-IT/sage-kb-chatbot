#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.sage_kb_chatbot_stack import SageKbChatbotStack


app = cdk.App()

SageKbChatbotStack(
    app,
    "SageKbChatbotStack",
    # Uncomment and configure for environment-specific deployment:
    # env=cdk.Environment(
    #     account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    #     region=os.getenv("CDK_DEFAULT_REGION"),
    # ),
)

app.synth()
