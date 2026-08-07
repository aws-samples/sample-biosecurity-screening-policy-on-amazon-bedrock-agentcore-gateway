# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Opt-in checks for a deployed AgentCore Gateway MCP endpoint.

These tests intentionally import the smoke-test client instead of recreating
its protocol: every request is SigV4 signed and each client calls initialize
followed by notifications/initialized before using MCP tools.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_GATEWAY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "invoke_gateway.py"
_GATEWAY_SPEC = importlib.util.spec_from_file_location("invoke_gateway", _GATEWAY_SCRIPT)
assert _GATEWAY_SPEC is not None and _GATEWAY_SPEC.loader is not None
_gateway_module = importlib.util.module_from_spec(_GATEWAY_SPEC)
_GATEWAY_SPEC.loader.exec_module(_gateway_module)

DEFAULT_REGION = _gateway_module.DEFAULT_REGION
GatewayClient = _gateway_module.GatewayClient
resolve_gateway_url = _gateway_module.resolve_gateway_url

PMC_SEARCH_TOOL = "pmc-search___search_pmc"
UNIPROT_SEARCH_TOOL = "uniprot-search___search_uniprot"
ORDERING_TOOL = "ordering-tool___ordering_tool"
EXPECTED_TOOLS = {PMC_SEARCH_TOOL, UNIPROT_SEARCH_TOOL, ORDERING_TOOL}
FOLDSEEK_DATABASE_SEQUENCE_1A2A = (
    "NLLQFNKMIKEETGKNAIPFYAFYGCYCGGGGNGKPKDGTDRCCFVHDCCYGRLVNCNTKSDIYSYSL"
    "KEGYITCGKGTNCEEQICECDRVAAECFRRNLDTYNNGYMFYRDSKCTETSEEC"
)


@pytest.fixture(scope="session")
def gateway_endpoint(pytestconfig) -> tuple[str, str]:
    """Use an explicit endpoint or resolve GatewayUrl from the deployed stack."""
    region = pytestconfig.getoption("--gateway-region") or DEFAULT_REGION
    url = pytestconfig.getoption("--gateway-url")
    if not url:
        url = resolve_gateway_url(pytestconfig.getoption("--gateway-stack"), region)
    return url, region


@pytest.fixture
def gateway_client(gateway_endpoint) -> GatewayClient:
    """Create a fresh initialized MCP session using ambient AWS credentials."""
    url, region = gateway_endpoint
    client = GatewayClient(url, region)
    client.initialize()
    return client


def _tool_call(client: GatewayClient, name: str, arguments: dict) -> dict:
    status, body, _ = client.request(
        "tools/call", {"name": name, "arguments": arguments}, request_id=2
    )
    assert status == 200, json.dumps(body, indent=2)
    assert body is not None
    assert body.get("jsonrpc") == "2.0"
    return body


@pytest.mark.gateway_integration
def test_gateway_initializes_and_lists_deployed_tools(gateway_client):
    status, body, _ = gateway_client.request("tools/list", request_id=2)

    assert status == 200, json.dumps(body, indent=2)
    assert body is not None
    assert body.get("jsonrpc") == "2.0"
    tools = body.get("result", {}).get("tools", [])
    assert EXPECTED_TOOLS <= {tool.get("name") for tool in tools}


@pytest.mark.gateway_integration
def test_gateway_permits_pmc_search(gateway_client):
    body = _tool_call(
        gateway_client,
        PMC_SEARCH_TOOL,
        {"query": "CRISPR gene editing"},
    )

    result = body.get("result", {})
    assert result.get("isError") is False, json.dumps(body, indent=2)
    assert result.get("structuredContent", {}).get("status") == "success"


@pytest.mark.gateway_integration
def test_gateway_forbids_blocked_uniprot_species(gateway_client):
    body = _tool_call(
        gateway_client,
        UNIPROT_SEARCH_TOOL,
        {
            "query": "neurotoxin type G",
            "species": "Clostridium botulinum",
        },
    )

    # Cedar forbids are valid MCP responses: AgentCore returns a JSON-RPC error
    # with HTTP 200 after a request has been authenticated and evaluated.
    assert "error" in body, json.dumps(body, indent=2)


@pytest.mark.gateway_integration
@pytest.mark.gateway_full
def test_gateway_allows_low_risk_order_after_screening(gateway_client):
    body = _tool_call(
        gateway_client,
        ORDERING_TOOL,
        {"inputs": ["MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCG"]},
    )

    result = body.get("result", {})
    assert result.get("isError") is False, json.dumps(body, indent=2)
    assert result.get("structuredContent", {}).get("status") == "success"


@pytest.mark.gateway_integration
@pytest.mark.gateway_full
def test_gateway_forbids_high_risk_order_after_screening(gateway_client):
    body = _tool_call(
        gateway_client,
        ORDERING_TOOL,
        {
            "inputs": [
                "MPVNIKXFNYNDPINNDDIIMMEPFNDPGPGTYYKAFRIIDRIWIVPERFTYGFQPDQFN"
            ]
        },
    )

    assert "error" in body, json.dumps(body, indent=2)


@pytest.mark.gateway_integration
@pytest.mark.gateway_full
def test_gateway_forbids_exact_foldseek_database_sequence(gateway_client):
    """A sequence from 1a2a.pdb must be denied after full biosafety screening."""
    body = _tool_call(
        gateway_client,
        ORDERING_TOOL,
        {"inputs": [FOLDSEEK_DATABASE_SEQUENCE_1A2A]},
    )

    assert "error" in body, json.dumps(body, indent=2)
