"""GLM Search Tool - Web search powered by Bocha Search API.

This tool provides web search capabilities using Bocha's web search API.
It supports parallel multi-query searches with configurable parameters.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .base import Tool, ToolResult

# Bocha Search API endpoint (alicloud marketplace)
BOCHA_API_URL = "https://bocha.market.alicloudapi.com/v1/web-search"

# Valid Bocha freshness values (used for validation)
_VALID_FRESHNESS = {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}

# Legacy freshness values from previous search providers -> Bocha equivalents
_FRESHNESS_COMPAT_MAP: dict[str, str] = {
    "pastDay": "oneDay",
    "pastWeek": "oneWeek",
    "pastMonth": "oneMonth",
}


def _resolve_freshness(freshness: str | None) -> str:
    """Resolve freshness value, mapping legacy names to Bocha equivalents."""
    if freshness in _VALID_FRESHNESS:
        return freshness
    if freshness in _FRESHNESS_COMPAT_MAP:
        return _FRESHNESS_COMPAT_MAP[freshness]
    return "noLimit"


def _resolve_bool(value: object, default: bool = True) -> bool:
    """Normalize a value to bool, handling string representations from tool calls."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no", "off", "")
    return bool(value)


@dataclass
class QuerySearchResult:
    """Search results for a single query."""

    query: str
    results: list[dict[str, str]]
    success: bool
    error_message: str | None = None


def _parse_bocha_response(data: dict, with_summary: bool = True) -> list[dict[str, str]]:
    """Parse Bocha API response into a list of result dicts."""
    results: list[dict[str, str]] = []
    web_pages = data.get("data", {}).get("webPages", {})
    for item in web_pages.get("value", []):
        if with_summary:
            content = item.get("summary") or item.get("snippet") or ""
        else:
            content = item.get("snippet") or ""

        # Prefer datePublished; only fall back to dateLastCrawled with a
        # distinct label so the model doesn't confuse crawl time with
        # actual publication time.
        date_published = item.get("datePublished") or ""
        date_crawled = item.get("dateLastCrawled") or ""

        results.append(
            {
                "title": item.get("name", ""),
                "snippet": content,
                "link": item.get("url", ""),
                "source": item.get("siteName", ""),
                "datePublished": date_published,
                "dateLastCrawled": date_crawled,
            }
        )
    return results


async def _bocha_search(
    api_key: str, query: str, count: int, freshness: str, summary: bool,
) -> QuerySearchResult:
    """Execute a single Bocha search request."""
    try:
        headers = {
            "Authorization": f"APPCODE {api_key}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        payload = {
            "query": query,
            "count": count,
            "freshness": freshness,
            "summary": summary,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(BOCHA_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 200:
            return QuerySearchResult(
                query=query, results=[], success=False,
                error_message=f"Bocha API error: {data.get('msg') or data.get('message') or 'unknown'}",
            )

        results = _parse_bocha_response(data, with_summary=summary)
        return QuerySearchResult(query=query, results=results, success=True)

    except Exception as e:
        return QuerySearchResult(query=query, results=[], success=False, error_message=str(e))


def _format_search_results(results: list[QuerySearchResult]) -> str:
    """Format search results for display (model-friendly, concise format)."""
    if not results:
        return "No search results found."

    output_parts = []

    for query_result in results:
        output_parts.append(f"Query: {query_result.query}")

        if not query_result.success:
            output_parts.append(f"Error: {query_result.error_message}")
            output_parts.append("")
            continue

        if not query_result.results:
            output_parts.append("No results found.")
            output_parts.append("")
            continue

        for idx, result in enumerate(query_result.results, 1):
            output_parts.append(f"\n[{idx}] {result['title']}")
            output_parts.append(f"URL: {result['link']}")
            output_parts.append(f"Source: {result['source']}")
            if result.get("datePublished"):
                output_parts.append(f"Published: {result['datePublished']}")
            elif result.get("dateLastCrawled"):
                output_parts.append(f"Crawled: {result['dateLastCrawled']}")
            output_parts.append(f"Content: {result['snippet']}")

        output_parts.append("")

    return "\n".join(output_parts)


class GLMSearchTool(Tool):
    """Web search tool powered by Bocha Search API.

    This tool enables the agent to search the web using Bocha's search API.
    It supports configurable search parameters and returns structured results.

    Features:
    - Configurable search parameters (count, freshness, summary)
    - Automatic result formatting with publish date
    - Graceful error handling

    Example usage by agent:
    - glm_search(query="Python async programming", count=5)
    - glm_search(query="MiniMax AI latest news", freshness="oneWeek")
    """

    def __init__(self, api_key: str | None = None):
        """Initialize Bocha search tool.

        Args:
            api_key: Bocha Search API key (AppCode).
                     If not provided, will use BOCHA_SEARCH_APPCODE env var.
        """
        self.api_key = api_key or os.getenv("BOCHA_SEARCH_APPCODE")
        if not self.api_key:
            raise ValueError("BOCHA_SEARCH_APPCODE not provided and not found in environment variables")

    @property
    def name(self) -> str:
        return "glm_search"

    @property
    def description(self) -> str:
        return """Search the web using Bocha Search API.

Performs intelligent web search and returns relevant results with titles, snippets, links, and publish dates.

Parameters:
  - query (required): Search query string
  - count: Number of results to return (default: 10, max: 50)
  - freshness: Time filter - "noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear" (default: "noLimit")
  - summary: Whether to include text summary in results (default: true)

Returns structured search results with title, snippet, link, source, and publish date.

Examples:
  - glm_search(query="Python async programming")
  - glm_search(query="AI news", count=10, freshness="oneWeek")
  - glm_search(query="latest gold price", freshness="oneDay")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (default: 10, max: 50)",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
                "freshness": {
                    "type": "string",
                    "description": "Time filter for search results. Use 'noLimit' to let the search engine decide optimal time range.",
                    "enum": ["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"],
                    "default": "noLimit",
                },
                "summary": {
                    "type": "boolean",
                    "description": "Whether to include text summary (default: true)",
                    "default": True,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        count: int = 10,
        freshness: str | None = None,
        summary: bool = True,
    ) -> ToolResult:
        """Execute web search.

        Args:
            query: Search query string
            count: Number of results
            freshness: Time filter (noLimit/oneDay/oneWeek/oneMonth/oneYear)
            summary: Whether to include text summary

        Returns:
            ToolResult with formatted search results
        """
        try:
            count = int(count) if isinstance(count, str) else count
            count = min(max(1, count), 50)

            resolved_freshness = _resolve_freshness(freshness)
            summary = _resolve_bool(summary)

            result = await _bocha_search(self.api_key, query, count, resolved_freshness, summary)

            formatted_output = _format_search_results([result])

            if not result.success:
                return ToolResult(
                    success=False, content="",
                    error=f"Search failed: {result.error_message}",
                )

            return ToolResult(success=True, content=formatted_output)

        except Exception as e:
            return ToolResult(
                success=False, content="",
                error=f"Bocha search execution failed: {str(e)}",
            )


class GLMBatchSearchTool(Tool):
    """Batch web search tool for multiple queries in parallel.

    This tool enables searching multiple queries simultaneously,
    which is more efficient than sequential searches.
    """

    def __init__(self, api_key: str | None = None):
        """Initialize Bocha batch search tool.

        Args:
            api_key: Bocha Search API key (AppCode).
                     If not provided, will use BOCHA_SEARCH_APPCODE env var.
        """
        self.api_key = api_key or os.getenv("BOCHA_SEARCH_APPCODE")
        if not self.api_key:
            raise ValueError("BOCHA_SEARCH_APPCODE not provided and not found in environment variables")

    @property
    def name(self) -> str:
        return "glm_batch_search"

    @property
    def description(self) -> str:
        return """Search the web for multiple queries in parallel using Bocha Search API.

This tool performs multiple web searches simultaneously, which is more efficient
than running multiple single searches sequentially.

Parameters:
  - queries (required): List of search query strings
  - count: Number of results per query (default: 10, max: 50)
  - freshness: Time filter - "noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear" (default: "noLimit")
  - summary: Whether to include text summary (default: true)

Example:
  glm_batch_search(queries=["Python async", "FastAPI tutorial", "Docker guide"])"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of search query strings",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results per query (default: 10, max: 50)",
                    "default": 10,
                },
                "freshness": {
                    "type": "string",
                    "description": "Time filter",
                    "enum": ["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"],
                    "default": "noLimit",
                },
                "summary": {
                    "type": "boolean",
                    "description": "Whether to include text summary (default: true)",
                    "default": True,
                },
            },
            "required": ["queries"],
        }

    async def execute(
        self,
        queries: list[str],
        count: int = 10,
        freshness: str | None = None,
        summary: bool = True,
    ) -> ToolResult:
        """Execute batch web search.

        Args:
            queries: List of search query strings
            count: Number of results per query
            freshness: Time filter
            summary: Whether to include text summary

        Returns:
            ToolResult with formatted search results for all queries
        """
        try:
            # Parse queries if passed as JSON string (common from tool calls)
            if isinstance(queries, str):
                try:
                    parsed = json.loads(queries)
                    if isinstance(parsed, list):
                        queries = parsed
                    else:
                        # e.g. json.loads('"single query"') -> str
                        queries = [str(parsed)]
                except json.JSONDecodeError:
                    queries = [queries]

            if not queries:
                return ToolResult(success=False, content="", error="No queries provided")

            count = int(count) if isinstance(count, str) else count
            count = min(max(1, count), 50)

            resolved_freshness = _resolve_freshness(freshness)
            summary = _resolve_bool(summary)

            # Execute searches in parallel (max 5 concurrent)
            sem = asyncio.Semaphore(5)

            async def _limited_search(q: str) -> QuerySearchResult:
                async with sem:
                    return await _bocha_search(self.api_key, q, count, resolved_freshness, summary)

            tasks = [_limited_search(q) for q in queries]
            # asyncio.gather preserves input order, no extra sorting needed
            search_results = list(await asyncio.gather(*tasks))

            formatted_output = _format_search_results(search_results)

            all_success = all(r.success for r in search_results)
            if not all_success:
                failed_queries = [r.query for r in search_results if not r.success]
                return ToolResult(
                    success=False, content=formatted_output,
                    error=f"Some searches failed: {', '.join(failed_queries)}",
                )

            return ToolResult(success=True, content=formatted_output)

        except Exception as e:
            return ToolResult(
                success=False, content="",
                error=f"Bocha batch search execution failed: {str(e)}",
            )
