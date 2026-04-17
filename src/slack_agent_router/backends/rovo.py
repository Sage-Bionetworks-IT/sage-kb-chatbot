"""Rovo MCP Backend — queries Atlassian Confluence/Jira via Rovo MCP Server.

Uses the MCP Python SDK's ClientSession with Streamable HTTP transport
to connect to the Rovo MCP Server and execute tool calls.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from slack_agent_router.models import BackendResult

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+")


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
        timeout_seconds: float = 15.0,
    ) -> None:
        self._mcp_server_url = mcp_server_url
        self._api_token = api_token
        self._cloud_id = cloud_id
        self._timeout_seconds = timeout_seconds

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
        """Check if the Rovo MCP Server is reachable."""
        try:
            await asyncio.wait_for(
                self._call_mcp_tool("health check"),
                timeout=self._timeout_seconds,
            )
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_mcp_tool(self, question: str) -> object:
        """Connect to the MCP server and call the search tool.

        Opens a fresh Streamable HTTP connection, initialises the
        session, and invokes the tool.  The connection is closed when
        the context managers exit.
        """
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "x-cloud-id": self._cloud_id,
        }

        async with streamablehttp_client(
            url=self._mcp_server_url,
            headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools_response = await session.list_tools()
                tool_name = self._resolve_tool_name(tools_response)

                result = await session.call_tool(
                    tool_name,
                    {"query": question},
                )
                return result

    @staticmethod
    def _resolve_tool_name(tools_response: object) -> str:
        """Pick the best search tool from the server's tool list.

        Falls back to the first available tool if no obvious search
        tool is found.
        """
        if not hasattr(tools_response, "tools") or not tools_response.tools:
            return RovoMCPBackend._TOOL_NAME

        for tool in tools_response.tools:
            name_lower = tool.name.lower()
            if "search" in name_lower or "rovo" in name_lower:
                return tool.name

        return tools_response.tools[0].name

    def _parse_mcp_result(self, mcp_result: object, start: float) -> BackendResult:
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
    def _extract_text(mcp_result: object) -> str:
        """Concatenate all text content items from an MCP result."""
        if not hasattr(mcp_result, "content") or not mcp_result.content:
            return ""
        parts: list[str] = []
        for item in mcp_result.content:
            if getattr(item, "type", None) == "text" and getattr(item, "text", None):
                parts.append(item.text)
        return "\n\n".join(parts)


def _extract_urls(text: str) -> list[str]:
    """Extract all HTTP(S) URLs from text, preserving order and removing duplicates."""
    return list(dict.fromkeys(_URL_PATTERN.findall(text)))


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since *start* (monotonic)."""
    return (time.monotonic() - start) * 1000
