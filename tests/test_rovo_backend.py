"""Tests for RovoMCPBackend.

Property 10: Rovo MCP response parsing completeness
Unit tests: auth failure, timeout, health_check
Validates: Requirements 7.2, 7.3, 7.4
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.backends.rovo import RovoMCPBackend
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
    """Generate a realistic source URL."""
    domain = draw(
        st.sampled_from(
            [
                "confluence.example.com/wiki",
                "jira.example.com/browse",
                "confluence.example.com/display",
            ]
        )
    )
    path = draw(url_path)
    return f"https://{domain}/{path}"


@st.composite
def mcp_text_content(draw):
    """Generate an MCP TextContent-like object with text."""
    text = draw(plain_word)
    content = MagicMock()
    content.type = "text"
    content.text = text
    return content, text


@st.composite
def mcp_tool_result(draw):
    """Generate a valid MCP tool call result with text content and source URLs.

    Simulates the structure returned by mcp ClientSession.call_tool().
    """
    num_contents = draw(st.integers(min_value=1, max_value=3))
    contents = []
    all_text = []
    for _ in range(num_contents):
        content, text = draw(mcp_text_content())
        contents.append(content)
        all_text.append(text)

    num_urls = draw(st.integers(min_value=0, max_value=5))
    urls = [draw(source_url()) for _ in range(num_urls)]

    # Build the MCP result mock
    result = MagicMock()
    result.isError = False
    result.content = contents

    return result, all_text, urls


@st.composite
def mcp_tool_result_with_embedded_urls(draw):
    """Generate an MCP result where URLs are embedded in the text content.

    In real MCP responses, source URLs are typically embedded in the text
    content as markdown links or plain URLs rather than in a separate field.
    """
    num_urls = draw(st.integers(min_value=1, max_value=4))
    urls = [draw(source_url()) for _ in range(num_urls)]

    # Build text that includes the URLs as markdown links
    answer_text = draw(plain_word)
    url_lines = [f"- [{draw(plain_word)}]({url})" for url in urls]
    full_text = answer_text + "\n\nSources:\n" + "\n".join(url_lines)

    content = MagicMock()
    content.type = "text"
    content.text = full_text

    result = MagicMock()
    result.isError = False
    result.content = [content]

    return result, full_text, urls


# --- Fixtures ---


@pytest.fixture
def backend():
    """Create a RovoMCPBackend instance for testing."""
    return RovoMCPBackend(
        mcp_server_url="https://mcp.atlassian.com/v1/mcp",
        api_token="test-token-placeholder",
        cloud_id="test-cloud-id",
    )


# -------------------------------------------------------
# Property 10: Rovo MCP response parsing completeness
# For any valid MCP response, the backend produces a
# BackendResult with success=True, answer text, and all
# source URLs.
# -------------------------------------------------------


class TestRovoMCPResponseParsing:
    """Property 10: valid MCP responses produce complete BackendResults."""

    @given(data=mcp_tool_result())
    @settings(max_examples=50)
    async def test_valid_response_produces_successful_result(self, data):
        """For any valid MCP response, result has success=True and answer text."""
        mcp_result, expected_texts, _ = data
        backend = RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token-placeholder",
            cloud_id="test-cloud-id",
        )

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mcp_result
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is True
        assert result.answer is not None
        assert len(result.answer) > 0
        # Every text content piece should appear in the answer
        for text in expected_texts:
            assert text in result.answer
        assert result.backend_name == backend.name
        assert result.error_message is None
        assert result.latency_ms >= 0

    @given(data=mcp_tool_result_with_embedded_urls())
    @settings(max_examples=50)
    async def test_embedded_urls_extracted_into_source_urls(self, data):
        """For any MCP response with embedded URLs, all URLs appear in source_urls."""
        mcp_result, _, expected_urls = data
        backend = RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token-placeholder",
            cloud_id="test-cloud-id",
        )

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mcp_result
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is True
        for url in expected_urls:
            assert url in result.source_urls, f"Missing URL: {url}"

    @given(question=plain_word)
    @settings(max_examples=30)
    async def test_result_is_backend_result_type(self, question):
        """For any question, query() always returns a BackendResult."""
        backend = RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token-placeholder",
            cloud_id="test-cloud-id",
        )

        content = MagicMock()
        content.type = "text"
        content.text = "Some answer"
        mcp_result = MagicMock()
        mcp_result.isError = False
        mcp_result.content = [content]

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mcp_result
            result = await backend.query(question)

        assert isinstance(result, BackendResult)

    async def test_mcp_error_flag_produces_failed_result(self):
        """When MCP result has isError=True, BackendResult has success=False."""
        backend = RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token-placeholder",
            cloud_id="test-cloud-id",
        )

        error_content = MagicMock()
        error_content.type = "text"
        error_content.text = "Tool execution failed"
        mcp_result = MagicMock()
        mcp_result.isError = True
        mcp_result.content = [error_content]

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mcp_result
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None

    async def test_empty_content_produces_failed_result(self):
        """When MCP result has no content, BackendResult has success=False."""
        backend = RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token-placeholder",
            cloud_id="test-cloud-id",
        )

        mcp_result = MagicMock()
        mcp_result.isError = False
        mcp_result.content = []

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mcp_result
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None


# -------------------------------------------------------
# Unit tests: auth failure, timeout, health_check
# Validates: Requirements 7.3, 7.4
# -------------------------------------------------------


class TestRovoMCPAuthFailure:
    """Requirement 7.3: auth failure returns BackendResult with success=False."""

    async def test_auth_failure_returns_failed_result(self, backend):
        """Authentication error produces BackendResult with success=False."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = PermissionError("Authentication failed: invalid API token")
            result = await backend.query("What is our PTO policy?")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None
        assert result.backend_name == backend.name
        assert result.source_urls == []
        assert result.latency_ms >= 0

    async def test_auth_failure_error_message_is_descriptive(self, backend):
        """Auth failure error message describes the authentication problem."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = PermissionError("401 Unauthorized")
            result = await backend.query("test question")

        assert result.error_message is not None
        assert len(result.error_message) > 0


class TestRovoMCPTimeout:
    """Requirement 7.4: timeout returns BackendResult with success=False."""

    async def test_timeout_returns_failed_result(self, backend):
        """Timeout produces BackendResult with success=False."""
        import asyncio

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = asyncio.TimeoutError()
            result = await backend.query("What is our PTO policy?")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None
        assert result.backend_name == backend.name
        assert result.source_urls == []
        assert result.latency_ms >= 0

    async def test_timeout_error_message_is_descriptive(self, backend):
        """Timeout error message describes the timeout condition."""
        import asyncio

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = asyncio.TimeoutError()
            result = await backend.query("test question")

        assert result.error_message is not None
        assert len(result.error_message) > 0

    async def test_http_error_returns_failed_result(self, backend):
        """HTTP error from MCP server produces BackendResult with success=False."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = ConnectionError("503 Service Unavailable")
            result = await backend.query("test question")

        assert isinstance(result, BackendResult)
        assert result.success is False
        assert result.error_message is not None


class TestRovoMCPHealthCheck:
    """Requirement 7.4: health_check returns boolean."""

    async def test_health_check_returns_true_when_healthy(self, backend):
        """health_check returns True when MCP server is reachable."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            content = MagicMock()
            content.type = "text"
            content.text = "ok"
            result = MagicMock()
            result.isError = False
            result.content = [content]
            mock_call.return_value = result

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is True

    async def test_health_check_returns_false_when_unreachable(self, backend):
        """health_check returns False when MCP server is unreachable."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = ConnectionError("Connection refused")

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False

    async def test_health_check_returns_false_on_timeout(self, backend):
        """health_check returns False when MCP server times out."""
        import asyncio

        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = asyncio.TimeoutError()

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False

    async def test_health_check_returns_false_on_auth_error(self, backend):
        """health_check returns False when authentication fails."""
        with patch.object(backend, "_call_mcp_tool", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = PermissionError("401 Unauthorized")

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False


class TestRovoMCPBackendName:
    """Backend name property returns expected value."""

    def test_name_property(self, backend):
        """name property returns the expected backend name."""
        assert backend.name == "Atlassian Rovo (Confluence/Jira)"
