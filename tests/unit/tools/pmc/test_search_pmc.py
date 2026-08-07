# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for research_gateway.tools.pmc.search — NCBI PMC search, XML parsing,
citation reranking, and the structured result shape consumed by the Lambda
handler."""

import httpx
import pytest
from defusedxml import ElementTree as ET

from research_gateway.tools.pmc import search as search_pmc

SAMPLE_ARTICLE_XML = """
<pmc-articleset>
  <article>
    <front>
      <article-meta>
        <article-id pub-id-type="pmcid">PMC12345</article-id>
        <article-id pub-id-type="pmid">98765</article-id>
        <article-id pub-id-type="doi">10.1234/example</article-id>
        <title-group>
          <article-title>A Study of <italic>Widgets</italic></article-title>
        </title-group>
        <contrib-group>
          <contrib contrib-type="author">
            <name><surname>Smith</surname><given-names>Jane</given-names></name>
          </contrib>
          <contrib contrib-type="author">
            <name><surname>Doe</surname></name>
          </contrib>
        </contrib-group>
        <pub-date pub-type="epub"><year>2021</year></pub-date>
        <abstract>
          <p>First paragraph.</p>
          <p>Second paragraph.</p>
        </abstract>
      </article-meta>
      <journal-meta>
        <journal-title>Journal of Widgets</journal-title>
      </journal-meta>
    </front>
    <back>
      <ref-list>
        <ref><mixed-citation><pub-id pub-id-type="pmid">11111</pub-id></mixed-citation></ref>
        <ref><mixed-citation><pub-id pub-id-type="pmid">not-a-pmid</pub-id></mixed-citation></ref>
      </ref-list>
    </back>
  </article>
</pmc-articleset>
"""


# ---------------------------------------------------------------------------
# _build_search_query / _add_quotes_to_search_filter
# ---------------------------------------------------------------------------


def test_add_quotes_to_search_filter_quotes_bracketed_clause():
    query = 'mRNA vaccine AND last 2 years[dp]'
    assert search_pmc._add_quotes_to_search_filter(query) == (
        'mRNA vaccine AND "last 2 years"[dp]'
    )


def test_add_quotes_to_search_filter_leaves_query_without_and_clause_untouched():
    query = "CRISPR gene editing"
    assert search_pmc._add_quotes_to_search_filter(query) == query


def test_build_search_query_commercial_use_only_true(monkeypatch):
    monkeypatch.setattr(search_pmc, "COMMERCIAL_USE_ONLY", True)
    result = search_pmc._build_search_query("insulin")
    assert result == (
        "insulin AND (cc0 license[Filter] OR cc by license[Filter] "
        "OR cc by-sa license[Filter] OR cc by-nd license[Filter])"
    )


def test_build_search_query_commercial_use_only_false(monkeypatch):
    monkeypatch.setattr(search_pmc, "COMMERCIAL_USE_ONLY", False)
    result = search_pmc._build_search_query("insulin")
    assert result == "insulin AND cc license[Filter]"


def test_commercial_use_only_env_var_string_false_is_truthy(monkeypatch):
    """Regression test for the documented gotcha: COMMERCIAL_USE_ONLY is read
    from an env var, so the string "false" is truthy and still applies the
    stricter commercial-use filter."""

    import importlib

    monkeypatch.setenv("COMMERCIAL_USE_ONLY", "false")
    reloaded = importlib.reload(search_pmc)
    try:
        assert reloaded.COMMERCIAL_USE_ONLY == "false"
        assert bool(reloaded.COMMERCIAL_USE_ONLY) is True
    finally:
        monkeypatch.delenv("COMMERCIAL_USE_ONLY", raising=False)
        importlib.reload(search_pmc)


# ---------------------------------------------------------------------------
# _get_api_key_params
# ---------------------------------------------------------------------------


def test_get_api_key_params_adds_key_when_set(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "secret-key")
    params = search_pmc._get_api_key_params({"db": "pmc"})
    assert params == {"db": "pmc", "api_key": "secret-key"}


def test_get_api_key_params_omits_key_when_unset(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    params = search_pmc._get_api_key_params({"db": "pmc"})
    assert params == {"db": "pmc"}


def test_get_api_key_params_omits_key_when_blank(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "   ")
    params = search_pmc._get_api_key_params({"db": "pmc"})
    assert params == {"db": "pmc"}


def test_get_api_key_params_does_not_mutate_input(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "secret-key")
    original = {"db": "pmc"}
    search_pmc._get_api_key_params(original)
    assert original == {"db": "pmc"}


# ---------------------------------------------------------------------------
# _extract_article_data (XML parsing)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_article_element():
    root = ET.fromstring(SAMPLE_ARTICLE_XML)
    return root.find(".//article")


def test_extract_article_data_ids(sample_article_element):
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["id"] == "12345"
    assert article["pmc"] == "PMC12345"
    assert article["pmid"] == "98765"
    assert article["doi"] == "10.1234/example"
    assert article["uri"] == "https://doi.org/10.1234/example"


def test_extract_article_data_title_strips_nested_markup(sample_article_element):
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["title"] == "A Study of Widgets"


def test_extract_article_data_abstract_joins_paragraphs(sample_article_element):
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["abstract"] == "First paragraph. Second paragraph."
    assert article["text"] == article["abstract"]


def test_extract_article_data_authors_handles_missing_given_names(sample_article_element):
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["authors"] == "Jane Smith, Doe"


def test_extract_article_data_journal_and_year(sample_article_element):
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["journal"] == "Journal of Widgets"
    assert article["year"] == "2021"


def test_extract_article_data_references_reads_pmid_ref_ids(sample_article_element):
    # _extract_article_data collects every <ref> with a pmid, numeric or not;
    # filtering out non-numeric PMIDs happens later, in
    # _calculate_referenced_by_counts.
    article = search_pmc._extract_article_data(sample_article_element)
    assert article["references"] == ["11111", "not-a-pmid"]


def test_extract_article_data_year_fallback_order():
    xml = """
    <article>
      <front><article-meta>
        <pub-date pub-type="ppub"><year>1999</year></pub-date>
      </article-meta></front>
    </article>
    """
    element = ET.fromstring(xml)
    article = search_pmc._extract_article_data(element)
    assert article["year"] == "1999"


def test_extract_article_data_empty_element_returns_empty_dict():
    element = ET.fromstring("<article></article>")
    assert search_pmc._extract_article_data(element) == {}


# ---------------------------------------------------------------------------
# _calculate_referenced_by_counts / _rank_by_citations
# ---------------------------------------------------------------------------


def test_calculate_referenced_by_counts_builds_citation_graph():
    articles = [
        {"id": "1", "pmid": "100", "references": []},
        {"id": "2", "pmid": "200", "references": ["100"]},
        {"id": "3", "pmid": "300", "references": ["100", "200"]},
    ]
    enhanced = search_pmc._calculate_referenced_by_counts(articles)
    counts = {a["id"]: a["referenced_by_count"] for a in enhanced}
    assert counts == {"1": 2, "2": 1, "3": 0}


def test_calculate_referenced_by_counts_ignores_self_references():
    articles = [{"id": "1", "pmid": "100", "references": ["100"]}]
    enhanced = search_pmc._calculate_referenced_by_counts(articles)
    assert enhanced[0]["referenced_by_count"] == 0


def test_calculate_referenced_by_counts_ignores_non_numeric_and_unresolved_refs():
    articles = [
        {"id": "1", "pmid": "100", "references": ["not-a-pmid", "999", None, ""]},
    ]
    enhanced = search_pmc._calculate_referenced_by_counts(articles)
    assert enhanced[0]["referenced_by_count"] == 0


def test_calculate_referenced_by_counts_does_not_mutate_input():
    articles = [{"id": "1", "pmid": "100", "references": []}]
    search_pmc._calculate_referenced_by_counts(articles)
    assert "referenced_by_count" not in articles[0]


def test_rank_by_citations_sorts_descending_by_count_then_id():
    articles = [
        {"id": "1", "referenced_by_count": 0},
        {"id": "5", "referenced_by_count": 2},
        {"id": "3", "referenced_by_count": 2},
        {"id": "2", "referenced_by_count": 1},
    ]
    ranked = search_pmc._rank_by_citations(articles)
    assert [a["id"] for a in ranked] == ["5", "3", "2", "1"]


def test_rank_by_citations_handles_non_numeric_id():
    articles = [
        {"id": "abc", "referenced_by_count": 1},
        {"id": "2", "referenced_by_count": 1},
    ]
    # Should not raise despite the non-numeric id in the tie-break key.
    ranked = search_pmc._rank_by_citations(articles)
    assert {a["id"] for a in ranked} == {"abc", "2"}


# ---------------------------------------------------------------------------
# _clean_article_for_output
# ---------------------------------------------------------------------------


def test_clean_article_for_output_splits_authors_and_coerces_year():
    article = {
        "pmc": "PMC1",
        "pmid": "1",
        "doi": "10.1/x",
        "uri": "https://doi.org/10.1/x",
        "title": "Title",
        "authors": "Jane Smith, John Doe",
        "journal": "J",
        "year": "2020",
        "abstract": "Abstract text",
        "references": ["1", "2"],
        "referenced_by_count": 3,
    }
    cleaned = search_pmc._clean_article_for_output(article)
    assert cleaned["authors"] == ["Jane Smith", "John Doe"]
    assert cleaned["year"] == 2020
    assert isinstance(cleaned["year"], int)
    assert cleaned["reference_count"] == 2
    assert cleaned["referenced_by_count"] == 3
    assert cleaned["pmc_id"] == "PMC1"


def test_clean_article_for_output_handles_missing_fields():
    cleaned = search_pmc._clean_article_for_output({})
    assert cleaned["authors"] == []
    assert cleaned["year"] is None
    assert cleaned["reference_count"] == 0
    assert cleaned["referenced_by_count"] == 0


def test_clean_article_for_output_leaves_non_numeric_year_as_string():
    cleaned = search_pmc._clean_article_for_output({"year": "circa 1990"})
    assert cleaned["year"] == "circa 1990"


# ---------------------------------------------------------------------------
# search_pmc_structured (network calls mocked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, json_data=None, text="", status_code=200, headers=None):
        self._json_data = json_data
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._json_data


def test_search_pmc_structured_no_results(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse({"esearchresult": {"idlist": []}}),
    )
    result = search_pmc.search_pmc_structured(query="nonexistent topic")
    assert result == {
        "status": "success",
        "query": "nonexistent topic",
        "total_found": 0,
        "returned": 0,
        "ranked_by": "references",
        "articles": [],
    }


def test_search_pmc_structured_success_with_reranking(monkeypatch):
    responses = [
        _FakeResponse({"esearchresult": {"idlist": ["12345"]}}),
        _FakeResponse(text=SAMPLE_ARTICLE_XML),
    ]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = search_pmc.search_pmc_structured(query="widgets")
    assert result["status"] == "success"
    assert result["total_found"] == 1
    assert result["returned"] == 1
    assert result["ranked_by"] == "references"
    assert result["articles"][0]["pmc_id"] == "PMC12345"
    assert result["articles"][0]["title"] == "A Study of Widgets"


def test_search_pmc_structured_rerank_by_none_skips_ranking(monkeypatch):
    responses = [
        _FakeResponse({"esearchresult": {"idlist": ["12345"]}}),
        _FakeResponse(text=SAMPLE_ARTICLE_XML),
    ]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: responses.pop(0))

    result = search_pmc.search_pmc_structured(query="widgets", rerank_by=None)
    assert result["ranked_by"] is None
    assert result["articles"][0]["referenced_by_count"] == 0


def test_search_pmc_structured_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(status_code=500))
    result = search_pmc.search_pmc_structured(query="widgets")
    assert result["status"] == "error"
    assert "500" in result["message"]


def test_search_pmc_structured_request_error(monkeypatch):
    def raise_request_error(*args, **kwargs):
        raise httpx.RequestError("boom")

    monkeypatch.setattr(httpx, "post", raise_request_error)
    result = search_pmc.search_pmc_structured(query="widgets")
    assert result["status"] == "error"
    assert "Request error" in result["message"]


def test_search_pmc_structured_malformed_response_missing_key(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse({"unexpected": {}}))
    result = search_pmc.search_pmc_structured(query="widgets")
    assert result["status"] == "error"
    assert "esearchresult" in result["message"]


def test_search_pmc_structured_respects_max_filtered_result_count(monkeypatch):
    ids = [str(i) for i in range(5)]
    xml_articles = "".join(
        f"""
        <article>
          <front><article-meta>
            <article-id pub-id-type="pmcid">PMC{i}</article-id>
          </article-meta></front>
        </article>
        """
        for i in ids
    )
    responses = [
        _FakeResponse({"esearchresult": {"idlist": ids}}),
        _FakeResponse(text=f"<pmc-articleset>{xml_articles}</pmc-articleset>"),
    ]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: responses.pop(0))

    result = search_pmc.search_pmc_structured(
        query="widgets", rerank_by=None, max_filtered_result_count=2
    )
    assert result["total_found"] == 5
    assert result["returned"] == 2


def test_fetch_pmc_rejects_dtd_entity_expansion(monkeypatch):
    malicious_xml = """<!DOCTYPE article [<!ENTITY payload 'blocked'>]>
    <pmc-articleset><article><title>&payload;</title></article></pmc-articleset>"""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(text=malicious_xml))

    with pytest.raises(Exception, match="Unsafe XML response"):
        search_pmc.fetch_pmc(["PMC12345"])
