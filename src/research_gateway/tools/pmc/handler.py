# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AWS Lambda entrypoint for the PMC search tool.

Supports two invocation styles:

1. Direct invocation (e.g. ``aws lambda invoke``)::

       {"query": "CRISPR gene editing", "rerank_by": "references"}

2. Bedrock AgentCore Gateway Lambda target. The gateway flattens the tool's
   ``inputSchema`` properties onto the event and passes tool metadata via
   ``context.client_context.custom`` (notably ``bedrockAgentCoreToolName``,
   which is prefixed with the target name, e.g. ``pmc-search___search_pmc``).

Both paths return an MCP-compliant tool result::

    {
        "content": [{"type": "text", "text": "..."}],
        "structuredContent": { ... },
        "isError": false
    }

``structuredContent`` mirrors the tool's ``outputSchema`` so MCP clients can
consume the results programmatically, while ``content`` gives a concise text
summary for text-only consumers.
"""

from typing import Any, Dict, List, Optional

from research_gateway.tools.pmc.search import search_pmc_structured

TOOL_NAME_DELIMITER = "___"
MAX_SUMMARY_ARTICLES = 5


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
    """Render a short text summary of a successful structured result."""

    articles: List[Dict[str, Any]] = result.get("articles", []) or []
    total_found = result.get("total_found", 0)
    returned = result.get("returned", len(articles))
    ranked_by = result.get("ranked_by")

    if not articles:
        return f'No PMC articles found for query "{result.get("query", "")}".'

    lines = [f"Showing {returned} of {total_found} PMC articles."]
    if ranked_by == "references":
        lines.append("Ranked by citation count within this result set.")
    lines.append("")

    for idx, article in enumerate(articles[:MAX_SUMMARY_ARTICLES], start=1):
        authors = article.get("authors") or []
        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += ", et al."
        meta_bits = [bit for bit in (article.get("journal"), article.get("year")) if bit]
        meta = f" ({', '.join(str(bit) for bit in meta_bits)})" if meta_bits else ""
        lines.append(f"{idx}. {article.get('title') or 'Untitled'}{meta}")
        if author_str:
            lines.append(f"   {author_str}")
        if article.get("pmc_id"):
            lines.append(f"   {article['pmc_id']} · cited by {article.get('referenced_by_count', 0)}")

    if returned > MAX_SUMMARY_ARTICLES:
        lines.append(f"... plus {returned - MAX_SUMMARY_ARTICLES} more in structuredContent.")

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
    if tool_name and tool_name != "search_pmc":
        return _error(f"Unknown tool: {tool_name}")

    query = event.get("query")
    if not query or not isinstance(query, str):
        return _error("'query' is required and must be a string.")

    rerank_by: Optional[str] = event.get("rerank_by", "references")
    if rerank_by not in ("references", None):
        return _error("'rerank_by' must be either 'references' or null.")

    result = search_pmc_structured(query=query, rerank_by=rerank_by)
    if result.get("status") == "error":
        return _error(result.get("message", "Unknown error during PMC search."))

    return _success(result)
