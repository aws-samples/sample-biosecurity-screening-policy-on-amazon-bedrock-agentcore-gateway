# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for uniprot/lambda_handler.py — the search_uniprot Lambda entrypoint."""

from types import SimpleNamespace

from research_gateway.tools.uniprot import handler as lambda_handler


def _context_with_tool_name(name):
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": name})
    )


# ---------------------------------------------------------------------------
# _extract_tool_name
# ---------------------------------------------------------------------------


def test_extract_tool_name_strips_gateway_prefix():
    context = _context_with_tool_name("uniprot-search___search_uniprot")
    assert lambda_handler._extract_tool_name(context) == "search_uniprot"


def test_extract_tool_name_no_client_context_returns_empty_string():
    assert lambda_handler._extract_tool_name(SimpleNamespace()) == ""


# ---------------------------------------------------------------------------
# _format_summary
# ---------------------------------------------------------------------------


def test_format_summary_no_proteins():
    result = {"query": "obscure protein", "proteins": []}
    summary = lambda_handler._format_summary(result)
    assert 'No UniProt proteins found for query "obscure protein"' in summary


def test_format_summary_never_mentions_total_found():
    # total_found is deliberately omitted from the text summary because a
    # plain-text query expansion inflates UniProt's match count into the
    # millions -- see the docstring in uniprot/lambda_handler.py.
    result = {
        "query": "insulin",
        "total_found": 45_000_000,
        "returned": 1,
        "proteins": [{"protein_name": "Insulin", "accession": "P01308"}],
    }
    summary = lambda_handler._format_summary(result)
    assert "45000000" not in summary
    assert "45,000,000" not in summary


def test_format_summary_includes_gene_length_and_reviewed_status():
    result = {
        "query": "insulin",
        "returned": 1,
        "proteins": [
            {
                "protein_name": "Insulin",
                "organism": "Homo sapiens",
                "accession": "P01308",
                "gene_names": ["INS"],
                "length": 110,
                "reviewed": True,
                "function": "Regulates glucose.",
                "subcellular_locations": ["Secreted"],
                "pdb_ids": ["1ABC", "2DEF"],
            }
        ],
    }
    summary = lambda_handler._format_summary(result)
    assert "Insulin (Homo sapiens)" in summary
    assert "Gene: INS" in summary
    assert "Length: 110 aa" in summary
    assert "reviewed" in summary
    assert "Location: Secreted" in summary
    assert "Structures: 1ABC, 2DEF" in summary


def test_format_summary_truncates_long_function_text():
    long_function = "x" * 300
    result = {
        "query": "q",
        "returned": 1,
        "proteins": [{"protein_name": "P", "accession": "A1", "function": long_function}],
    }
    summary = lambda_handler._format_summary(result)
    assert "..." in summary
    assert long_function not in summary


# ---------------------------------------------------------------------------
# handler()
# ---------------------------------------------------------------------------


def test_handler_rejects_non_dict_event():
    result = lambda_handler.handler([], SimpleNamespace())
    assert result["isError"] is True


def test_handler_rejects_missing_query():
    result = lambda_handler.handler({}, SimpleNamespace())
    assert result["isError"] is True
    assert "'query' is required" in result["structuredContent"]["message"]


def test_handler_rejects_non_string_species():
    result = lambda_handler.handler(
        {"query": "insulin", "species": 123}, SimpleNamespace()
    )
    assert result["isError"] is True
    assert "'species' must be a string" in result["structuredContent"]["message"]


def test_handler_rejects_mismatched_tool_name():
    context = _context_with_tool_name("uniprot-search___some_other_tool")
    result = lambda_handler.handler({"query": "insulin"}, context)
    assert result["isError"] is True


def test_handler_returns_error_when_search_fails(monkeypatch):
    monkeypatch.setattr(
        lambda_handler,
        "search_uniprot_structured",
        lambda **kwargs: {"status": "error", "message": "upstream failure"},
    )
    result = lambda_handler.handler({"query": "insulin"}, SimpleNamespace())
    assert result["isError"] is True
    assert result["structuredContent"]["message"] == "upstream failure"


def test_handler_success_envelope(monkeypatch):
    fake_result = {
        "status": "success",
        "query": "insulin",
        "proteins": [{"protein_name": "Insulin", "accession": "P01308"}],
        "returned": 1,
    }
    monkeypatch.setattr(
        lambda_handler, "search_uniprot_structured", lambda **kwargs: fake_result
    )
    result = lambda_handler.handler({"query": "insulin"}, SimpleNamespace())
    assert result["isError"] is False
    assert result["structuredContent"] == fake_result
    assert "Insulin" in result["content"][0]["text"]


def test_handler_passes_species_through(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "proteins": []}

    monkeypatch.setattr(lambda_handler, "search_uniprot_structured", fake_search)
    lambda_handler.handler(
        {"query": "insulin", "species": "Homo sapiens"}, SimpleNamespace()
    )
    assert captured["species"] == "Homo sapiens"


def test_handler_defaults_species_to_none(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "proteins": []}

    monkeypatch.setattr(lambda_handler, "search_uniprot_structured", fake_search)
    lambda_handler.handler({"query": "insulin"}, SimpleNamespace())
    assert captured["species"] is None
