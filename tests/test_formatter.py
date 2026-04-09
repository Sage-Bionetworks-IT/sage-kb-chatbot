"""Property tests for answer formatting (RED).

Property 12: Answer formatting includes all required components
Property 13: Partial failure fallback includes all successful
             tool outputs

These tests should FAIL until task 1.6 implements the
formatting functions.
"""

from hypothesis import given
from hypothesis import strategies as st

from slack_agent_router.formatter import (
    format_answer,
    format_fallback_answer,
)
from slack_agent_router.models import AgentResponse, ToolOutput

# --- Strategies ---

url_path = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        min_codepoint=97,
        max_codepoint=122,
    ),
    min_size=3,
    max_size=20,
)

system_name = st.sampled_from(["Confluence", "Jira", "Google Sites"])

source_entry = st.fixed_dictionaries(
    {
        "title": st.text(min_size=1, max_size=30).filter(lambda s: s.strip() != ""),
        "url": url_path.map(lambda p: f"https://example.com/{p}"),
        "system": system_name,
    }
)

answer_text = st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != "")


@st.composite
def agent_response_with_sources(draw):
    """Generate an AgentResponse with answer and sources."""
    answer = draw(answer_text)
    sources = draw(st.lists(source_entry, min_size=1, max_size=5))
    urls = [s["url"] for s in sources]
    latency = draw(st.floats(min_value=100, max_value=30000))
    return (
        AgentResponse(
            answer=answer,
            source_urls=urls,
            tool_calls_made=["SearchConfluenceJira"],
            latency_ms=latency,
        ),
        sources,
    )


@st.composite
def successful_tool_output(draw):
    """Generate a successful ToolOutput."""
    content = draw(answer_text)
    sources = draw(st.lists(source_entry, min_size=1, max_size=3))
    return ToolOutput(
        success=True,
        content=content,
        sources=sources,
        error_message=None,
    )


# -------------------------------------------------------
# Property 12: Answer formatting includes all components
# -------------------------------------------------------


class TestAnswerFormatting:
    """Property 12: formatted answer has all required parts."""

    @given(data=agent_response_with_sources())
    def test_contains_answer_text(self, data):
        response, _ = data
        result = format_answer(response, response.latency_ms / 1000)
        assert response.answer in result

    @given(data=agent_response_with_sources())
    def test_contains_all_source_urls(self, data):
        response, _ = data
        result = format_answer(response, response.latency_ms / 1000)
        for url in response.source_urls:
            assert url in result

    @given(data=agent_response_with_sources())
    def test_sources_are_numbered(self, data):
        response, _ = data
        result = format_answer(response, response.latency_ms / 1000)
        for i in range(1, len(response.source_urls) + 1):
            assert f"{i}." in result

    @given(data=agent_response_with_sources())
    def test_contains_latency_footer(self, data):
        response, _ = data
        elapsed = response.latency_ms / 1000
        result = format_answer(response, elapsed)
        assert f"{elapsed:.1f}s" in result

    @given(data=agent_response_with_sources())
    def test_sources_header_present(self, data):
        response, _ = data
        result = format_answer(response, response.latency_ms / 1000)
        assert "*Sources:*" in result

    def test_empty_sources_no_sources_section(self):
        response = AgentResponse(
            answer="Some answer",
            source_urls=[],
            tool_calls_made=[],
            latency_ms=1000.0,
        )
        result = format_answer(response, 1.0)
        assert "Some answer" in result


# -------------------------------------------------------
# Property 13: Partial failure fallback includes all
#              successful tool outputs
# -------------------------------------------------------


class TestFallbackFormatting:
    """Property 13: fallback includes all successful outputs."""

    @given(outputs=st.lists(successful_tool_output(), min_size=1, max_size=3))
    def test_contains_all_content(self, outputs):
        result = format_fallback_answer(outputs)
        for output in outputs:
            assert output.content in result

    @given(outputs=st.lists(successful_tool_output(), min_size=1, max_size=3))
    def test_contains_all_source_urls(self, outputs):
        result = format_fallback_answer(outputs)
        for output in outputs:
            for source in output.sources:
                assert source["url"] in result

    @given(outputs=st.lists(successful_tool_output(), min_size=1, max_size=3))
    def test_contains_fallback_prefix(self, outputs):
        result = format_fallback_answer(outputs)
        assert "trouble synthesizing" in result.lower()

    def test_empty_outputs_returns_message(self):
        result = format_fallback_answer([])
        assert isinstance(result, str)
        assert len(result) > 0
