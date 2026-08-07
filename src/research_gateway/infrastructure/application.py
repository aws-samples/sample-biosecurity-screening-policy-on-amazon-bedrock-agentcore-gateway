# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK application assembly for the Research Gateway stacks."""

import aws_cdk as cdk

from research_gateway.infrastructure.biosafety_stack import BiosafetyStack
from research_gateway.infrastructure.gateway_stack import ResearchGatewayStack


def build_app() -> cdk.App:
    """Assemble the CDK app with both stacks wired together."""
    app = cdk.App()
    biosafety_stack = BiosafetyStack(app, "BiosafetyStack")
    ResearchGatewayStack(
        app,
        "ResearchGatewayStack",
        interceptor_function=biosafety_stack.interceptor_function,
        mmseqs_risk_threshold=biosafety_stack.mmseqs_risk_threshold,
        embedding_risk_threshold=biosafety_stack.embedding_risk_threshold,
        foldseek_risk_threshold=biosafety_stack.foldseek_risk_threshold,
    )
    return app


def synth_app() -> None:
    build_app().synth()
