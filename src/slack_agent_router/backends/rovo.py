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
        """Connect to the MCP server and call the search tool.

        Opens a fresh Streamable HTTP connection, initialises the
        session, and invokes the tool.  The connection is closed when
        the context managers exit.
        """
        headers = self._build_auth_headers()

        async with streamablehttp_client(
            url=self._mcp_server_url,
            headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools_response = await session.list_tools()
                tool_name = self._resolve_tool_name(tools_response)
                logger.info(
                    "Available MCP tools: %s — selected: %s",
                    [t.name for t in (tools_response.tools or [])],
                    tool_name,
                )

                # Build arguments based on the selected tool
                tool_args = self._build_tool_args(tool_name, question)

                result = await session.call_tool(tool_name, tool_args)
                return result

    # Preferred tools in priority order — first match wins.
    _PREFERRED_TOOLS = ("searchConfluenceUsingCql", "searchJiraIssuesUsingJql")

    @classmethod
    def _resolve_tool_name(cls, tools_response: ListToolsResult) -> str:
        """Pick the best search tool from the server's tool list.

        Prefers searchConfluenceUsingCql, then searchJiraIssuesUsingJql,
        then any tool with "search" in the name, then falls back to the
        first available tool.
        """
        if not hasattr(tools_response, "tools") or not tools_response.tools:
            return cls._PREFERRED_TOOLS[0]

        available = {t.name for t in tools_response.tools}

        # Check preferred tools first
        for preferred in cls._PREFERRED_TOOLS:
            if preferred in available:
                return preferred

        # Fall back to any tool with "search" in the name
        for tool in tools_response.tools:
            if "search" in tool.name.lower():
                return tool.name

        logger.warning("No search tool found in MCP tools list, falling back to: %s", tools_response.tools[0].name)
        return tools_response.tools[0].name

    def _build_tool_args(self, tool_name: str, question: str) -> dict[str, str]:
        """Build the correct arguments for the selected MCP tool."""
        args: dict[str, str] = {"cloudId": self._cloud_id}

        if tool_name == "searchConfluenceUsingCql":
            # CQL text search: siteSearch ~ "question text"
            escaped = question.replace('"', '\\"')
            args["cql"] = f'siteSearch ~ "{escaped}"'
        elif tool_name == "searchJiraIssuesUsingJql":
            # JQL text search
            escaped = question.replace('"', '\\"')
            args["jql"] = f'text ~ "{escaped}"'
        else:
            # Generic fallback
            args["query"] = question

        return args

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

        source_urls = _extract_urls(answer_text)

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


def _extract_urls(text: str) -> list[str]:
    """Extract all HTTP(S) URLs from text, preserving order and removing duplicates.

    Handles URLs with parentheses (common in wiki/Jira links) by
    stripping only unbalanced trailing closing parens. Also strips
    trailing sentence punctuation.
    """
    raw = _URL_PATTERN.findall(text)
    cleaned: list[str] = []
    for url in raw:
        # Strip trailing sentence punctuation
        url = url.rstrip(".,;:!?")
        # Strip unbalanced trailing closing parens
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        cleaned.append(url)
    return list(dict.fromkeys(cleaned))


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since *start* (monotonic)."""
    return (time.monotonic() - start) * 1000
