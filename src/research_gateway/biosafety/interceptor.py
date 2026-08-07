# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3

from research_gateway.biosafety.sequence_finder import extract_sequences

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_MAX_WORKERS = 10
_lambda_client = boto3.client("lambda")
_sagemaker_runtime = boto3.client("sagemaker-runtime")

# AgentCore prefixes tools/call params.name with "<target>___<tool>". Screening
# only runs for the ordering-tool target — see gateway_target_name in
# gateway_stack.py — since gateway interceptors cannot be scoped to a target at
# the infrastructure level; this check is the only place that scoping can happen.
TOOL_NAME_DELIMITER = "___"
ORDERING_TOOL_TARGET_NAME = "ordering-tool"


def _screen(sequence: str) -> dict:
    function_name = os.environ["SCREENING_FUNCTION_NAME"]
    response = _lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"sequence": sequence}),
    )
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(f"Screening Lambda error: {payload}")
    return payload


def _screen_embedding(sequence: str) -> dict | None:
    function_name = os.environ.get("EMBEDDING_SCREENING_FUNCTION_NAME")
    if not function_name:
        logger.debug("EMBEDDING_SCREENING_FUNCTION_NAME not set, skipping embedding screening")
        return None
    response = _lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"sequence": sequence}),
    )
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(f"Embedding screening Lambda error: {payload}")
    return payload


def _screen_foldseek(sequence: str) -> dict | None:
    endpoint_name = os.environ.get("FOLDSEEK_ENDPOINT_NAME")
    if not endpoint_name:
        logger.debug("FOLDSEEK_ENDPOINT_NAME not set, skipping foldseek screening")
        return None
    response = _sagemaker_runtime.invoke_endpoint(
        EndpointName=endpoint_name,
        ContentType="application/json",
        Body=json.dumps({"sequence": sequence}),
    )
    return json.loads(response["Body"].read())


def _error_response(message: str) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "error": {
                "code": -32000,
                "message": message,
            }
        },
    }


def lambda_handler(event, _context):
    gateway_request = event["mcp"]["gatewayRequest"]
    headers = gateway_request["headers"]
    body = gateway_request["body"]
    method = body.get("method")

    logger.info("Interceptor invoked: method=%s", method)

    # Only screen tools/call requests — pass everything else through unmodified.
    if method != "tools/call":
        logger.debug("Non-tools/call request (%s), passing through unmodified", method)
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "headers": headers,
                    "body": body,
                }
            },
        }

    tool_name = body.get("params", {}).get("name") or ""
    target_name = tool_name.split(TOOL_NAME_DELIMITER, 1)[0]

    if target_name != ORDERING_TOOL_TARGET_NAME:
        logger.debug(
            "Tool call not targeting %s (tool=%s), passing through unmodified",
            ORDERING_TOOL_TARGET_NAME,
            tool_name,
        )
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "headers": headers,
                    "body": body,
                }
            },
        }

    arguments = body.get("params", {}).get("arguments", {})
    sequences = extract_sequences(arguments)

    logger.info(
        "Screening tool call: tool=%s sequences_found=%d", tool_name, len(sequences)
    )

    if not sequences:
        logger.debug(
            "No sequences detected in tool=%s, passing through unmodified", tool_name
        )
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "headers": headers,
                    "body": body,
                }
            },
        }

    # ── Parallel screening (MMseqs2 + Embedding + Foldseek) ─────────────────

    mmseqs_results = {}
    similarities = []
    foldseek_evalues = []

    def _do_mmseqs(seq: str, key: str) -> tuple:
        return ("mmseqs", key, _screen(seq))

    def _do_embedding(seq: str, key: str) -> tuple:
        return ("embedding", key, _screen_embedding(seq))

    def _do_foldseek(seq: str, key: str) -> tuple:
        return ("foldseek", key, _screen_foldseek(seq))

    futures = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        for seq in sequences:
            key = seq[:40] + ("..." if len(seq) > 40 else "")
            futures[executor.submit(_do_mmseqs, seq, key)] = ("mmseqs", key)
            futures[executor.submit(_do_embedding, seq, key)] = ("embedding", key)
            futures[executor.submit(_do_foldseek, seq, key)] = ("foldseek", key)

        for future in as_completed(futures):
            screening_type, key = futures[future]
            try:
                result_type, result_key, result = future.result()
                if result_type == "mmseqs":
                    mmseqs_results[result_key] = {
                        "min_evalue": result.get("min_evalue"),
                        "hit_count": result.get("count", 0),
                    }
                    logger.debug(
                        "MMseqs2 screened: key=%s min_evalue=%s hit_count=%d",
                        result_key,
                        result.get("min_evalue"),
                        result.get("count", 0),
                    )
                elif result_type == "embedding":
                    if result is not None:
                        sim = result.get("max_similarity")
                        if sim is not None:
                            similarities.append(sim)
                            logger.debug(
                                "Embedding screened: key=%s max_similarity=%.4f", result_key, sim
                            )
                else:
                    if result is not None:
                        ev = result.get("min_evalue")
                        if ev is not None:
                            foldseek_evalues.append(ev)
                            logger.debug(
                                "Foldseek screened: key=%s min_evalue=%s hit_count=%d",
                                result_key,
                                ev,
                                result.get("count", 0),
                            )
            except Exception as exc:
                logger.error("%s screening failed: key=%s error=%s", screening_type, key, exc)
                return _error_response(f"{screening_type.title()} screening failed: {exc}")

    evalues = [r["min_evalue"] for r in mmseqs_results.values() if r["min_evalue"] is not None]
    max_risk_evalue = min(evalues) if evalues else None

    if max_risk_evalue is not None and max_risk_evalue > 0:
        mmseqs_risk_score = min(int(-math.log10(max_risk_evalue)), 999)
    else:
        mmseqs_risk_score = 0

    max_similarity = max(similarities) if similarities else 0.0
    embedding_risk_score = int(max_similarity * 100)

    min_foldseek_evalue = min(foldseek_evalues) if foldseek_evalues else None
    if min_foldseek_evalue is not None and min_foldseek_evalue > 0:
        foldseek_risk_score = min(int(-math.log10(min_foldseek_evalue)), 999)
    else:
        foldseek_risk_score = 0

    logger.info(
        "Screening complete: tool=%s sequences_screened=%d "
        "mmseqs_risk=%d embedding_risk=%d foldseek_risk=%d",
        tool_name,
        len(sequences),
        mmseqs_risk_score,
        embedding_risk_score,
        foldseek_risk_score,
    )

    transformed_body = copy.deepcopy(body)
    params = transformed_body.setdefault("params", {})
    args = params.setdefault("arguments", {})
    # Inject as flat top-level keys — AgentCore serializes nested dicts as JSON
    # strings in Cedar context, making attribute access fail with "got string".
    args["_biosafety_mmseqs_risk_score"] = mmseqs_risk_score
    args["_biosafety_embedding_risk_score"] = embedding_risk_score
    args["_biosafety_foldseek_risk_score"] = foldseek_risk_score
    args["_biosafety_sequences_found"] = len(sequences)
    args["_biosafety_screened_at"] = datetime.now(timezone.utc).isoformat()
    args["_biosafety_embedding_max_similarity"] = max_similarity

    logger.debug(transformed_body)
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": headers,
                "body": transformed_body,
            }
        },
    }
