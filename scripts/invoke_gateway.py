# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Calls the AgentCore Gateway MCP endpoint with SigV4-signed requests.

The gateway uses IAM inbound auth, so there is no token to fetch — this signs with
whatever credentials the standard AWS chain resolves. The calling principal needs
`bedrock-agentcore:InvokeGateway` on the gateway ARN; note that AmazonBedrockFullAccess
does *not* grant it, since `bedrock-agentcore` is a separate service prefix.

For interactive use (MCP Inspector, Claude Code), prefer `mcp-proxy-for-aws` — see the
Authentication section of the top-level README. This script is for smoke tests and CI.

Usage:
    python scripts/invoke_gateway.py --list-tools # Allowed

    python scripts/invoke_gateway.py --tool pmc-search___search_pmc --args '{"query": "Botulism toxin"}' # Allowed

    python scripts/invoke_gateway.py --tool uniprot-search___search_uniprot --args '{"query": "Insulin", "species": "Homo sapiens"}' # Allowed
    python scripts/invoke_gateway.py --tool uniprot-search___search_uniprot --args '{"query": "neurotoxin type G", "species": "Clostridium botulinum"}' # Blocked by policy
    
    python scripts/invoke_gateway.py --tool ordering-tool___ordering_tool --args '{"inputs": ["MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCG"]}' # Partial Human Insulin Chain B Allowed
    python scripts/invoke_gateway.py --tool ordering-tool___ordering_tool --args '{"inputs": ["MPVNIKXFNYNDPINNDDIIMMEPFNDPGPGTYYKAFRIIDRIWIVPERFTYGFQPDQFN"]}' # Partial Botulism neurotoxin Blocked by policy

"""

import argparse
import http.client
import json
import ssl
import sys
from urllib.parse import SplitResult, urlsplit, urlunsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SERVICE = "bedrock-agentcore"
DEFAULT_STACK = "ResearchGatewayStack"
DEFAULT_REGION = "us-east-1"
PROTOCOL_VERSION = "2025-03-26"
_HTTPS_CONTEXT = ssl.create_default_context()


def resolve_gateway_url(stack_name: str, region: str) -> str:
    """Read the GatewayUrl output from the deployed stack."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except Exception as exc:
        sys.exit(f"Could not describe stack {stack_name}: {exc}")
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == "GatewayUrl":
            return output["OutputValue"]
    sys.exit(f"Stack {stack_name} has no GatewayUrl output — is it fully deployed?")


def _validate_gateway_url(url: str, region: str) -> SplitResult:
    """Accept only the regional HTTPS AgentCore Gateway endpoint before signing."""
    endpoint = urlsplit(url)
    expected_suffix = f".gateway.bedrock-agentcore.{region}.amazonaws.com"
    hostname = endpoint.hostname.lower() if endpoint.hostname else ""

    try:
        port = endpoint.port
    except ValueError as exc:
        raise ValueError("Gateway URL has an invalid port") from exc

    if endpoint.scheme != "https" or not hostname:
        raise ValueError("Gateway URL must use HTTPS and include a hostname")
    if endpoint.username or endpoint.password or endpoint.fragment:
        raise ValueError("Gateway URL must not include credentials or a fragment")
    if port not in (None, 443):
        raise ValueError("Gateway URL must use the default HTTPS port")
    if not hostname.endswith(expected_suffix) or hostname == expected_suffix[1:]:
        raise ValueError(f"Gateway URL must target an AgentCore Gateway in {region}")
    return endpoint


def parse_body(content_type: str, raw: str) -> dict:
    """Handle both plain JSON and SSE-framed (text/event-stream) responses."""
    if "text/event-stream" in content_type:
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ValueError(f"No data: frame in SSE response: {raw[:400]}")
    return json.loads(raw)


class GatewayClient:
    def __init__(self, url: str, region: str):
        self.url = url
        self.endpoint = _validate_gateway_url(url, region)
        self.region = region
        self.session_id: str | None = None
        creds = boto3.Session().get_credentials()
        if creds is None:
            sys.exit("No AWS credentials found. Configure a profile or set AWS_PROFILE.")
        self.creds = creds.get_frozen_credentials()

    def _post(self, payload: dict) -> tuple[int, dict | None, dict]:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        signed = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        SigV4Auth(self.creds, SERVICE, self.region).add_auth(signed)
        request_path = urlunsplit(("", "", self.endpoint.path or "/", self.endpoint.query, ""))
        # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected -- AgentCore hostname is validated before SigV4 signing and this context verifies certificates and hostnames.
        connection = http.client.HTTPSConnection(
            self.endpoint.hostname,
            self.endpoint.port or 443,
            timeout=120,
            context=_HTTPS_CONTEXT,
        )
        try:
            connection.request("POST", request_path, body=body, headers=dict(signed.headers))
            response = connection.getresponse()
            raw = response.read().decode()
            status = response.status
            content_type = response.getheader("Content-Type", "")
            response_headers = dict(response.getheaders())
            session_id = response.getheader("Mcp-Session-Id")
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"HTTPS request to AgentCore Gateway failed: {exc}") from exc
        finally:
            connection.close()

        if session_id:
            self.session_id = session_id
        if status >= 400:
            try:
                detail = parse_body(content_type, raw) if raw else None
            except ValueError:
                detail = {"raw": raw[:400]}
            return status, detail, response_headers
        return status, parse_body(content_type, raw) if raw else None, response_headers

    def request(self, method: str, params: dict | None = None, request_id: int = 1):
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload)

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def initialize(self) -> None:
        status, body, _ = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "invoke_gateway.py", "version": "1.0"},
            },
        )
        fail_on_error(status, body, "initialize")
        self.notify("notifications/initialized")


def fail_on_error(status: int, body: dict | None, what: str) -> None:
    if status == 403:
        sys.exit(
            f"403 on {what} — the signature was accepted but the caller is not authorized.\n"
            "Grant the calling principal bedrock-agentcore:InvokeGateway on the GatewayArn\n"
            "stack output. Check with:\n"
            "  aws iam simulate-principal-policy --policy-source-arn <role-arn> \\\n"
            "    --action-names bedrock-agentcore:InvokeGateway --resource-arns <GatewayArn>"
        )
    if status >= 400:
        sys.exit(f"HTTP {status} on {what}: {json.dumps(body, indent=2) if body else '<empty>'}")
    if body and "error" in body:
        sys.exit(f"MCP error on {what}: {json.dumps(body['error'], indent=2)}")


EXAMPLES = (
    ("List available tools", None, None),
    ("Allowed PMC search", "pmc-search___search_pmc", {"query": "Botulism toxin"}),
    (
        "Allowed UniProt search",
        "uniprot-search___search_uniprot",
        {"query": "Insulin", "species": "Homo sapiens"},
    ),
    (
        "UniProt search expected to be blocked by policy",
        "uniprot-search___search_uniprot",
        {"query": "neurotoxin type G", "species": "Clostridium botulinum"},
    ),
    (
        "Allowed partial human insulin chain B order",
        "ordering-tool___ordering_tool",
        {"inputs": ["MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCG"]},
    ),
    (
        "Partial botulism neurotoxin order expected to be blocked by policy",
        "ordering-tool___ordering_tool",
        {"inputs": ["MPVNIKXFNYNDPINNDDIIMMEPFNDPGPGTYYKAFRIIDRIWIVPERFTYGFQPDQFN"]},
    ),
)


def _list_tools(client: GatewayClient, request_id: int) -> None:
    status, body, _ = client.request("tools/list", request_id=request_id)
    fail_on_error(status, body, "tools/list")
    tools = (body or {}).get("result", {}).get("tools", [])
    print(f"{len(tools)} tool(s):", file=sys.stderr)
    for tool in tools:
        print(f"  - {tool.get('name')}", file=sys.stderr)
    print(json.dumps(body, indent=2))


def _call_tool(
    client: GatewayClient, tool_name: str, tool_args: dict, request_id: int
) -> None:
    status, body, _ = client.request(
        "tools/call", {"name": tool_name, "arguments": tool_args}, request_id=request_id
    )
    # A Cedar forbid surfaces as an MCP error, which is a valid outcome to inspect
    # rather than an exception — print it instead of exiting non-zero on 200s.
    if status >= 400:
        fail_on_error(status, body, f"tools/call {tool_name}")
    print(json.dumps(body, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Invoke the AgentCore Gateway MCP endpoint with SigV4 auth"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list-tools", action="store_true", help="Call tools/list")
    action.add_argument("--tool", help="Tool name to call, e.g. pmc-search___search_pmc or uniprot-search___search_uniprot")
    parser.add_argument("--args", default="{}", help="JSON arguments for --tool (default: {})")
    parser.add_argument("--url", help="Gateway MCP URL (default: read GatewayUrl from the stack)")
    parser.add_argument("--stack", default=DEFAULT_STACK, help=f"Stack to read GatewayUrl from (default: {DEFAULT_STACK})")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    invoked_without_options = len(sys.argv) == 1
    args = parser.parse_args()

    if not invoked_without_options and not (args.list_tools or args.tool):
        parser.error("provide --list-tools or --tool")
    if args.list_tools and args.args != "{}":
        parser.error("--args requires --tool")

    try:
        tool_args = json.loads(args.args)
    except json.JSONDecodeError as exc:
        sys.exit(f"--args is not valid JSON: {exc}")

    url = args.url or resolve_gateway_url(args.stack, args.region)
    print(f"POST {url}", file=sys.stderr)

    client = GatewayClient(url, args.region)
    client.initialize()

    if invoked_without_options:
        for request_id, (description, tool_name, example_args) in enumerate(EXAMPLES, start=2):
            print(f"\nExample {request_id - 1}/{len(EXAMPLES)}: {description}", file=sys.stderr)
            if tool_name is None:
                _list_tools(client, request_id)
            else:
                _call_tool(client, tool_name, example_args, request_id)
    elif args.list_tools:
        _list_tools(client, request_id=2)
    else:
        _call_tool(client, args.tool, tool_args, request_id=2)


if __name__ == "__main__":
    main()
