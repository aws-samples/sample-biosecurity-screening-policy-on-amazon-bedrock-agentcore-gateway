# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structural tests for gateway_stack.ResearchGatewayStack.

Bundling is disabled via the ``aws:cdk:bundling-stacks`` context key so
synthesis never shells out to Docker for the PythonLayerVersion dependency
layer -- these tests only assert on the generated CloudFormation template,
not on the built Lambda artifacts.
"""

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as _lambda
from aws_cdk.assertions import Match, Template

from research_gateway.infrastructure.gateway_stack import ResearchGatewayStack

MMSEQS_THRESHOLD = 7
EMBEDDING_THRESHOLD = 90
FOLDSEEK_THRESHOLD = 3


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    deps_stack = cdk.Stack(app, "DummyDeps")
    interceptor_function = _lambda.Function(
        deps_stack,
        "DummyInterceptor",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=_lambda.Code.from_inline("def handler(event, context): return {}"),
    )

    stack = ResearchGatewayStack(
        app,
        "ResearchGatewayStack",
        interceptor_function=interceptor_function,
        mmseqs_risk_threshold=MMSEQS_THRESHOLD,
        embedding_risk_threshold=EMBEDDING_THRESHOLD,
        foldseek_risk_threshold=FOLDSEEK_THRESHOLD,
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Gateway resource
# ---------------------------------------------------------------------------


def test_gateway_uses_iam_authorizer(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {"AuthorizerType": "AWS_IAM"},
    )


def test_gateway_policy_engine_is_enforce_mode(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {"PolicyEngineConfiguration": Match.object_like({"Mode": "ENFORCE"})},
    )


def test_gateway_has_request_interceptor_attached(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {
            "InterceptorConfigurations": Match.array_with(
                [Match.object_like({"InterceptionPoints": ["REQUEST"]})]
            )
        },
    )


def test_exactly_one_gateway_and_one_policy_engine(template):
    template.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)
    template.resource_count_is("AWS::BedrockAgentCore::PolicyEngine", 1)


# ---------------------------------------------------------------------------
# Lambda functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "function_name,handler,timeout,memory",
    [
        ("search-pmc", "research_gateway.tools.pmc.handler.handler", 60, 512),
        ("uniprot-search", "research_gateway.tools.uniprot.handler.handler", 60, 512),
        ("ordering-tool", "research_gateway.tools.ordering.handler.handler", 30, 128),
    ],
)
def test_lambda_function_configuration(template, function_name, handler, timeout, memory):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": function_name,
            "Handler": handler,
            "Runtime": "python3.12",
            "Timeout": timeout,
            "MemorySize": memory,
        },
    )


def test_search_pmc_and_uniprot_share_the_dependencies_layer(template):
    template.resource_count_is("AWS::Lambda::LayerVersion", 1)


# ---------------------------------------------------------------------------
# Gateway targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_name", ["pmc-search", "uniprot-search", "ordering-tool"]
)
def test_gateway_target_exists_for_each_tool(template, target_name):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::GatewayTarget",
        {"Name": target_name},
    )


def test_three_gateway_targets_total(template):
    template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 3)


# ---------------------------------------------------------------------------
# Cedar policies
# ---------------------------------------------------------------------------


def test_six_cedar_policies_are_defined(template):
    template.resource_count_is("AWS::BedrockAgentCore::Policy", 6)


def test_allow_all_policy_permits_everything(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Policy",
        {
            "Definition": Match.object_like(
                {
                    "Cedar": Match.object_like(
                        {
                            "Statement": Match.object_like(
                                {
                                    "Fn::Join": Match.array_with(
                                        [
                                            "",
                                            Match.array_with(
                                                ["permit(principal, action, resource == AgentCore::Gateway::\""]
                                            ),
                                        ]
                                    )
                                }
                            )
                        }
                    )
                }
            ),
            "Description": "Allow all requests through (development only).",
        },
    )


def _cedar_statement_fragments(resource_properties):
    return resource_properties["Definition"]["Cedar"]["Statement"]["Fn::Join"][1]


def test_blocked_species_policy_lists_configured_species(template):
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    matches = [
        v
        for v in resources.values()
        if v["Properties"].get("Description", "").startswith(
            "Block search_uniprot requests"
        )
    ]
    assert len(matches) == 1
    fragments = _cedar_statement_fragments(matches[0]["Properties"])
    joined = "".join(f for f in fragments if isinstance(f, str))
    assert '"Clostridium botulinum"' in joined
    assert '"Gloydius halys"' in joined


@pytest.mark.parametrize(
    "score_key,threshold",
    [
        ("_biosafety_mmseqs_risk_score", MMSEQS_THRESHOLD),
        ("_biosafety_embedding_risk_score", EMBEDDING_THRESHOLD),
        ("_biosafety_foldseek_risk_score", FOLDSEEK_THRESHOLD),
    ],
)
def test_biosafety_forbid_policy_embeds_configured_threshold(
    template, score_key, threshold
):
    resources = template.find_resources("AWS::BedrockAgentCore::Policy")
    matches = [
        v
        for v in resources.values()
        if score_key in "".join(
            f
            for f in _cedar_statement_fragments(v["Properties"])
            if isinstance(f, str)
        )
    ]
    assert len(matches) == 1
    joined = "".join(
        f for f in _cedar_statement_fragments(matches[0]["Properties"]) if isinstance(f, str)
    )
    assert f"{score_key} > {threshold}" in joined
    assert "forbid(principal, action, resource ==" in joined


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output_name", ["PolicyEngineId", "GatewayId", "GatewayUrl", "GatewayArn"]
)
def test_expected_output_exists(template, output_name):
    template.has_output(output_name, {})
