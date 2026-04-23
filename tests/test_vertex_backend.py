"""Tests for VertexAISearchBackend.

Property 11: Vertex AI Search response parsing completeness
Unit tests: API error, permission denied, timeout, health_check, name property
Validates: Requirements 8.2, 8.3
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.backends.vertex import VertexAISearchBackend
from slack_agent_router.models import BackendResult

# --- Strategies ---

plain_word = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        min_codepoint=65,
        max_codepoint=122,
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip() != "")

url_path = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        min_codepoint=48,
        max_codepoint=122,
    ),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")


@st.composite
def source_url(draw):
    """Generate a realistic Google Sites source URL."""
    domain = draw(
        st.sampled_from(
            [
                "sites.google.com/sage.com/handbook",
                "sites.google.com/sage.com/policies",
                "sites.google.com/sage.com/engineering",
            ]
        )
    )
    path = draw(url_path)
    return f"https://{domain}/{path}"


@st.composite
def vertex_search_result(draw):
    """Generate a single Vertex AI Search result with document metadata.

    Simulates the structure returned by the Discovery Engine search API.
    Each result has a document with derived_struct_data containing a link.
    """
    url = draw(source_url())
    title = draw(plain_word)

    result = MagicMock()
    result.document = MagicMock()
    result.document.derived_struct_data = {"link": url, "title": title}

    return result, url, title


@st.composite
def vertex_api_response(draw):
    """Generate a valid Vertex AI Search API response with results and AI summary.

    Simulates the SearchResponse from google.cloud.discoveryengine.
    """
    num_results = draw(st.integers(min_value=1, max_value=5))
    results = []
    expected_urls = []
    for _ in range(num_results):
        result, url, _ = draw(vertex_search_result())
        results.append(result)
        expected_urls.append(url)

    summary_text = draw(plain_word)

    response = MagicMock()
    response.results = results
    response.summary = MagicMock()
    response.summary.summary_text = summary_text

    return response, summary_text, expected_urls


# --- Fixtures ---


@pytest.fixture
def backend():
    """Create a VertexAISearchBackend instance for testing."""
    return VertexAISearchBackend(
        project_id="test-project-id",
        location="global",
        data_store_id="test-data-store-id",
        service_account_credentials={"type": "service_account", "project_id": "test"},
    )


# -------------------------------------------------------
# Property 11: Vertex AI Search response parsing completeness
# For any valid API response, the backend produces a
# BackendResult with success=True, answer text with AI
# summary, and all source URLs.
# Validates: Requirement 8.2
# -------------------------------------------------------


class TestVertexResponseParsing:
    """Property 11: valid Vertex AI Search responses produce complete BackendResults.

    **Validates: Requirements 8.2**
    """

    @given(data=vertex_api_response())
    @settings(max_examples=50)
    async def test_valid_response_produces_successful_result(self, data):
        """For any valid API response with results and AI summary, result has success=True and answer text.

        **Validates: Requirements 8.2**
        """
        api_response, summary_text, _ = data
        backend = VertexAISearchBackend(
            project_id="test-project-id",
            location="global",
            data_store_id="test-data-store-id",
            service_account_credentials={"type": "service_account", "project_id": "test"},
        )

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = api_response
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is True
        assert result.answer is not None
        assert len(result.answer) > 0
        assert summary_text in result.answer
        assert result.backend_name == backend.name
        assert result.error_message is None
        assert result.latency_ms >= 0

    @given(data=vertex_api_response())
    @settings(max_examples=50)
    async def test_all_source_urls_extracted(self, data):
        """For any valid response with document URIs, all URIs appear in source_urls.

        **Validates: Requirements 8.2**
        """
        api_response, _, expected_urls = data
        backend = VertexAISearchBackend(
            project_id="test-project-id",
            location="global",
            data_store_id="test-data-store-id",
            service_account_credentials={"type": "service_account", "project_id": "test"},
        )

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = api_response
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is True
        for url in expected_urls:
            assert url in result.source_urls, f"Missing URL: {url}"

    @given(question=plain_word)
    @settings(max_examples=30)
    async def test_result_is_backend_result_type(self, question):
        """For any question, query() always returns a BackendResult.

        **Validates: Requirements 8.2**
        """
        backend = VertexAISearchBackend(
            project_id="test-project-id",
            location="global",
            data_store_id="test-data-store-id",
            service_account_credentials={"type": "service_account", "project_id": "test"},
        )

        result_mock = MagicMock()
        result_mock.document = MagicMock()
        result_mock.document.derived_struct_data = {"link": "https://sites.google.com/test", "title": "Test"}
        api_response = MagicMock()
        api_response.results = [result_mock]
        api_response.summary = MagicMock()
        api_response.summary.summary_text = "Some answer"

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = api_response
            result = await backend.query(question)

        assert isinstance(result, BackendResult)


# -------------------------------------------------------
# Unit tests: API errors, health_check, name property
# Validates: Requirement 8.3
# -------------------------------------------------------


class TestVertexAPIError:
    """Requirement 8.3: API error returns BackendResult with success=False."""

    async def test_google_api_error_returns_failed_result(self, backend):
        """GoogleAPIError produces BackendResult with success=False."""
        from google.api_core.exceptions import GoogleAPIError

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = GoogleAPIError("Internal server error")
            result = await backend.query("What is our PTO policy?")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None
        assert len(result.error_message) > 0
        assert result.backend_name == backend.name
        assert result.source_urls == []
        assert result.latency_ms >= 0

    async def test_permission_denied_returns_failed_result(self, backend):
        """Permission denied error produces BackendResult with success=False."""
        from google.api_core.exceptions import PermissionDenied

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = PermissionDenied("403 Permission denied")
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None
        assert len(result.error_message) > 0

    async def test_timeout_returns_failed_result(self, backend):
        """Timeout produces BackendResult with success=False."""
        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = asyncio.TimeoutError()
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None
        assert len(result.error_message) > 0

    async def test_api_error_message_is_descriptive(self, backend):
        """API error message describes the problem."""
        from google.api_core.exceptions import GoogleAPIError

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = GoogleAPIError("Service unavailable")
            result = await backend.query("test question")

        assert result.error_message is not None
        assert len(result.error_message) > 0


class TestVertexHealthCheck:
    """Requirement 8.3: health_check returns boolean."""

    async def test_health_check_returns_true_when_healthy(self, backend):
        """health_check returns True when Vertex AI Search API is reachable."""
        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = MagicMock(results=[])
            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is True

    async def test_health_check_returns_false_when_unreachable(self, backend):
        """health_check returns False when Vertex AI Search API is unreachable."""
        from google.api_core.exceptions import GoogleAPIError

        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = GoogleAPIError("Connection refused")
            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False

    async def test_health_check_returns_false_on_timeout(self, backend):
        """health_check returns False when API times out."""
        with patch.object(backend, "_search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = asyncio.TimeoutError()
            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False


class TestVertexBackendName:
    """Backend name property returns expected value."""

    def test_name_property(self, backend):
        """name property returns the expected backend name."""
        assert backend.name == "Google Sites (Vertex AI Search)"
