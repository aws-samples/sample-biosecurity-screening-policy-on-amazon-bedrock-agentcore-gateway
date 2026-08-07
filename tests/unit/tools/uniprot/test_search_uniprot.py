# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for research_gateway.tools.uniprot.search — query building, species
filtering, UniProt REST response flattening, and the structured search flow."""

import httpx
import pytest

from research_gateway.tools.uniprot import search as search_uniprot


# ---------------------------------------------------------------------------
# _escape_term
# ---------------------------------------------------------------------------


def test_escape_term_escapes_quotes_and_backslashes():
    assert search_uniprot._escape_term('a"b\\c') == 'a\\"b\\\\c'


def test_escape_term_leaves_plain_text_untouched():
    assert search_uniprot._escape_term("insulin") == "insulin"


# ---------------------------------------------------------------------------
# _build_search_query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        'insulin AND organism_name:"Homo sapiens"',
        "spike AND reviewed:true",
        "gene:INS",
    ],
)
def test_build_search_query_passes_through_field_syntax_unmodified(query):
    assert search_uniprot._build_search_query(query) == query


def test_build_search_query_single_term_expands_across_fields():
    result = search_uniprot._build_search_query("insulin")
    assert result == (
        '(protein_name:"insulin" OR gene:"insulin" OR cc_function:"insulin" '
        'OR cc_disease:"insulin" OR keyword:"insulin")'
    )


def test_build_search_query_multi_term_includes_conjunctive_full_text_clause():
    result = search_uniprot._build_search_query("SARS-CoV-2 spike")
    assert '("SARS-CoV-2" AND "spike")' in result
    assert 'protein_name:"SARS-CoV-2 spike"' in result
    assert 'cc_function:"SARS-CoV-2 spike"' in result
    assert 'cc_disease:"SARS-CoV-2 spike"' in result
    assert 'gene:"spike"' in result
    assert 'keyword:"SARS-CoV-2"' in result


def test_build_search_query_escapes_quotes_within_terms():
    result = search_uniprot._build_search_query('foo"bar')
    assert '\\"' in result


def test_build_search_query_empty_string_returns_empty():
    assert search_uniprot._build_search_query("   ") == ""


# ---------------------------------------------------------------------------
# _resolve_species_filter
# ---------------------------------------------------------------------------


def test_resolve_species_filter_builds_organism_name_clause():
    assert search_uniprot._resolve_species_filter("Homo sapiens") == (
        'organism_name:"Homo sapiens"'
    )


def test_resolve_species_filter_strips_and_escapes():
    assert search_uniprot._resolve_species_filter('  Homo "sapiens"  ') == (
        'organism_name:"Homo \\"sapiens\\""'
    )


# ---------------------------------------------------------------------------
# _extract_protein_name / comment / feature helpers
# ---------------------------------------------------------------------------


def test_extract_protein_name_prefers_recommended_name():
    entry = {
        "proteinDescription": {
            "recommendedName": {"fullName": {"value": "Insulin"}},
            "submissionNames": [{"fullName": {"value": "Should not be used"}}],
        }
    }
    assert search_uniprot._extract_protein_name(entry) == "Insulin"


def test_extract_protein_name_falls_back_to_submission_names():
    entry = {
        "proteinDescription": {
            "submissionNames": [{"fullName": {"value": "Unreviewed Protein"}}]
        }
    }
    assert search_uniprot._extract_protein_name(entry) == "Unreviewed Protein"


def test_extract_protein_name_returns_none_when_absent():
    assert search_uniprot._extract_protein_name({"proteinDescription": {}}) is None


def test_extract_comment_texts_filters_by_type():
    entry = {
        "comments": [
            {"commentType": "FUNCTION", "texts": [{"value": "Does a thing."}]},
            {"commentType": "DISEASE", "texts": [{"value": "Irrelevant."}]},
        ]
    }
    assert search_uniprot._extract_comment_texts(entry, "FUNCTION") == ["Does a thing."]


def test_extract_subcellular_locations_uses_space_separated_comment_type():
    entry = {
        "comments": [
            {
                "commentType": "SUBCELLULAR LOCATION",
                "subcellularLocations": [
                    {"location": {"value": "Cell membrane"}},
                    {"location": {"value": "Cell membrane"}},  # duplicate
                    {"location": {"value": "Nucleus"}},
                ],
            }
        ]
    }
    assert search_uniprot._extract_subcellular_locations(entry) == [
        "Cell membrane",
        "Nucleus",
    ]


def test_extract_subcellular_locations_ignores_underscore_variant():
    entry = {
        "comments": [
            {
                "commentType": "SUBCELLULAR_LOCATION",
                "subcellularLocations": [{"location": {"value": "Cell membrane"}}],
            }
        ]
    }
    assert search_uniprot._extract_subcellular_locations(entry) == []


def test_extract_diseases_returns_id_acronym_description():
    entry = {
        "comments": [
            {
                "commentType": "DISEASE",
                "disease": {"diseaseId": "D1", "acronym": "D1A"},
                "texts": [{"value": "A description."}],
            }
        ]
    }
    assert search_uniprot._extract_diseases(entry) == [
        {"disease_id": "D1", "acronym": "D1A", "description": "A description."}
    ]


def test_extract_features_maps_location_and_description():
    entry = {
        "features": [
            {
                "type": "Domain",
                "location": {"start": {"value": 1}, "end": {"value": 10}},
                "description": "Kinase domain",
            }
        ]
    }
    assert search_uniprot._extract_features(entry) == [
        {"type": "Domain", "start": 1, "end": 10, "description": "Kinase domain"}
    ]


# ---------------------------------------------------------------------------
# _clean_entry
# ---------------------------------------------------------------------------


def _minimal_entry(entry_type):
    return {
        "primaryAccession": "P12345",
        "uniProtkbId": "P12345_HUMAN",
        "proteinDescription": {"recommendedName": {"fullName": {"value": "Test Protein"}}},
        "genes": [{"geneName": {"value": "TST"}}],
        "organism": {"scientificName": "Homo sapiens"},
        "sequence": {"length": 100, "value": "M" * 100},
        "entryType": entry_type,
        "comments": [],
        "features": [],
        "uniProtKBCrossReferences": [
            {"database": "PDB", "id": "1ABC"},
            {"database": "RefSeq", "id": "NP_000000"},
        ],
    }


def test_clean_entry_reviewed_swissprot():
    entry = _minimal_entry("UniProtKB reviewed (Swiss-Prot)")
    cleaned = search_uniprot._clean_entry(entry)
    assert cleaned["reviewed"] is True


def test_clean_entry_unreviewed_trembl_is_not_reviewed():
    # "reviewed" is a substring of "unreviewed" -- this exercises the
    # "unreviewed" not in ... guard that keeps that substring from flipping
    # the result to True.
    entry = _minimal_entry("UniProtKB unreviewed (TrEMBL)")
    cleaned = search_uniprot._clean_entry(entry)
    assert cleaned["reviewed"] is False


def test_clean_entry_extracts_pdb_ids_only():
    entry = _minimal_entry("UniProtKB reviewed (Swiss-Prot)")
    cleaned = search_uniprot._clean_entry(entry)
    assert cleaned["pdb_ids"] == ["1ABC"]


def test_clean_entry_builds_url_from_accession():
    entry = _minimal_entry("UniProtKB reviewed (Swiss-Prot)")
    cleaned = search_uniprot._clean_entry(entry)
    assert cleaned["url"] == "https://www.uniprot.org/uniprotkb/P12345/entry"


def test_clean_entry_missing_accession_gives_none_url():
    entry = _minimal_entry("UniProtKB reviewed (Swiss-Prot)")
    entry["primaryAccession"] = None
    cleaned = search_uniprot._clean_entry(entry)
    assert cleaned["url"] is None


# ---------------------------------------------------------------------------
# _search_accessions / _fetch_entry / _fetch_entries
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, json_data=None, headers=None, status_code=200):
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, *args, **kwargs):
        return self._responses.pop(0)


def test_search_accessions_uses_total_header_when_present():
    client = _FakeClient(
        [
            _FakeHttpResponse(
                {"results": [{"primaryAccession": "P1"}, {"primaryAccession": "P2"}]},
                headers={"x-total-results": "500"},
            )
        ]
    )
    accessions, total = search_uniprot._search_accessions(client, "insulin", 10)
    assert accessions == ["P1", "P2"]
    assert total == 500


def test_search_accessions_falls_back_to_result_count_when_header_missing():
    client = _FakeClient(
        [_FakeHttpResponse({"results": [{"primaryAccession": "P1"}]})]
    )
    accessions, total = search_uniprot._search_accessions(client, "insulin", 10)
    assert total == 1


def test_search_accessions_falls_back_when_header_is_not_numeric():
    client = _FakeClient(
        [
            _FakeHttpResponse(
                {"results": [{"primaryAccession": "P1"}]},
                headers={"x-total-results": "not-a-number"},
            )
        ]
    )
    accessions, total = search_uniprot._search_accessions(client, "insulin", 10)
    assert total == 1


def test_search_accessions_skips_results_without_accession():
    client = _FakeClient(
        [_FakeHttpResponse({"results": [{"primaryAccession": "P1"}, {}]})]
    )
    accessions, _ = search_uniprot._search_accessions(client, "insulin", 10)
    assert accessions == ["P1"]


def test_fetch_entry_returns_none_on_failure():
    class RaisingClient:
        def get(self, *args, **kwargs):
            raise httpx.RequestError("boom")

    assert search_uniprot._fetch_entry(RaisingClient(), "P1") is None


def test_fetch_entry_returns_json_on_success():
    class SuccessClient:
        def get(self, *args, **kwargs):
            return _FakeHttpResponse({"primaryAccession": "P1"})

    assert search_uniprot._fetch_entry(SuccessClient(), "P1") == {"primaryAccession": "P1"}


def test_fetch_entries_preserves_order_and_drops_failures():
    entries_by_accession = {
        "P1": {"primaryAccession": "P1"},
        "P3": {"primaryAccession": "P3"},
    }

    class MixedClient:
        def get(self, url, **kwargs):
            accession = url.rsplit("/", 1)[-1]
            if accession not in entries_by_accession:
                raise httpx.RequestError("not found")
            return _FakeHttpResponse(entries_by_accession[accession])

    client = MixedClient()
    result = search_uniprot._fetch_entries(client, ["P1", "P2", "P3"])
    assert [entry["primaryAccession"] for entry in result] == ["P1", "P3"]


# ---------------------------------------------------------------------------
# search_uniprot_structured (end-to-end with mocked httpx.Client)
# ---------------------------------------------------------------------------


class _FakeContextClient(_FakeClient):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_search_uniprot_structured_rejects_empty_query():
    result = search_uniprot.search_uniprot_structured(query="   ")
    assert result == {"status": "error", "message": "Query must not be empty."}


def test_search_uniprot_structured_no_accessions_found(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _FakeContextClient([_FakeHttpResponse({"results": []})]),
    )
    result = search_uniprot.search_uniprot_structured(query="nonexistent")
    assert result["status"] == "success"
    assert result["proteins"] == []
    assert result["total_found"] == 0


def test_search_uniprot_structured_success(monkeypatch):
    search_response = _FakeHttpResponse(
        {"results": [{"primaryAccession": "P1"}]},
        headers={"x-total-results": "1"},
    )
    detail_response = _FakeHttpResponse(_minimal_entry("UniProtKB reviewed (Swiss-Prot)"))
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _FakeContextClient([search_response, detail_response]),
    )
    result = search_uniprot.search_uniprot_structured(query="insulin")
    assert result["status"] == "success"
    assert result["returned"] == 1
    assert result["proteins"][0]["accession"] == "P12345"


def test_search_uniprot_structured_applies_species_filter(monkeypatch):
    captured_queries = []

    class RecordingClient(_FakeContextClient):
        def get(self, url, params=None, **kwargs):
            if params and "query" in params:
                captured_queries.append(params["query"])
            return self._responses.pop(0)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: RecordingClient([_FakeHttpResponse({"results": []})]),
    )
    search_uniprot.search_uniprot_structured(query="insulin", species="Homo sapiens")
    assert 'organism_name:"Homo sapiens"' in captured_queries[0]


def test_search_uniprot_structured_http_error(monkeypatch):
    class FailingClient(_FakeContextClient):
        def get(self, *args, **kwargs):
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FailingClient([]))
    result = search_uniprot.search_uniprot_structured(query="insulin")
    assert result["status"] == "error"
    assert "503" in result["message"]


def test_search_uniprot_structured_request_error(monkeypatch):
    class FailingClient(_FakeContextClient):
        def get(self, *args, **kwargs):
            raise httpx.RequestError("network down")

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FailingClient([]))
    result = search_uniprot.search_uniprot_structured(query="insulin")
    assert result["status"] == "error"
    assert "Request error" in result["message"]
