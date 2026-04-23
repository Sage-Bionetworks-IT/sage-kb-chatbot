"""Vertex AI Search Backend — queries Google Sites via Discovery Engine API.

Uses the google-cloud-discoveryengine library to search the company
Google Sites website and return results with AI-generated summaries.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.api_core.exceptions import GoogleAPIError
from google.cloud.discoveryengine_v1 import SearchRequest, SearchServiceAsyncClient
from google.oauth2.service_account import Credentials

from slack_agent_router.models import BackendResult

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexAISearchBackend:
    """Google Sites search via Vertex AI Search.

    Queries the Discovery Engine Search API with a configured project,
    location, and data store. Authenticates using GCP service account
    credentials.
    """

    def __init__(
        self,
        project_id: str,
        location: str,
        data_store_id: str,
        service_account_credentials: dict,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._data_store_id = data_store_id
        self._credentials_info = service_account_credentials
        self._timeout_seconds = timeout_seconds
        self._serving_config = (
            f"projects/{project_id}/locations/{location}/dataStores/{data_store_id}/servingConfigs/default_search"
        )

    @property
    def name(self) -> str:
        return "Google Sites (Vertex AI Search)"

    async def query(self, question: str) -> BackendResult:
        """Search Google Sites content via Vertex AI Search."""
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._search(question),
                timeout=self._timeout_seconds,
            )
            return self._parse_response(response, start)
        except asyncio.TimeoutError:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message="Vertex AI Search request timed out",
                latency_ms=_elapsed_ms(start),
            )
        except GoogleAPIError as exc:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=f"Vertex AI Search API error: {exc}",
                latency_ms=_elapsed_ms(start),
            )
        except Exception as exc:
            logger.error("Unexpected error querying Vertex AI Search: %s", exc, exc_info=True)
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=[],
                error_message=f"Unexpected error: {exc}",
                latency_ms=_elapsed_ms(start),
            )

    async def health_check(self) -> bool:
        """Check if the Vertex AI Search API is reachable.

        Sends a lightweight search request to verify connectivity
        and authentication.
        """
        try:
            await asyncio.wait_for(
                self._search("health check"),
                timeout=self._timeout_seconds,
            )
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _search(self, question: str) -> object:
        """Execute a search request against the Discovery Engine API.

        Creates a fresh client per request to avoid holding connections
        open between requests. The client's transport is closed in a
        finally block to prevent leaking gRPC channels/sockets.
        """
        credentials = Credentials.from_service_account_info(
            self._credentials_info,
            scopes=_SCOPES,
        )
        client = SearchServiceAsyncClient(credentials=credentials)

        try:
            request = SearchRequest(
                serving_config=self._serving_config,
                query=question,
                page_size=10,
                content_search_spec=SearchRequest.ContentSearchSpec(
                    summary_spec=SearchRequest.ContentSearchSpec.SummarySpec(
                        summary_result_count=5,
                        include_citations=True,
                    ),
                ),
            )

            response = await client.search(request=request)
            return response
        finally:
            await client.transport.close()

    def _parse_response(self, response: object, start: float) -> BackendResult:
        """Convert a Discovery Engine SearchResponse into a BackendResult."""
        source_urls = _extract_source_urls(response)
        summary_text = _extract_summary(response)

        if not summary_text:
            return BackendResult(
                backend_name=self.name,
                success=False,
                answer=None,
                source_urls=source_urls,
                error_message="Vertex AI Search returned no summary",
                latency_ms=_elapsed_ms(start),
            )

        return BackendResult(
            backend_name=self.name,
            success=True,
            answer=summary_text,
            source_urls=source_urls,
            error_message=None,
            latency_ms=_elapsed_ms(start),
        )


def _extract_source_urls(response: object) -> list[str]:
    """Extract document URLs from search results, preserving order and deduplicating."""
    urls: list[str] = []
    results = getattr(response, "results", None)
    if not results:
        return urls

    for result in results:
        doc = getattr(result, "document", None)
        if doc is None:
            continue
        struct_data = getattr(doc, "derived_struct_data", None)
        if struct_data and hasattr(struct_data, "get"):
            link = struct_data.get("link")
            if link and link not in urls:
                urls.append(link)

    return urls


def _extract_summary(response: object) -> str | None:
    """Extract the AI-generated summary text from the response."""
    summary = getattr(response, "summary", None)
    if summary is None:
        return None
    text = getattr(summary, "summary_text", None)
    if not text or not text.strip():
        return None
    return text


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since *start* (monotonic)."""
    return (time.monotonic() - start) * 1000
