import aws_cdk as core
import aws_cdk.assertions as assertions

from stacks.sage_kb_chatbot_stack import SageKbChatbotStack


def test_stack_creates_successfully():
    app = core.App()
    stack = SageKbChatbotStack(app, "sage-kb-chatbot")
    template = assertions.Template.from_stack(stack)
    # Snapshot and resource assertions will be added as constructs are implemented
