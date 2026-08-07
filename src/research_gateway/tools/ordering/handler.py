# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""" Lambda handler for the ordering-tool AgentCore Gateway target.

Returns a mock order id
"""

from typing import Any, Dict
import random


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # noqa: ARG001
    inputs = event.get("inputs")
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        payload = {"status": "error", "message": "'inputs' must be a list of strings."}
        return {
            "content": [{"type": "text", "text": f"Error: {payload['message']}"}],
            "structuredContent": payload,
            "isError": True,
        }
    
    order_id = random.randint(10**9, 10**10 - 1)
    message = f"Order {order_id} received."
    result = {"status": "success", "message": message}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": result,
        "isError": False,
    }
