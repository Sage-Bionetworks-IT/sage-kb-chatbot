from aws_cdk import (
    Stack,
)
from constructs import Construct


class SageKbChatbotStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Constructs will be composed here as they are implemented
        pass
