import aws_cdk as core
import aws_cdk.assertions as assertions

from sage_kb_chatbot.sage_kb_chatbot_stack import SageKbChatbotStack


# example tests. To run these tests, uncomment this file along with the example
# resource in sage_kb_chatbot/sage_kb_chatbot_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = SageKbChatbotStack(app, "sage-kb-chatbot")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
