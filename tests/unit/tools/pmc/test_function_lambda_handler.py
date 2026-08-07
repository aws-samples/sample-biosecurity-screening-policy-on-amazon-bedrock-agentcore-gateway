# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for function/lambda_handler.py — the search_pmc Lambda entrypoint:
tool-name extraction, input validation, and the MCP response envelope."""

from types import SimpleNamespace

from research_gateway.tools.pmc import handler as lambda_handler


def _context_with_tool_name(name):
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": name})
    )


# ---------------------------------------------------------------------------
# _extract_tool_name
# ---------------------------------------------------------------------------


def test_extract_tool_name_strips_gateway_prefix():
    context = _context_with_tool_name("pmc-search___search_pmc")
    assert lambda_handler._extract_tool_name(context) == "search_pmc"


def test_extract_tool_name_no_delimiter_returns_raw_value():
    context = _context_with_tool_name("search_pmc")
    assert lambda_handler._extract_tool_name(context) == "search_pmc"


def test_extract_tool_name_no_client_context_returns_empty_string():
    assert lambda_handler._extract_tool_name(SimpleNamespace()) == ""


def test_extract_tool_name_no_custom_returns_empty_string():
    context = SimpleNamespace(client_context=SimpleNamespace(custom=None))
    assert lambda_handler._extract_tool_name(context) == ""


def test_extract_tool_name_custom_not_a_dict_returns_empty_string():
    context = SimpleNamespace(client_context=SimpleNamespace(custom="not-a-dict"))
    assert lambda_handler._extract_tool_name(context) == ""


# ---------------------------------------------------------------------------
# _format_summary
# ---------------------------------------------------------------------------


def test_format_summary_no_articles():
    result = {"query": "obscure topic", "articles": []}
    assert 'No PMC articles found for query "obscure topic"' in lambda_handler._format_summary(
        result
    )


def test_format_summary_lists_ranked_by_references():
    result = {
        "query": "q",
        "total_found": 1,
        "returned": 1,
        "ranked_by": "references",
        "articles": [
            {
                "title": "T",
                "authors": ["A", "B"],
                "journal": "J",
                "year": 2020,
                "pmc_id": "PMC1",
                "referenced_by_count": 4,
            }
        ],
    }
    summary = lambda_handler._format_summary(result)
    assert "Ranked by citation count within this result set." in summary
    assert "1. T (J, 2020)" in summary
    assert "PMC1 · cited by 4" in summary


def test_format_summary_truncates_author_list_over_three():
    result = {
        "query": "q",
        "total_found": 1,
        "returned": 1,
        "articles": [{"title": "T", "authors": ["A", "B", "C", "D"]}],
    }
    summary = lambda_handler._format_summary(result)
    assert "A, B, C, et al." in summary


def test_format_summary_notes_additional_articles_beyond_max():
    articles = [{"title": f"Article {i}"} for i in range(7)]
    result = {"query": "q", "total_found": 7, "returned": 7, "articles": articles}
    summary = lambda_handler._format_summary(result)
    assert "... plus 2 more in structuredContent." in summary


# ---------------------------------------------------------------------------
# handler()
# ---------------------------------------------------------------------------


def test_handler_rejects_non_dict_event():
    result = lambda_handler.handler("not a dict", SimpleNamespace())
    assert result["isError"] is True
    assert "JSON object" in result["structuredContent"]["message"]


def test_handler_rejects_missing_query():
    result = lambda_handler.handler({}, SimpleNamespace())
    assert result["isError"] is True
    assert "'query' is required" in result["structuredContent"]["message"]


def test_handler_rejects_non_string_query():
    result = lambda_handler.handler({"query": 123}, SimpleNamespace())
    assert result["isError"] is True


def test_handler_rejects_invalid_rerank_by():
    result = lambda_handler.handler(
        {"query": "q", "rerank_by": "citations"}, SimpleNamespace()
    )
    assert result["isError"] is True
    assert "rerank_by" in result["structuredContent"]["message"]


def test_handler_rejects_mismatched_tool_name():
    context = _context_with_tool_name("pmc-search___some_other_tool")
    result = lambda_handler.handler({"query": "q"}, context)
    assert result["isError"] is True
    assert "Unknown tool" in result["structuredContent"]["message"]


def test_handler_returns_error_when_search_fails(monkeypatch):
    monkeypatch.setattr(
        lambda_handler,
        "search_pmc_structured",
        lambda **kwargs: {"status": "error", "message": "upstream failure"},
    )
    result = lambda_handler.handler({"query": "q"}, SimpleNamespace())
    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "upstream failure"


def test_handler_success_envelope(monkeypatch):
    fake_result = {
        "status": "success",
        "query": "q",
        "total_found": 1,
        "returned": 1,
        "ranked_by": "references",
        "articles": [{"title": "T"}],
    }
    monkeypatch.setattr(
        lambda_handler, "search_pmc_structured", lambda **kwargs: fake_result
    )
    result = lambda_handler.handler({"query": "q"}, SimpleNamespace())
    assert result["isError"] is False
    assert result["structuredContent"] == fake_result
    assert result["content"][0]["type"] == "text"
    assert "T" in result["content"][0]["text"]


def test_handler_defaults_rerank_by_to_references(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "articles": []}

    monkeypatch.setattr(lambda_handler, "search_pmc_structured", fake_search)
    lambda_handler.handler({"query": "q"}, SimpleNamespace())
    assert captured["rerank_by"] == "references"


def test_handler_passes_rerank_by_none_through(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "articles": []}

    monkeypatch.setattr(lambda_handler, "search_pmc_structured", fake_search)
    lambda_handler.handler({"query": "q", "rerank_by": None}, SimpleNamespace())
    assert captured["rerank_by"] is None
