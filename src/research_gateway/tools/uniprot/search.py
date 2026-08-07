# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""UniProtKB protein search.

Resolves a free-text query into full protein records in a single tool call by
chaining the two UniProt REST endpoints:

1. ``/uniprotkb/search`` returns the ordered list of matching accessions.
2. ``/uniprotkb/{accession}`` returns the detail record for each one.

The detail fetches are fanned out concurrently and the results reordered to
match search relevance order.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx

# Configure logging
logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger("search_uniprot")
logger.setLevel(logging.INFO)

# Type aliases for better readability
EntryDict = Dict[str, Any]
ProteinDict = Dict[str, Any]

UNIPROT_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb"
UNIPROT_ENTRY_WEB_URL = "https://www.uniprot.org/uniprotkb"

# UniProt asks API clients to identify themselves so they can be contacted about
# problematic usage patterns rather than simply blocked.
USER_AGENT = "research-gateway-uniprot-search/1.0"

# The tool takes only a query, so the result count is fixed here rather than
# exposed as an input parameter.
DEFAULT_LIMIT = 10

REQUEST_TIMEOUT = 25.0
MAX_FETCH_WORKERS = 10

# Fields requested from the detail endpoint. Anything not listed here is absent
# from the response, so this list and _clean_entry must stay in sync.
DETAIL_FIELDS = (
    "accession,id,protein_name,gene_names,organism_name,length,reviewed,"
    "cc_function,cc_subcellular_location,cc_disease,ft_domain,ft_region,"
    "xref_pdb,sequence"
)

# UniProt's commentType values are space-separated, not underscore-separated -
# "SUBCELLULAR LOCATION", not "SUBCELLULAR_LOCATION". Getting this wrong fails
# silently: the comment is simply never matched and the field comes back empty.
COMMENT_FUNCTION = "FUNCTION"
COMMENT_SUBCELLULAR_LOCATION = "SUBCELLULAR LOCATION"
COMMENT_DISEASE = "DISEASE"

# Detects a query that already uses UniProt field syntax (e.g. `gene:INS`,
# `organism_name:"Homo sapiens"`, `reviewed:true`). Such queries are passed
# through untouched instead of being expanded by _build_search_query.
_FIELD_TOKEN_PATTERN = re.compile(r"\b[a-z][a-z0-9_]*:")



def _escape_term(term: str) -> str:
    """Escape a term for interpolation into a quoted UniProt query clause.

    An unescaped double quote or trailing backslash would terminate the quoted
    phrase early and corrupt the surrounding boolean expression.
    """

    return term.replace("\\", "\\\\").replace('"', '\\"')


def _build_search_query(query: str) -> str:
    """Expand a free-text query into a field-scoped UniProt query.

    Queries that already contain UniProt field syntax are returned verbatim, so
    callers can reach filters this tool does not expose as parameters::

        'insulin AND organism_name:"Homo sapiens"'
        'spike AND reviewed:true'

    Everything else is expanded across the name, gene, function, disease, and
    keyword fields, plus a plain full-text clause requiring every term.

    Args:
        query: The caller's raw query string.

    Returns:
        A UniProt query string suitable for the ``query`` request parameter.
    """

    query = query.strip()

    if _FIELD_TOKEN_PATTERN.search(query):
        logger.info("Query uses UniProt field syntax, passing through unmodified")
        return query

    terms = query.split()
    if not terms:
        return query

    if len(terms) == 1:
        term = _escape_term(terms[0])
        return (
            f'(protein_name:"{term}" OR gene:"{term}" OR cc_function:"{term}" '
            f'OR cc_disease:"{term}" OR keyword:"{term}")'
        )

    full = _escape_term(" ".join(terms))
    escaped_terms = [_escape_term(term) for term in terms]

    clauses = [
        f'protein_name:"{full}"',
        f'cc_function:"{full}"',
        f'cc_disease:"{full}"',
        # Plain full-text conjunction. This is the clause that carries queries
        # whose wording does not match any single field, e.g. "SARS-CoV-2 spike
        # protein", where the organism and the protein name live in different
        # fields. Terms stay quoted so a hyphen (SARS-CoV-2) is not read as a
        # Lucene negation operator.
        "({})".format(" AND ".join(f'"{term}"' for term in escaped_terms)),
    ]
    for term in escaped_terms:
        clauses.extend(
            [f'protein_name:"{term}"', f'gene:"{term}"', f'keyword:"{term}"']
        )

    return f"({' OR '.join(clauses)})"


def _resolve_species_filter(species: str) -> str:
    """Build an ``organism_name`` clause for the optional species filter.

    ``species`` is passed to ``organism_name`` verbatim - callers must supply a
    scientific name UniProt recognizes (e.g. ``"Homo sapiens"``), not a common
    name like ``"human"``.

    Args:
        species: A scientific organism name.

    Returns:
        An ``organism_name:"..."`` clause.
    """

    return f'organism_name:"{_escape_term(species.strip())}"'


def _search_accessions(
    client: httpx.Client, uniprot_query: str, limit: int
) -> tuple[List[str], int]:
    """Return matching accessions in relevance order, plus the total match count.

    Only the ``accession`` field is requested — the detail records are fetched
    separately by :func:`_fetch_entries`.

    Args:
        client: Shared HTTP client.
        uniprot_query: A built UniProt query string.
        limit: Maximum number of accessions to return.

    Returns:
        A ``(accessions, total_found)`` tuple. ``total_found`` is UniProt's total
        match count, which is typically larger than ``len(accessions)``.
    """

    response = client.get(
        UNIPROT_SEARCH_URL,
        params={
            "query": uniprot_query,
            "format": "json",
            "size": str(limit),
            "fields": "accession",
        },
    )
    response.raise_for_status()
    results = response.json().get("results", [])

    accessions = [
        result["primaryAccession"] for result in results if result.get("primaryAccession")
    ]

    # UniProt reports the full match count in a header, not the body. It is
    # omitted on some responses, so fall back to what we actually received.
    total_header = response.headers.get("x-total-results")
    try:
        total_found = int(total_header) if total_header else len(accessions)
    except ValueError:
        total_found = len(accessions)

    return accessions, total_found


def _fetch_entry(client: httpx.Client, accession: str) -> Optional[EntryDict]:
    """Fetch one detail record, returning None if it cannot be retrieved."""

    try:
        response = client.get(
            f"{UNIPROT_ENTRY_URL}/{accession}",
            params={"format": "json", "fields": DETAIL_FIELDS},
        )
        response.raise_for_status()
        return response.json()
    except Exception as fetch_error:  # noqa: BLE001
        # One bad accession should not sink the whole search — drop it and keep
        # the rest of the result set.
        logger.warning(f"Failed to fetch UniProt entry {accession}: {fetch_error}")
        return None


def _fetch_entries(client: httpx.Client, accessions: List[str]) -> List[EntryDict]:
    """Fetch detail records for every accession, preserving relevance order.

    Args:
        client: Shared HTTP client.
        accessions: Accessions in the order returned by the search endpoint.

    Returns:
        Detail records in the same order, with unretrievable entries omitted.
    """

    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        entries = executor.map(lambda acc: _fetch_entry(client, acc), accessions)

    # executor.map yields in input order, so ordering is already correct.
    return [entry for entry in entries if entry is not None]


def _extract_protein_name(entry: EntryDict) -> Optional[str]:
    """Return the best available protein name.

    ``recommendedName`` is only present on reviewed (Swiss-Prot) entries;
    unreviewed TrEMBL entries carry a ``submissionNames`` list instead.
    """

    description = entry.get("proteinDescription", {})

    recommended = (
        description.get("recommendedName", {}).get("fullName", {}).get("value")
    )
    if recommended:
        return recommended

    for key in ("submissionNames", "alternativeNames"):
        for name in description.get(key, []) or []:
            value = name.get("fullName", {}).get("value")
            if value:
                return value

    return None


def _extract_comment_texts(entry: EntryDict, comment_type: str) -> List[str]:
    """Return every text value across comments of the given type."""

    texts: List[str] = []
    for comment in entry.get("comments", []) or []:
        if comment.get("commentType") != comment_type:
            continue
        for text in comment.get("texts", []) or []:
            value = text.get("value")
            if value:
                texts.append(value)
    return texts


def _extract_subcellular_locations(entry: EntryDict) -> List[str]:
    """Return deduplicated subcellular location names, in first-seen order."""

    locations: List[str] = []
    for comment in entry.get("comments", []) or []:
        if comment.get("commentType") != COMMENT_SUBCELLULAR_LOCATION:
            continue
        for location in comment.get("subcellularLocations", []) or []:
            value = location.get("location", {}).get("value")
            if value and value not in locations:
                locations.append(value)
    return locations


def _extract_diseases(entry: EntryDict) -> List[Dict[str, Any]]:
    """Return disease associations as ``{disease_id, acronym, description}`` dicts."""

    diseases = []
    for comment in entry.get("comments", []) or []:
        if comment.get("commentType") != COMMENT_DISEASE:
            continue
        disease = comment.get("disease") or {}
        if not disease:
            continue
        texts = comment.get("texts", []) or []
        diseases.append(
            {
                "disease_id": disease.get("diseaseId"),
                "acronym": disease.get("acronym"),
                "description": texts[0].get("value") if texts else None,
            }
        )
    return diseases


def _extract_features(entry: EntryDict) -> List[Dict[str, Any]]:
    """Return annotated features as ``{type, start, end, description}`` dicts."""

    features = []
    for feature in entry.get("features", []) or []:
        location = feature.get("location", {})
        features.append(
            {
                "type": feature.get("type"),
                "start": location.get("start", {}).get("value"),
                "end": location.get("end", {}).get("value"),
                "description": feature.get("description") or None,
            }
        )
    return features


def _clean_entry(entry: EntryDict) -> ProteinDict:
    """Flatten a UniProt detail record into a tidy output dict.

    Missing values become ``None`` or ``[]`` rather than placeholder strings, so
    the result stays type-consistent with the tool's ``outputSchema``.
    """

    accession = entry.get("primaryAccession")
    genes = entry.get("genes", []) or []
    functions = _extract_comment_texts(entry, COMMENT_FUNCTION)

    return {
        "accession": accession,
        "entry_name": entry.get("uniProtkbId"),
        "protein_name": _extract_protein_name(entry),
        "gene_names": [
            name
            for name in (
                gene.get("geneName", {}).get("value") for gene in genes
            )
            if name
        ],
        "organism": entry.get("organism", {}).get("scientificName"),
        "length": entry.get("sequence", {}).get("length"),
        # entryType reads "UniProtKB reviewed (Swiss-Prot)" or
        # "UniProtKB unreviewed (TrEMBL)".
        "reviewed": "reviewed" in (entry.get("entryType") or "").lower()
        and "unreviewed" not in (entry.get("entryType") or "").lower(),
        "url": f"{UNIPROT_ENTRY_WEB_URL}/{accession}/entry" if accession else None,
        "function": functions[0] if functions else None,
        "subcellular_locations": _extract_subcellular_locations(entry),
        "diseases": _extract_diseases(entry),
        "features": _extract_features(entry),
        "pdb_ids": [
            xref["id"]
            for xref in entry.get("uniProtKBCrossReferences", []) or []
            if xref.get("database") == "PDB" and xref.get("id")
        ],
        "sequence": entry.get("sequence", {}).get("value"),
    }


def search_uniprot_structured(
    query: str,
    species: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Search UniProtKB and return full protein records as structured data.

    Runs the search and the per-accession detail fetches over a single pooled
    HTTP client, so callers get complete records from one tool call.

    Args:
        query: Free-text protein query, e.g. ``"Human insulin"`` or
            ``"SARS-CoV-2 spike protein"``. Native UniProt field syntax is
            passed through unmodified - see :func:`_build_search_query`.
        species: Optional organism filter. Must be a scientific name UniProt
            recognizes, e.g. ``"Homo sapiens"``, not a common name like
            ``"human"``. ANDed onto the built query - see
            :func:`_resolve_species_filter`. If ``query`` already contains its
            own ``organism_name`` field filter, the two are combined, which can
            produce a contradictory query; omit ``species`` when the query
            already filters by organism.
        limit: Maximum number of protein records to return.

    Returns:
        On success::

            {
                "status": "success",
                "query": <original query>,
                "uniprot_query": <query actually sent to UniProt>,
                "total_found": <int>,
                "returned": <int>,
                "proteins": [<protein dict>, ...],
            }

        On failure::

            {"status": "error", "message": "..."}
    """

    logger.info(f"Searching UniProt for: {query}")

    if not query or not query.strip():
        return {"status": "error", "message": "Query must not be empty."}

    uniprot_query = _build_search_query(query)
    if species and species.strip():
        uniprot_query = f"({uniprot_query}) AND {_resolve_species_filter(species)}"

    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            accessions, total_found = _search_accessions(client, uniprot_query, limit)
            logger.info(f"Found {len(accessions)} accessions of {total_found} total")

            if not accessions:
                return {
                    "status": "success",
                    "query": query,
                    "uniprot_query": uniprot_query,
                    "total_found": 0,
                    "returned": 0,
                    "proteins": [],
                }

            entries = _fetch_entries(client, accessions)

        logger.info(f"Fetched {len(entries)} detail records")

        return {
            "status": "success",
            "query": query,
            "uniprot_query": uniprot_query,
            "total_found": total_found,
            "returned": len(entries),
            "proteins": [_clean_entry(entry) for entry in entries],
        }

    except httpx.HTTPStatusError as http_error:
        logger.error(f"HTTP error during UniProt search: {http_error}")
        return {
            "status": "error",
            "message": (
                f"HTTP {http_error.response.status_code} from UniProt: {http_error}"
            ),
        }
    except httpx.RequestError as request_error:
        logger.error(f"Request error during UniProt search: {request_error}")
        return {"status": "error", "message": f"Request error: {request_error}"}
    except KeyError as key_error:
        logger.error(f"Unexpected search response format: {key_error}")
        return {
            "status": "error",
            "message": f"Unexpected UniProt response format: missing {key_error}",
        }
    except Exception as unexpected_error:  # noqa: BLE001
        logger.error(
            f"Unexpected error in search_uniprot_structured: {unexpected_error}"
        )
        return {"status": "error", "message": str(unexpected_error)}
