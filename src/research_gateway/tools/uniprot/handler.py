# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AWS Lambda entrypoint for the UniProt protein search tool.

Supports two invocation styles:

1. Direct invocation (e.g. ``aws lambda invoke``)::

       {"query": "SARS-CoV-2 spike protein"}

2. Bedrock AgentCore Gateway Lambda target. The gateway flattens the tool's
   ``inputSchema`` properties onto the event and passes tool metadata via
   ``context.client_context.custom`` (notably ``bedrockAgentCoreToolName``,
   which is prefixed with the target name, e.g.
   ``uniprot-search___search_uniprot``).

Both paths return an MCP-compliant tool result::

    {
        "content": [{"type": "text", "text": "..."}],
        "structuredContent": { ... },
        "isError": false
    }

``structuredContent`` mirrors the tool's ``outputSchema`` and always carries the
full amino acid sequence for every match, while ``content`` gives a concise text
summary for text-only consumers.
"""

from typing import Any, Dict, List

from research_gateway.tools.uniprot.search import search_uniprot_structured

TOOL_NAME_DELIMITER = "___"
MAX_SUMMARY_PROTEINS = 5
MAX_SUMMARY_FUNCTION_CHARS = 200


def _extract_tool_name(context: Any) -> str:
    """Return the un-prefixed tool name when invoked via AgentCore Gateway."""

    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) if client_context else None
    if not custom:
        return ""

    raw = custom.get("bedrockAgentCoreToolName", "") if isinstance(custom, dict) else ""
    if TOOL_NAME_DELIMITER in raw:
        return raw.split(TOOL_NAME_DELIMITER, 1)[1]
    return raw


def _format_summary(result: Dict[str, Any]) -> str:
    """Render a short text summary of a successful structured result.

    Sequences are deliberately left out - they live in ``structuredContent``,
    where consumers can read them without a few thousand residues crowding out
    the rest of the summary.
    """

    proteins: List[Dict[str, Any]] = result.get("proteins", []) or []
    returned = result.get("returned", len(proteins))

    if not proteins:
        return f'No UniProt proteins found for query "{result.get("query", "")}".'

    # total_found is deliberately left out of the summary. A plain-text query is
    # expanded into a broad OR across several fields, so UniProt's match count
    # runs into the millions and reads as a meaningful result count when it is
    # not. It stays in structuredContent, where its schema description explains
    # what it actually counts.
    lines = [f"Showing the top {returned} UniProt proteins by relevance.", ""]

    for idx, protein in enumerate(proteins[:MAX_SUMMARY_PROTEINS], start=1):
        name = protein.get("protein_name") or "Unnamed protein"
        organism = protein.get("organism")
        lines.append(f"{idx}. {name}{f' ({organism})' if organism else ''}")

        meta_bits = [f"Accession: {protein.get('accession')}"]
        gene_names = protein.get("gene_names") or []
        if gene_names:
            meta_bits.append(f"Gene: {', '.join(gene_names)}")
        if protein.get("length"):
            meta_bits.append(f"Length: {protein['length']} aa")
        meta_bits.append("reviewed" if protein.get("reviewed") else "unreviewed")
        lines.append(f"   {' | '.join(meta_bits)}")

        function = protein.get("function")
        if function:
            if len(function) > MAX_SUMMARY_FUNCTION_CHARS:
                function = function[:MAX_SUMMARY_FUNCTION_CHARS] + "..."
            lines.append(f"   Function: {function}")

        locations = protein.get("subcellular_locations") or []
        if locations:
            lines.append(f"   Location: {', '.join(locations)}")

        pdb_ids = protein.get("pdb_ids") or []
        if pdb_ids:
            lines.append(f"   Structures: {', '.join(pdb_ids[:5])}")

    if returned > MAX_SUMMARY_PROTEINS:
        lines.append(
            f"... plus {returned - MAX_SUMMARY_PROTEINS} more in structuredContent."
        )

    lines.append("")
    lines.append("Full records, including amino acid sequences, are in structuredContent.")

    return "\n".join(lines)


def _success(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _format_summary(result)}],
        "structuredContent": result,
        "isError": False,
    }


def _error(message: str) -> Dict[str, Any]:
    payload = {"status": "error", "message": message}
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "structuredContent": payload,
        "isError": True,
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return _error("Event payload must be a JSON object.")

    tool_name = _extract_tool_name(context)
    if tool_name and tool_name != "search_uniprot":
        return _error(f"Unknown tool: {tool_name}")

    query = event.get("query")
    if not query or not isinstance(query, str):
        return _error("'query' is required and must be a string.")

    species = event.get("species")
    if species is not None and not isinstance(species, str):
        return _error("'species' must be a string when provided.")

    result = search_uniprot_structured(query=query, species=species)
    if result.get("status") == "error":
        return _error(result.get("message", "Unknown error during UniProt search."))

    return _success(result)
