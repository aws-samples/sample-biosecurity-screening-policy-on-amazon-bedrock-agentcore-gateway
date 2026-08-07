# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structural tests for biosafety_stack.BiosafetyStack.

Bundling is disabled via the ``aws:cdk:bundling-stacks`` context key. The
ESMC/Foldseek/MMseqs2 images are now built remotely on CodeBuild rather than
via local `DockerImageAsset`/`DockerImageFunction` bundling (see
CodeBuildDockerImage), so synthesis never touches Docker at all for these
three -- the S3 `Asset` each one uploads is a plain zip. The bundling-stacks
context key still matters for the embedding-screening Lambda's
`PythonLayerVersion`, which does bundle in a container.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from research_gateway.infrastructure.biosafety_stack import (
    DEFAULT_EMBEDDING_RISK_THRESHOLD,
    DEFAULT_FOLDSEEK_RISK_THRESHOLD,
    DEFAULT_MMSEQS_RISK_THRESHOLD,
    ESMC_ENDPOINT_NAME,
    FOLDSEEK_ENDPOINT_NAME,
    BiosafetyStack,
)


def _synth(context=None):
    app = cdk.App(context={"aws:cdk:bundling-stacks": [], **(context or {})})
    stack = BiosafetyStack(app, "BiosafetyStack")
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def default_stack_and_template():
    return _synth()


@pytest.fixture(scope="module")
def template(default_stack_and_template):
    return default_stack_and_template[1]


@pytest.fixture(scope="module")
def stack(default_stack_and_template):
    return default_stack_and_template[0]


# ---------------------------------------------------------------------------
# Risk threshold context resolution
# ---------------------------------------------------------------------------


def test_default_risk_thresholds_when_no_context_given(stack):
    assert stack.mmseqs_risk_threshold == DEFAULT_MMSEQS_RISK_THRESHOLD
    assert stack.embedding_risk_threshold == DEFAULT_EMBEDDING_RISK_THRESHOLD
    assert stack.foldseek_risk_threshold == DEFAULT_FOLDSEEK_RISK_THRESHOLD


def test_risk_thresholds_can_be_overridden_via_context():
    stack, _ = _synth(
        {
            "mmseqs_risk_threshold": "9",
            "embedding_risk_threshold": "88",
            "foldseek_risk_threshold": "4",
        }
    )
    assert stack.mmseqs_risk_threshold == 9
    assert stack.embedding_risk_threshold == 88
    assert stack.foldseek_risk_threshold == 4


# ---------------------------------------------------------------------------
# SageMaker endpoints
# ---------------------------------------------------------------------------


def test_esmc_and_foldseek_endpoints_are_created(template):
    template.resource_count_is("AWS::SageMaker::Model", 2)
    template.resource_count_is("AWS::SageMaker::EndpointConfig", 2)
    template.resource_count_is("AWS::SageMaker::Endpoint", 2)


def test_esmc_endpoint_name_matches_constant(template):
    template.has_resource_properties(
        "AWS::SageMaker::Endpoint", {"EndpointName": ESMC_ENDPOINT_NAME}
    )


def test_foldseek_endpoint_name_matches_constant(template):
    template.has_resource_properties(
        "AWS::SageMaker::Endpoint", {"EndpointName": FOLDSEEK_ENDPOINT_NAME}
    )


def test_foldseek_endpoint_uses_cpu_instance_type(template):
    template.has_resource_properties(
        "AWS::SageMaker::EndpointConfig",
        {
            "ProductionVariants": Match.array_with(
                [Match.object_like({"InstanceType": "ml.c6i.2xlarge"})]
            )
        },
    )


def test_esmc_model_has_network_isolation_enabled(template):
    template.has_resource_properties(
        "AWS::SageMaker::Model", {"EnableNetworkIsolation": True}
    )


def test_model_images_reference_codebuild_custom_resource_via_get_att(template):
    # Both models' `image` must be a Fn::GetAtt on the CodeBuildDockerImage
    # trigger's custom resource -- a hand-composed string here would silently
    # drop the CloudFormation dependency on the build finishing.
    template.has_resource_properties(
        "AWS::SageMaker::Model",
        {
            "PrimaryContainer": Match.object_like(
                {"Image": Match.object_like({"Fn::GetAtt": Match.array_with(["ImageUri"])})}
            )
        },
    )


# ---------------------------------------------------------------------------
# CodeBuild-based image builds
# ---------------------------------------------------------------------------


def test_three_codebuild_projects_and_ecr_repos_are_created(template):
    template.resource_count_is("AWS::CodeBuild::Project", 3)
    template.resource_count_is("AWS::ECR::Repository", 3)


def test_codebuild_projects_are_privileged_and_use_native_x86(template):
    template.has_resource_properties(
        "AWS::CodeBuild::Project",
        {
            "Environment": Match.object_like(
                {
                    "PrivilegedMode": True,
                    "Type": "LINUX_CONTAINER",
                    "ComputeType": "BUILD_GENERAL1_LARGE",
                }
            )
        },
    )


def test_codebuild_role_does_not_use_ecr_power_user_managed_policy(template):
    # This is the least-privilege bar this design set for itself: repo-scoped
    # push permissions instead of account-wide ECR access.
    roles = template.find_resources("AWS::IAM::Role")
    for role in roles.values():
        managed_policies = role["Properties"].get("ManagedPolicyArns", [])
        for policy_ref in managed_policies:
            assert "AmazonEC2ContainerRegistryPowerUser" not in str(policy_ref)


def test_codebuild_custom_resource_triggers_exist_for_each_image(template):
    template.resource_count_is("AWS::CloudFormation::CustomResource", 3)


def test_mmseqs_function_depends_on_its_codebuild_trigger(template):
    resources = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": "mmseqs-screening"}}
    )
    assert len(resources) == 1
    depends_on = list(resources.values())[0].get("DependsOn", [])
    assert any("Trigger" in dep for dep in depends_on)


def test_sagemaker_execution_roles_use_sagemaker_full_access(template):
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Principal": {
                                        "Service": "sagemaker.amazonaws.com"
                                    }
                                }
                            )
                        ]
                    )
                }
            ),
            "ManagedPolicyArns": Match.array_with(
                [
                    Match.object_like(
                        {"Fn::Join": Match.array_with(["", Match.array_with([":iam::aws:policy/AmazonSageMakerFullAccess"])])}
                    )
                ]
            ),
        },
    )


# ---------------------------------------------------------------------------
# Screening Lambdas
# ---------------------------------------------------------------------------


def test_mmseqs_screening_function_configuration(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "mmseqs-screening",
            "MemorySize": 3008,
            "Timeout": 15,
            "Architectures": ["x86_64"],
        },
    )


def test_embedding_screening_function_environment(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "embedding-screening",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "SAGEMAKER_ENDPOINT_NAME": ESMC_ENDPOINT_NAME,
                        "TOP_K": "10",
                    }
                )
            },
        },
    )


def test_embedding_screening_function_can_invoke_esmc_endpoint(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "sagemaker:InvokeEndpoint",
                                    "Resource": Match.object_like(
                                        {
                                            "Fn::Join": Match.array_with(
                                                [
                                                    Match.array_with(
                                                        [
                                                            Match.string_like_regexp(
                                                                f".*endpoint/{ESMC_ENDPOINT_NAME}.*"
                                                            )
                                                        ]
                                                    )
                                                ]
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


# ---------------------------------------------------------------------------
# Biosafety interceptor Lambda
# ---------------------------------------------------------------------------


def test_interceptor_function_configuration(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "biosafety-interceptor",
            "Handler": "research_gateway.biosafety.interceptor.lambda_handler",
            "Timeout": 60,
            "MemorySize": 256,
        },
    )


def test_interceptor_environment_wires_screening_function_names_by_reference(template):
    resources = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": "biosafety-interceptor"}}
    )
    assert len(resources) == 1
    env_vars = list(resources.values())[0]["Properties"]["Environment"]["Variables"]
    assert "SCREENING_FUNCTION_NAME" in env_vars
    assert "EMBEDDING_SCREENING_FUNCTION_NAME" in env_vars
    assert env_vars["FOLDSEEK_ENDPOINT_NAME"] == FOLDSEEK_ENDPOINT_NAME
    # These must be CloudFormation references to the actual screening
    # functions, not hardcoded names -- otherwise a rename desyncs them.
    assert "Ref" in env_vars["SCREENING_FUNCTION_NAME"]
    assert "Ref" in env_vars["EMBEDDING_SCREENING_FUNCTION_NAME"]


def test_interceptor_can_invoke_foldseek_endpoint(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "sagemaker:InvokeEndpoint",
                                    "Resource": Match.object_like(
                                        {
                                            "Fn::Join": Match.array_with(
                                                [
                                                    Match.array_with(
                                                        [
                                                            Match.string_like_regexp(
                                                                f".*endpoint/{FOLDSEEK_ENDPOINT_NAME}.*"
                                                            )
                                                        ]
                                                    )
                                                ]
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


def test_stack_exposes_interceptor_function_for_gateway_stack(stack):
    assert stack.interceptor_function.function_name is not None
