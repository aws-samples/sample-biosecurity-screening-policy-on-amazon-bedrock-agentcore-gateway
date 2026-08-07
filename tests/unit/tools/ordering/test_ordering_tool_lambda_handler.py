# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ordering_tool/lambda_handler.py — the placeholder ordering-tool
gateway target."""

from types import SimpleNamespace

from research_gateway.tools.ordering import handler as lambda_handler


def test_handler_success_returns_order_id_message():
    result = lambda_handler.handler({"inputs": ["widget-a", "widget-b"]}, SimpleNamespace())
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "success"
    assert result["content"][0]["text"].startswith("Order ")
    assert result["content"][0]["text"].endswith(" received.")


def test_handler_order_id_is_a_ten_digit_number():
    result = lambda_handler.handler({"inputs": ["widget"]}, SimpleNamespace())
    order_id_str = result["content"][0]["text"].split()[1]
    assert order_id_str.isdigit()
    assert len(order_id_str) == 10


def test_handler_rejects_missing_inputs():
    result = lambda_handler.handler({}, SimpleNamespace())
    assert result["isError"] is True
    assert "'inputs' must be a list of strings" in result["structuredContent"]["message"]


def test_handler_rejects_non_list_inputs():
    result = lambda_handler.handler({"inputs": "widget"}, SimpleNamespace())
    assert result["isError"] is True


def test_handler_rejects_list_with_non_string_items():
    result = lambda_handler.handler({"inputs": ["widget", 123]}, SimpleNamespace())
    assert result["isError"] is True


def test_handler_accepts_empty_inputs_list():
    result = lambda_handler.handler({"inputs": []}, SimpleNamespace())
    assert result["isError"] is False
