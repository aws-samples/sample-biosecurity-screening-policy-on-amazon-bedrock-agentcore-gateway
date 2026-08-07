# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for build_app()'s cross-stack wiring.

build_app() constructs BiosafetyStack, then passes its interceptor Lambda and
three risk thresholds into ResearchGatewayStack by keyword. This is the one
place that contract is expressed, and it is easy to silently break (wrong
kwarg name, forgotten threshold, stale attribute) without any type checker or
CDK synth catching it, since both sides just pass through **kwargs-shaped
calls.

To verify the wiring without paying for a full two-stack synth, BiosafetyStack
and ResearchGatewayStack are replaced with fakes on the application module
before calling build_app(), and the fake ResearchGatewayStack records the
kwargs it was constructed with.
"""

import aws_cdk as cdk

from research_gateway.infrastructure import application


def test_build_app_wires_biosafety_stack_outputs_into_gateway_stack(monkeypatch):
    captured_kwargs = {}

    class FakeBiosafetyStack(cdk.Stack):
        def __init__(self, scope, construct_id, **kwargs):
            super().__init__(scope, construct_id, **kwargs)
            self.interceptor_function = "SENTINEL_INTERCEPTOR_FUNCTION"
            self.mmseqs_risk_threshold = 111
            self.embedding_risk_threshold = 222
            self.foldseek_risk_threshold = 333

    class FakeResearchGatewayStack(cdk.Stack):
        def __init__(self, scope, construct_id, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__(scope, construct_id)

    monkeypatch.setattr(application, "BiosafetyStack", FakeBiosafetyStack)
    monkeypatch.setattr(application, "ResearchGatewayStack", FakeResearchGatewayStack)

    application.build_app()

    assert captured_kwargs == {
        "interceptor_function": "SENTINEL_INTERCEPTOR_FUNCTION",
        "mmseqs_risk_threshold": 111,
        "embedding_risk_threshold": 222,
        "foldseek_risk_threshold": 333,
    }
