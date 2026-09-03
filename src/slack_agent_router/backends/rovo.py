"""Rovo MCP Backend — queries Atlassian Confluence/Jira via Rovo MCP Server.

Uses the MCP Python SDK's ClientSession with Streamable HTTP transport
to connect to the Rovo MCP Server and execute tool calls.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, ListToolsResult

from slack_agent_router.models import BackendResult

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s\]>\"']+")

# Jira issue key: a project key (letters/digits, starting with a letter)
# followed by a hyphen and an issue number, e.g. "IT-5205". Matched
# case-insensitively so a lowercase "it-5205" is recognised too; keys are
# normalised to uppercase before use. Bounded so we don't match substrings
# inside longer tokens.
_JIRA_KEY_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")


class RovoMCPBackend:
    """Atlassian Rovo MCP Server integration.

    Connects to the Rovo MCP Server using the MCP Python SDK's
    ClientSession with Streamable HTTP transport. Authenticates
    using an Atlassian API token.
    """

    _TOOL_NAME = "rovo_search"

    def __init__(
        self,
        mcp_server_url: str,
        api_token: str,
        cloud_id: str,
        service_user: str = "",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._mcp_server_url = mcp_server_url
        self._api_token = api_token
        self._cloud_id = cloud_id
        self._service_user = service_user
        self._timeout_seconds = timeout_seconds
        self._cached_tool_name: str | None = None

    @property
    def name(self) -> str:
        return "Atlassian Rovo (Confluence/Jira)"

    async def query(self, question: str) -> BackendResult:
        """Search Confluence/Jira content via Rovo MCP Server."""
        start = time.monotonic()
        try:
            mcp_result = await asyncio.wait_for(
                self._call_mcp_tool(question),
                timeout=self._timeout_seconds,
            )
            return self._parse_mcp_result(mcp_result, start)
        except asyncio.TimeoutError:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message="Rovo MCP Server request timed out",
                latency_ms=_elapsed_ms(start),
            )
        except PermissionError as exc:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=f"Authentication failed: {exc}",
                latency_ms=_elapsed_ms(start),
            )
        except ConnectionError as exc:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=f"Connection error: {exc}",
                latency_ms=_elapsed_ms(start),
            )
        except Exception as exc:
            logger.error("Unexpected error querying Rovo MCP: %s", exc, exc_info=True)
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=f"Unexpected error: {exc}",
                latency_ms=_elapsed_ms(start),
            )

    async def health_check(self) -> bool:
        """Check if the Rovo MCP Server is reachable.

        Uses list_tools() instead of a real search call to avoid
        consuming Rovo API quota on every liveness probe.
        """
        try:
            await asyncio.wait_for(
                self._ping_server(),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            logger.debug("Health check failed: %s", exc)
            return False
        return True

    def _build_auth_headers(self) -> dict[str, str]:
        """Build authentication headers for the MCP server.

        A service user can only authenticate with basic auth while
        an atlassian org admin provisioned service account can authenticate
        with a bearer token.

        Uses Basic auth (email:token base64-encoded) when service_user
        is configured, otherwise falls back to Bearer token.
        """
        if self._service_user:
            credentials = f"{self._service_user}:{self._api_token}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {
                "Authorization": f"Basic {encoded}",
                "x-cloud-id": self._cloud_id,
            }
        return {
            "Authorization": f"Bearer {self._api_token}",
            "x-cloud-id": self._cloud_id,
        }

    async def _ping_server(self) -> None:
        """Open a connection and call list_tools() as a lightweight liveness check."""
        headers = self._build_auth_headers()
        async with streamablehttp_client(
            url=self._mcp_server_url,
            headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await session.list_tools()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_mcp_tool(self, question: str) -> CallToolResult:
        """Connect to the MCP server and call all available search tools.

        Opens a fresh Streamable HTTP connection, initialises the
        session, and invokes both Confluence and Jira search tools
        if available. Results are merged into a single CallToolResult.
        The connection is closed when the context managers exit.
        """
        headers = self._build_auth_headers()

        async with streamablehttp_client(
            url=self._mcp_server_url,
            headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools_response = await session.list_tools()
                tool_names = self._resolve_available_tools(tools_response)
                logger.info(
                    "Available MCP tools: %s — selected: %s",
                    [t.name for t in (tools_response.tools or [])],
                    tool_names,
                )

                # Call all selected tools and merge results
                results: list[CallToolResult] = []
                for tool_name in tool_names:
                    tool_args = self._build_tool_args(tool_name, question)
                    result = await session.call_tool(tool_name, tool_args)
                    results.append(result)
                    logger.info("Tool %s returned isError=%s", tool_name, result.isError)

                return self._merge_tool_results(results)

    # Preferred tools in priority order.
    _PREFERRED_TOOLS = ("searchConfluenceUsingCql", "searchJiraIssuesUsingJql")

    @classmethod
    def _resolve_available_tools(cls, tools_response: ListToolsResult) -> list[str]:
        """Find all available search tools from the server's tool list.

        Returns all preferred tools that are available. If none are found,
        falls back to any tool with "search" in the name, then to the
        first available tool.
        """
        if not hasattr(tools_response, "tools") or not tools_response.tools:
            return [cls._PREFERRED_TOOLS[0]]

        available = {t.name for t in tools_response.tools}

        # Collect all preferred tools that exist
        found = [t for t in cls._PREFERRED_TOOLS if t in available]
        if found:
            return found

        # Fall back to any tool with "search" in the name
        search_tools = [t.name for t in tools_response.tools if "search" in t.name.lower()]
        if search_tools:
            return search_tools

        logger.warning("No search tool found in MCP tools list, falling back to: %s", tools_response.tools[0].name)
        return [tools_response.tools[0].name]

    @classmethod
    def _resolve_tool_name(cls, tools_response: ListToolsResult) -> str:
        """Pick the best search tool from the server's tool list.

        Prefers searchConfluenceUsingCql, then searchJiraIssuesUsingJql,
        then any tool with "search" in the name, then falls back to the
        first available tool.

        Note: Kept for backward compatibility with tests. Use
        _resolve_available_tools for the multi-tool flow.
        """
        tools = cls._resolve_available_tools(tools_response)
        return tools[0]

    @staticmethod
    def _merge_tool_results(results: list[CallToolResult]) -> CallToolResult:
        """Merge multiple CallToolResults into a single result.

        Combines content from all successful results. If all results
        are errors, returns the first error.
        """
        if not results:
            from mcp.types import TextContent

            return CallToolResult(
                content=[TextContent(type="text", text="")],
                isError=True,
            )

        if len(results) == 1:
            return results[0]

        # Separate successes from errors
        successes = [r for r in results if not r.isError]
        errors = [r for r in results if r.isError]

        if not successes:
            # All failed — return first error
            return errors[0]

        # Merge content from all successful results
        merged_content = []
        for result in successes:
            if hasattr(result, "content") and result.content:
                merged_content.extend(result.content)

        return CallToolResult(content=merged_content, isError=False)

    def _build_tool_args(self, tool_name: str, question: str) -> dict[str, str]:
        """Build the correct arguments for the selected MCP tool."""
        args: dict[str, str] = {"cloudId": self._cloud_id}

        if tool_name == "searchConfluenceUsingCql":
            # CQL text search: siteSearch ~ "question text"
            escaped = question.replace('"', '\\"')
            args["cql"] = f'siteSearch ~ "{escaped}"'
        elif tool_name == "searchJiraIssuesUsingJql":
            args["jql"] = self._build_jira_jql(question)
        else:
            # Generic fallback
            args["query"] = question

        return args

    @staticmethod
    def _build_jira_jql(question: str) -> str:
        """Build a JQL query for a natural-language Jira question.

        When the question references one or more Jira issue keys (e.g.
        "IT-5205"), we match them with ``key in (...)`` — an exact key
        lookup. A plain ``text ~`` search does NOT reliably match an
        issue by its key, so questions like "what is IT-5205 about?"
        would otherwise return nothing.

        The full-text ``text ~`` clause is OR'd in as well so the query
        still surfaces issues that merely mention the key in their body,
        and so keyword-only questions (no key) keep working.
        """
        escaped = question.replace('"', '\\"')
        text_clause = f'text ~ "{escaped}"'

        keys = _extract_jira_keys(question)
        if not keys:
            return text_clause

        key_list = ", ".join(keys)
        return f"key in ({key_list}) OR {text_clause}"

    def _parse_mcp_result(self, mcp_result: CallToolResult, start: float) -> BackendResult:
        """Convert an MCP tool result into a BackendResult."""
        if mcp_result.isError:
            error_text = self._extract_text(mcp_result)
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=error_text or "MCP tool returned an error",
                latency_ms=_elapsed_ms(start),
            )

        answer_text = self._extract_text(mcp_result)
        if not answer_text:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message="MCP tool returned empty content",
                latency_ms=_elapsed_ms(start),
            )

        source_urls: list[str] = []

        return BackendResult(
            backend_name=self.name,
            success=True,
            answer=answer_text,
            source_urls=source_urls,
            error_message=None,
            latency_ms=_elapsed_ms(start),
        )

    @staticmethod
    def _extract_text(mcp_result: CallToolResult) -> str:
        """Concatenate all text content items from an MCP result."""
        if not hasattr(mcp_result, "content") or not mcp_result.content:
            return ""
        parts: list[str] = []
        for item in mcp_result.content:
            if getattr(item, "type", None) == "text" and getattr(item, "text", None):
                parts.append(item.text)
        return "\n\n".join(parts)


def _extract_jira_keys(text: str) -> list[str]:
    """Return unique Jira issue keys found in *text*, in order of appearance.

    Matches keys like ``IT-5205`` or ``ABC1-42`` case-insensitively,
    normalising each to uppercase. Deduplicates while preserving order
    so the resulting JQL is stable.
    """
    keys = (m.upper() for m in _JIRA_KEY_PATTERN.findall(text))
    return list(dict.fromkeys(keys))


def _extract_urls(text: str) -> list[str]:
    """Extract user-facing URLs from text, filtering out API metadata.

    Handles URLs with parentheses (common in wiki/Jira links) by
    stripping only unbalanced trailing closing parens. Filters out
    REST API endpoints (user lookups, content history, search queries)
    that are internal metadata rather than user-facing page links.
    """
    raw = _URL_PATTERN.findall(text)
    cleaned: list[str] = []
    for url in raw:
        # Strip trailing sentence punctuation
        url = url.rstrip(".,;:!?")
        # Strip unbalanced trailing closing parens
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        # Filter out REST API metadata URLs
        if _is_api_metadata_url(url):
            continue
        cleaned.append(url)
    return list(dict.fromkeys(cleaned))


# Patterns that indicate an internal API/metadata URL rather than
# a user-facing page link.
_API_METADATA_PATTERNS = (
    "/rest/api/user",
    "/rest/api/content/",
    "/rest/api/search",
    "/rest/api/space",
    "/rest/agile/",
)


def _is_api_metadata_url(url: str) -> bool:
    """Return True if the URL is an internal API endpoint, not a user-facing page."""
    for pattern in _API_METADATA_PATTERNS:
        if pattern in url:
            return True
    return False


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since *start* (monotonic)."""
    return (time.monotonic() - start) * 1000
