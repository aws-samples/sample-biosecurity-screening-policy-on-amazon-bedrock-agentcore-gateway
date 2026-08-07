# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest


def pytest_addoption(parser):
    """Register opt-in controls for tests that call a deployed gateway."""
    group = parser.getgroup("gateway integration")
    group.addoption(
        "--run-gateway-integration",
        action="store_true",
        help="run tests marked gateway_integration against a deployed gateway",
    )
    group.addoption(
        "--run-gateway-full",
        action="store_true",
        help="also run gateway_full tests that invoke the biosafety screeners",
    )
    group.addoption(
        "--gateway-url",
        help="deployed AgentCore Gateway URL (defaults to the stack GatewayUrl output)",
    )
    group.addoption(
        "--gateway-stack",
        default="ResearchGatewayStack",
        help="CloudFormation stack containing GatewayUrl (default: ResearchGatewayStack)",
    )
    group.addoption(
        "--gateway-region",
        default="us-east-1",
        help="AWS Region for the gateway and stack lookup (default: us-east-1)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "gateway_integration: calls a deployed AgentCore Gateway with ambient AWS credentials",
    )
    config.addinivalue_line(
        "markers",
        "gateway_full: gateway integration test that invokes the biosafety screeners",
    )


def pytest_collection_modifyitems(config, items):
    """Skip deployed checks unless the caller explicitly opts in."""
    run_integration = config.getoption("--run-gateway-integration")
    run_full = config.getoption("--run-gateway-full")
    for item in items:
        if item.get_closest_marker("gateway_full") and not run_full:
            item.add_marker(
                pytest.mark.skip(
                    reason="requires --run-gateway-full to invoke deployed biosafety screeners"
                )
            )
        elif item.get_closest_marker("gateway_integration") and not (
            run_integration or run_full
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason="requires --run-gateway-integration and deployed AWS resources"
                )
            )


@pytest.fixture(autouse=True)
def _hermetic_aws_environment(monkeypatch, request):
    """Give ordinary tests fixed credentials without masking integration credentials.

    Several modules under test (research_gateway.biosafety.interceptor) create
    boto3 clients at import time. Gateway integration tests deliberately use
    the caller's standard credential chain to exercise SigV4 authentication.
    """
    if request.node.get_closest_marker("gateway_integration"):
        yield
        return

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    yield
