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
from mcp.types import CallToolResult

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
    async def test_source_urls_empty_for_mcp_responses(self, data):
        """Source URLs are not extracted from MCP response text.

        The Bedrock Agent curates citations in its synthesized answer,
        so we don't extract URLs from raw backend responses.
        """
        mcp_result, _, _ = data
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
        assert result.source_urls == []

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
        with patch.object(backend, "_ping_server", new_callable=AsyncMock) as mock_ping:
            mock_ping.return_value = None
            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is True

    async def test_health_check_returns_false_when_unreachable(self, backend):
        """health_check returns False when MCP server is unreachable."""
        with patch.object(backend, "_ping_server", new_callable=AsyncMock) as mock_ping:
            mock_ping.side_effect = ConnectionError("Connection refused")

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False

    async def test_health_check_returns_false_on_timeout(self, backend):
        """health_check returns False when MCP server times out."""
        import asyncio

        with patch.object(backend, "_ping_server", new_callable=AsyncMock) as mock_ping:
            mock_ping.side_effect = asyncio.TimeoutError()

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False

    async def test_health_check_returns_false_on_auth_error(self, backend):
        """health_check returns False when authentication fails."""
        with patch.object(backend, "_ping_server", new_callable=AsyncMock) as mock_ping:
            mock_ping.side_effect = PermissionError("401 Unauthorized")

            healthy = await backend.health_check()

        assert isinstance(healthy, bool)
        assert healthy is False


class TestRovoMCPBackendName:
    """Backend name property returns expected value."""

    def test_name_property(self, backend):
        """name property returns the expected backend name."""
        assert backend.name == "Atlassian Rovo (Confluence/Jira)"


class TestResolveToolName:
    """Tests for _resolve_tool_name fallback logic."""

    def test_picks_tool_with_search_in_name(self):
        """When a tool name contains 'search', it is selected."""
        tool_a = MagicMock()
        tool_a.name = "list_pages"
        tool_b = MagicMock()
        tool_b.name = "confluence_search"
        tools_response = MagicMock()
        tools_response.tools = [tool_a, tool_b]

        result = RovoMCPBackend._resolve_tool_name(tools_response)
        assert result == "confluence_search"

    def test_picks_preferred_tool_over_others(self):
        """When a preferred tool is available, it is selected over others."""
        tool_a = MagicMock()
        tool_a.name = "list_pages"
        tool_b = MagicMock()
        tool_b.name = "searchConfluenceUsingCql"
        tools_response = MagicMock()
        tools_response.tools = [tool_a, tool_b]

        result = RovoMCPBackend._resolve_tool_name(tools_response)
        assert result == "searchConfluenceUsingCql"

    def test_falls_back_to_first_tool_when_no_search_or_rovo(self):
        """When no tool name contains 'search' or preferred names, the first tool is used."""
        tool_a = MagicMock()
        tool_a.name = "list_pages"
        tool_b = MagicMock()
        tool_b.name = "get_content"
        tools_response = MagicMock()
        tools_response.tools = [tool_a, tool_b]

        result = RovoMCPBackend._resolve_tool_name(tools_response)
        assert result == "list_pages"

    def test_falls_back_to_default_when_tools_list_empty(self):
        """When the tools list is empty, the first preferred tool name is returned."""
        tools_response = MagicMock()
        tools_response.tools = []

        result = RovoMCPBackend._resolve_tool_name(tools_response)
        assert result == RovoMCPBackend._PREFERRED_TOOLS[0]

    def test_falls_back_to_default_when_no_tools_attr(self):
        """When the response has no tools attribute, the first preferred tool name is returned."""
        tools_response = object()

        result = RovoMCPBackend._resolve_tool_name(tools_response)
        assert result == RovoMCPBackend._PREFERRED_TOOLS[0]


# -------------------------------------------------------
# _extract_urls helper
# -------------------------------------------------------


class TestExtractUrls:
    """Tests for the _extract_urls helper function."""

    def test_extracts_simple_url(self):
        """Extracts a plain URL from text."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "See https://confluence.example.com/wiki/PTO for details."
        result = _extract_urls(text)
        assert result == ["https://confluence.example.com/wiki/PTO"]

    def test_extracts_multiple_urls(self):
        """Extracts multiple URLs from text."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "Check https://confluence.example.com/a and https://jira.example.com/b"
        result = _extract_urls(text)
        assert len(result) == 2
        assert "https://confluence.example.com/a" in result
        assert "https://jira.example.com/b" in result

    def test_deduplicates_urls(self):
        """Duplicate URLs are returned only once."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "https://example.com/page https://example.com/page"
        result = _extract_urls(text)
        assert result == ["https://example.com/page"]

    def test_strips_trailing_punctuation(self):
        """Trailing sentence punctuation is stripped from URLs."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "Visit https://example.com/page."
        result = _extract_urls(text)
        assert result == ["https://example.com/page"]

    def test_strips_unbalanced_trailing_parens(self):
        """Unbalanced trailing closing parentheses are stripped."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "(see https://example.com/page)"
        result = _extract_urls(text)
        assert result == ["https://example.com/page"]

    def test_preserves_balanced_parens_in_url(self):
        """Balanced parentheses within URLs are preserved."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "https://en.wikipedia.org/wiki/Test_(assessment)"
        result = _extract_urls(text)
        assert result == ["https://en.wikipedia.org/wiki/Test_(assessment)"]

    def test_filters_api_metadata_urls(self):
        """REST API metadata URLs are filtered out."""
        from slack_agent_router.backends.rovo import _extract_urls

        text = "https://confluence.example.com/rest/api/user?key=abc https://confluence.example.com/wiki/PTO"
        result = _extract_urls(text)
        assert result == ["https://confluence.example.com/wiki/PTO"]

    def test_empty_text_returns_empty_list(self):
        """Empty text returns an empty list."""
        from slack_agent_router.backends.rovo import _extract_urls

        assert _extract_urls("") == []

    def test_text_with_no_urls_returns_empty_list(self):
        """Text without URLs returns an empty list."""
        from slack_agent_router.backends.rovo import _extract_urls

        assert _extract_urls("No links here, just plain text.") == []


# -------------------------------------------------------
# _is_api_metadata_url helper
# -------------------------------------------------------


class TestIsApiMetadataUrl:
    """Tests for the _is_api_metadata_url helper function."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://confluence.example.com/rest/api/user?key=abc",
            "https://confluence.example.com/rest/api/content/12345",
            "https://confluence.example.com/rest/api/search?cql=test",
            "https://confluence.example.com/rest/api/space/TEAM",
            "https://jira.example.com/rest/agile/1.0/board/5",
        ],
    )
    def test_detects_api_metadata_urls(self, url):
        """Known API metadata patterns are detected."""
        from slack_agent_router.backends.rovo import _is_api_metadata_url

        assert _is_api_metadata_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://confluence.example.com/wiki/PTO",
            "https://confluence.example.com/display/TEAM/Page",
            "https://jira.example.com/browse/PROJ-123",
            "https://example.com/page",
        ],
    )
    def test_allows_user_facing_urls(self, url):
        """User-facing page URLs are not flagged as API metadata."""
        from slack_agent_router.backends.rovo import _is_api_metadata_url

        assert _is_api_metadata_url(url) is False


# -------------------------------------------------------
# _build_tool_args helper
# -------------------------------------------------------


class TestBuildToolArgs:
    """Tests for the _build_tool_args method."""

    @pytest.fixture()
    def backend(self):
        return RovoMCPBackend(
            mcp_server_url="https://mcp.atlassian.com/v1/mcp",
            api_token="test-token",
            cloud_id="cloud-123",
        )

    def test_confluence_cql_args(self, backend):
        """searchConfluenceUsingCql builds CQL siteSearch query."""
        args = backend._build_tool_args("searchConfluenceUsingCql", "PTO policy")
        assert args["cloudId"] == "cloud-123"
        assert args["cql"] == 'siteSearch ~ "PTO policy"'
        assert "query" not in args

    def test_jira_jql_args(self, backend):
        """searchJiraIssuesUsingJql builds JQL text query."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", "sprint planning")
        assert args["cloudId"] == "cloud-123"
        assert args["jql"] == 'text ~ "sprint planning"'
        assert "query" not in args

    def test_generic_tool_args(self, backend):
        """Unknown tool name falls back to generic query arg."""
        args = backend._build_tool_args("someOtherTool", "test question")
        assert args["cloudId"] == "cloud-123"
        assert args["query"] == "test question"
        assert "cql" not in args
        assert "jql" not in args

    def test_escapes_quotes_in_cql(self, backend):
        """Double quotes in the question are escaped for CQL."""
        args = backend._build_tool_args("searchConfluenceUsingCql", 'find "PTO" info')
        assert '\\"PTO\\"' in args["cql"]

    def test_escapes_quotes_in_jql(self, backend):
        """Double quotes in the question are escaped for JQL."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", 'find "bug" issues')
        assert '\\"bug\\"' in args["jql"]

    def test_jira_key_lookup(self, backend):
        """A referenced issue key produces an exact key lookup OR'd with text."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", "what is IT-5205 about?")
        assert args["jql"] == 'key in (IT-5205) OR text ~ "what is IT-5205 about?"'

    def test_jira_multiple_keys(self, backend):
        """Multiple keys are all included, deduplicated, in order."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", "compare IT-5205 and IT-42 and IT-5205 again")
        assert args["jql"].startswith("key in (IT-5205, IT-42) OR text ~ ")

    def test_jira_no_key_is_text_only(self, backend):
        """A question with no key stays a plain text search."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", "sprint planning")
        assert args["jql"] == 'text ~ "sprint planning"'

    def test_jira_lowercase_key_normalized(self, backend):
        """A lowercase key is detected and normalized to uppercase."""
        args = backend._build_tool_args("searchJiraIssuesUsingJql", "what is it-5205 about?")
        assert args["jql"] == 'key in (IT-5205) OR text ~ "what is it-5205 about?"'


# -------------------------------------------------------
# _resolve_available_tools helper
# -------------------------------------------------------


class TestResolveAvailableTools:
    """Tests for _resolve_available_tools — returns all matching search tools."""

    def test_returns_both_preferred_tools_when_available(self):
        """When both Confluence and Jira tools exist, both are returned."""
        tools_response = MagicMock()
        tool_confluence = MagicMock()
        tool_confluence.name = "searchConfluenceUsingCql"
        tool_jira = MagicMock()
        tool_jira.name = "searchJiraIssuesUsingJql"
        tools_response.tools = [tool_confluence, tool_jira]

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["searchConfluenceUsingCql", "searchJiraIssuesUsingJql"]

    def test_returns_only_confluence_when_jira_missing(self):
        """When only Confluence tool exists, only it is returned."""
        tools_response = MagicMock()
        tool_confluence = MagicMock()
        tool_confluence.name = "searchConfluenceUsingCql"
        tools_response.tools = [tool_confluence]

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["searchConfluenceUsingCql"]

    def test_returns_only_jira_when_confluence_missing(self):
        """When only Jira tool exists, only it is returned."""
        tools_response = MagicMock()
        tool_jira = MagicMock()
        tool_jira.name = "searchJiraIssuesUsingJql"
        tools_response.tools = [tool_jira]

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["searchJiraIssuesUsingJql"]

    def test_falls_back_to_search_named_tools(self):
        """When no preferred tools exist, falls back to tools with 'search' in name."""
        tools_response = MagicMock()
        tool_custom = MagicMock()
        tool_custom.name = "customSearchTool"
        tool_other = MagicMock()
        tool_other.name = "list_pages"
        tools_response.tools = [tool_custom, tool_other]

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["customSearchTool"]

    def test_falls_back_to_first_tool_when_no_search(self):
        """When no search tools exist, falls back to the first available tool."""
        tools_response = MagicMock()
        tool = MagicMock()
        tool.name = "list_pages"
        tools_response.tools = [tool]

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["list_pages"]

    def test_empty_tools_returns_default(self):
        """When tools list is empty, returns the first preferred tool as default."""
        tools_response = MagicMock()
        tools_response.tools = []

        result = RovoMCPBackend._resolve_available_tools(tools_response)
        assert result == ["searchConfluenceUsingCql"]


# -------------------------------------------------------
# _merge_tool_results helper
# -------------------------------------------------------


class TestMergeToolResults:
    """Tests for _merge_tool_results — merging multiple CallToolResults."""

    def test_merges_two_successful_results(self):
        """Content from both successful results is combined."""
        from mcp.types import TextContent

        content_a = TextContent(type="text", text="Confluence result")
        content_b = TextContent(type="text", text="Jira result")

        result_a = CallToolResult(content=[content_a], isError=False)
        result_b = CallToolResult(content=[content_b], isError=False)

        merged = RovoMCPBackend._merge_tool_results([result_a, result_b])

        assert merged.isError is False
        assert len(merged.content) == 2
        assert content_a in merged.content
        assert content_b in merged.content

    def test_merges_one_success_one_error(self):
        """When one succeeds and one fails, only successful content is returned."""
        from mcp.types import TextContent

        content_ok = TextContent(type="text", text="Good result")
        content_err = TextContent(type="text", text="Error details")

        result_ok = CallToolResult(content=[content_ok], isError=False)
        result_err = CallToolResult(content=[content_err], isError=True)

        merged = RovoMCPBackend._merge_tool_results([result_ok, result_err])

        assert merged.isError is False
        assert len(merged.content) == 1
        assert content_ok in merged.content

    def test_all_errors_returns_first_error(self):
        """When all results are errors, returns the first error result."""
        from mcp.types import TextContent

        content_a = TextContent(type="text", text="Auth failed")
        content_b = TextContent(type="text", text="Timeout")

        result_a = CallToolResult(content=[content_a], isError=True)
        result_b = CallToolResult(content=[content_b], isError=True)

        merged = RovoMCPBackend._merge_tool_results([result_a, result_b])

        assert merged.isError is True
        assert merged is result_a

    def test_single_result_returned_as_is(self):
        """A single result is returned unchanged."""
        from mcp.types import TextContent

        content = TextContent(type="text", text="Only result")
        result = CallToolResult(content=[content], isError=False)

        merged = RovoMCPBackend._merge_tool_results([result])

        assert merged is result

    def test_empty_list_returns_error(self):
        """An empty results list returns an error CallToolResult."""
        merged = RovoMCPBackend._merge_tool_results([])

        assert merged.isError is True
        assert isinstance(merged, CallToolResult)
