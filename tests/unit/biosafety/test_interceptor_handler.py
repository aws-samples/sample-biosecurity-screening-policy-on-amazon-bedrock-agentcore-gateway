# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for research_gateway.biosafety.interceptor — the biosafety REQUEST
interceptor that screens gateway tool calls and injects risk-score context
for Cedar policies.

boto3 clients are created at module import time, so each test replaces the two
module-level clients with mocks via monkeypatch.setattr, which restores the
originals afterward regardless of import caching.
"""

import json

import pytest

from research_gateway.biosafety import interceptor as interceptor_module


@pytest.fixture
def interceptor(monkeypatch):
    monkeypatch.setenv("SCREENING_FUNCTION_NAME", "mmseqs-screening")
    monkeypatch.setenv("EMBEDDING_SCREENING_FUNCTION_NAME", "embedding-screening")
    monkeypatch.setenv("FOLDSEEK_ENDPOINT_NAME", "foldseek-prostt5")
    monkeypatch.setattr(interceptor_module, "_lambda_client", _FakeLambdaClient())
    monkeypatch.setattr(interceptor_module, "_sagemaker_runtime", _FakeSageMakerRuntime())
    return interceptor_module


class _FakeLambdaClient:
    """Routes Lambda invokes by FunctionName to canned or callable payloads."""

    def __init__(self):
        self.responses = {}
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        self.calls.append((FunctionName, json.loads(Payload)))
        responder = self.responses.get(FunctionName)
        if responder is None:
            payload, function_error = {}, None
        elif callable(responder):
            payload, function_error = responder(json.loads(Payload))
        else:
            payload, function_error = responder, None

        response = {"Payload": _StreamLike(json.dumps(payload))}
        if function_error:
            response["FunctionError"] = function_error
        return response


class _FakeSageMakerRuntime:
    def __init__(self):
        self.response_body = {"min_evalue": None, "count": 0}
        self.raise_error = None

    def invoke_endpoint(self, EndpointName, ContentType, Body):  # noqa: N803
        if self.raise_error:
            raise self.raise_error
        return {"Body": _StreamLike(json.dumps(self.response_body))}


class _StreamLike:
    def __init__(self, text):
        self._text = text.encode()

    def read(self):
        return self._text


VALID_SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"


def _tools_call_body(arguments, tool_name="ordering-tool___ordering_tool"):
    return {
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def _event(body):
    return {"mcp": {"gatewayRequest": {"headers": {"h": "1"}, "body": body}}}


# ---------------------------------------------------------------------------
# _screen / _screen_embedding / _screen_foldseek
# ---------------------------------------------------------------------------


def test_screen_invokes_configured_function_name(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-10,
        "count": 3,
    }
    result = interceptor._screen(VALID_SEQUENCE)
    assert result == {"min_evalue": 1e-10, "count": 3}
    function_name, payload = interceptor._lambda_client.calls[0]
    assert function_name == "mmseqs-screening"
    assert payload == {"sequence": VALID_SEQUENCE}


def test_screen_raises_on_function_error(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = (
        lambda payload: ({"errorMessage": "boom"}, "Unhandled")
    )
    with pytest.raises(RuntimeError, match="Screening Lambda error"):
        interceptor._screen(VALID_SEQUENCE)


def test_screen_embedding_returns_none_when_unconfigured(interceptor, monkeypatch):
    monkeypatch.delenv("EMBEDDING_SCREENING_FUNCTION_NAME", raising=False)
    assert interceptor._screen_embedding(VALID_SEQUENCE) is None


def test_screen_embedding_invokes_configured_function(interceptor):
    interceptor._lambda_client.responses["embedding-screening"] = {
        "max_similarity": 0.42
    }
    result = interceptor._screen_embedding(VALID_SEQUENCE)
    assert result == {"max_similarity": 0.42}


def test_screen_foldseek_returns_none_when_unconfigured(interceptor, monkeypatch):
    monkeypatch.delenv("FOLDSEEK_ENDPOINT_NAME", raising=False)
    assert interceptor._screen_foldseek(VALID_SEQUENCE) is None


def test_screen_foldseek_invokes_sagemaker_endpoint(interceptor):
    interceptor._sagemaker_runtime.response_body = {"min_evalue": 1e-8, "count": 1}
    result = interceptor._screen_foldseek(VALID_SEQUENCE)
    assert result == {"min_evalue": 1e-8, "count": 1}


# ---------------------------------------------------------------------------
# lambda_handler() — pass-through paths
# ---------------------------------------------------------------------------


def test_lambda_handler_passes_through_non_tools_call_requests(interceptor):
    body = {"method": "tools/list", "params": {}}
    result = interceptor.lambda_handler(_event(body), None)
    assert result["mcp"]["transformedGatewayRequest"]["body"] == body


def test_lambda_handler_passes_through_when_no_sequences_found(interceptor):
    body = _tools_call_body({"query": "SARS-CoV-2 spike protein"})
    result = interceptor.lambda_handler(_event(body), None)
    assert result["mcp"]["transformedGatewayRequest"]["body"] == body


def test_lambda_handler_passes_through_calls_to_other_targets(interceptor):
    body = _tools_call_body(
        {"query": VALID_SEQUENCE}, tool_name="uniprot-search___search_uniprot"
    )
    result = interceptor.lambda_handler(_event(body), None)
    assert result["mcp"]["transformedGatewayRequest"]["body"] == body
    assert interceptor._lambda_client.calls == []


# ---------------------------------------------------------------------------
# lambda_handler() — screening path
# ---------------------------------------------------------------------------


def test_lambda_handler_injects_risk_scores_when_sequence_found(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-3,
        "count": 2,
    }
    interceptor._lambda_client.responses["embedding-screening"] = {
        "max_similarity": 0.5
    }
    interceptor._sagemaker_runtime.response_body = {"min_evalue": 1e-2, "count": 1}

    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)

    transformed_args = result["mcp"]["transformedGatewayRequest"]["body"]["params"][
        "arguments"
    ]
    assert transformed_args["query"] == VALID_SEQUENCE
    assert transformed_args["_biosafety_mmseqs_risk_score"] == 3  # -log10(1e-3)
    assert transformed_args["_biosafety_embedding_risk_score"] == 50  # int(0.5 * 100)
    assert transformed_args["_biosafety_foldseek_risk_score"] == 2  # -log10(1e-2)
    assert transformed_args["_biosafety_sequences_found"] == 1
    assert transformed_args["_biosafety_embedding_max_similarity"] == 0.5
    assert "_biosafety_screened_at" in transformed_args


def test_lambda_handler_does_not_mutate_original_body(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-3,
        "count": 1,
    }
    body = _tools_call_body({"query": VALID_SEQUENCE})
    interceptor.lambda_handler(_event(body), None)
    assert "_biosafety_mmseqs_risk_score" not in body["params"]["arguments"]


def test_lambda_handler_risk_score_zero_when_no_evalue(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": None,
        "count": 0,
    }
    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)
    transformed_args = result["mcp"]["transformedGatewayRequest"]["body"]["params"][
        "arguments"
    ]
    assert transformed_args["_biosafety_mmseqs_risk_score"] == 0
    assert transformed_args["_biosafety_embedding_risk_score"] == 0
    assert transformed_args["_biosafety_foldseek_risk_score"] == 0


def test_lambda_handler_risk_score_for_very_small_evalue_is_uncapped_below_999(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-300,
        "count": 1,
    }
    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)
    transformed_args = result["mcp"]["transformedGatewayRequest"]["body"]["params"][
        "arguments"
    ]
    assert transformed_args["_biosafety_mmseqs_risk_score"] == 300


def test_lambda_handler_risk_score_zero_when_evalue_underflows_to_zero(interceptor):
    # float64 cannot represent evalues small enough to push -log10(evalue)
    # above ~323, so the `min(..., 999)` cap in handler.py is unreachable via
    # a real evalue. Below the smallest representable positive double,
    # Python's own float parsing rounds the value to 0.0, and `> 0` guards
    # that branch -- so a "perfect" (fully underflowed) match scores 0, not
    # a high risk score. This documents that behavior rather than asserting
    # it is desirable.
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-1000,
        "count": 1,
    }
    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)
    transformed_args = result["mcp"]["transformedGatewayRequest"]["body"]["params"][
        "arguments"
    ]
    assert transformed_args["_biosafety_mmseqs_risk_score"] == 0


def test_lambda_handler_returns_error_response_when_screening_fails(interceptor):
    interceptor._lambda_client.responses["mmseqs-screening"] = (
        lambda payload: ({"errorMessage": "boom"}, "Unhandled")
    )
    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)
    assert result["mcp"]["error"]["code"] == -32000
    assert "Mmseqs screening failed" in result["mcp"]["error"]["message"]


def test_lambda_handler_skips_embedding_and_foldseek_when_unconfigured(
    interceptor, monkeypatch
):
    monkeypatch.delenv("EMBEDDING_SCREENING_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("FOLDSEEK_ENDPOINT_NAME", raising=False)
    interceptor._lambda_client.responses["mmseqs-screening"] = {
        "min_evalue": 1e-1,
        "count": 1,
    }
    body = _tools_call_body({"query": VALID_SEQUENCE})
    result = interceptor.lambda_handler(_event(body), None)
    transformed_args = result["mcp"]["transformedGatewayRequest"]["body"]["params"][
        "arguments"
    ]
    assert transformed_args["_biosafety_embedding_risk_score"] == 0
    assert transformed_args["_biosafety_foldseek_risk_score"] == 0
