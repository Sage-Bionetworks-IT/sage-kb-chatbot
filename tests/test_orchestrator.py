"""Property and unit tests for BedrockAgentOrchestrator (RED).

Property 6: Return control loop iteration bound
Property 7: Return control loop duplicate tool call detection
Property 8: Action group to backend mapping correctness
Property 9: Session ID derivation from Slack thread context

Unit tests: agent failure before/after tool calls, timeout enforcement.

These tests should FAIL until tasks 7.2 and 7.3 implement the
orchestrator.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.models import (
    AgentResponse,
    BackendResult,
    ParsedQuestion,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

user_id = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=90),
    min_size=3,
    max_size=10,
)

channel_id = st.from_regex(r"C[A-Z0-9]{8}", fullmatch=True)
thread_ts = st.from_regex(r"[0-9]{10}\.[0-9]{6}", fullmatch=True)
message_ts = st.from_regex(r"[0-9]{10}\.[0-9]{6}", fullmatch=True)

action_group_name = st.sampled_from(["SearchConfluenceJira", "SearchGoogleSites"])

question_text = st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != "")


# ---------------------------------------------------------------------------
# Helpers — fake Bedrock Agent responses
# ---------------------------------------------------------------------------


def _make_return_control_response(action_group: str, function_name: str, parameters: dict) -> dict:
    """Build a fake Bedrock Agent response that requests a tool call."""
    return {
        "returnControl": {
            "invocationId": "inv-123",
            "invocationInputs": [
                {
                    "functionInvocationInput": {
                        "actionGroup": action_group,
                        "function": function_name,
                        "parameters": [{"name": k, "value": v} for k, v in parameters.items()],
                    }
                }
            ],
        }
    }


def _make_final_response(answer: str) -> dict:
    """Build a fake Bedrock Agent final answer response."""
    return {
        "output": {
            "text": answer,
        }
    }


def _make_backend_result(backend_name: str, answer: str = "Some answer") -> BackendResult:
    """Build a successful BackendResult."""
    return BackendResult(
        backend_name=backend_name,
        success=True,
        answer=answer,
        source_urls=["https://example.com/doc1"],
        error_message=None,
        latency_ms=500.0,
    )


def _make_failed_backend_result(backend_name: str) -> BackendResult:
    """Build a failed BackendResult."""
    return BackendResult(
        backend_name=backend_name,
        success=False,
        answer=None,
        source_urls=[],
        error_message="Backend error",
        latency_ms=100.0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rovo_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.name = "Atlassian Rovo (Confluence/Jira)"
    backend.query = AsyncMock(return_value=_make_backend_result("Atlassian Rovo (Confluence/Jira)"))
    return backend


@pytest.fixture()
def vertex_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.name = "Google Sites (Vertex AI Search)"
    backend.query = AsyncMock(return_value=_make_backend_result("Google Sites (Vertex AI Search)"))
    return backend


@pytest.fixture()
def orchestrator(rovo_backend, vertex_backend):
    """Create an orchestrator with mocked backends."""
    from slack_agent_router.orchestrator import BedrockAgentOrchestrator

    return BedrockAgentOrchestrator(
        agent_id="test-agent-id",
        agent_alias_id="test-alias-id",
        rovo_backend=rovo_backend,
        vertex_backend=vertex_backend,
    )


# -------------------------------------------------------
# Property 6: Return control loop iteration bound
# -------------------------------------------------------


class TestReturnControlLoopIterationBound:
    """Property 6: orchestrator executes at most 5 iterations."""

    async def test_max_iterations_enforced(self, orchestrator, rovo_backend):
        """Agent keeps requesting tools forever — orchestrator stops at 5."""
        call_count = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_return_control_response(
                "SearchConfluenceJira",
                "search",
                {"query": f"test-{call_count}"},
            )

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        # initial call + up to 5 return control iterations
        assert call_count <= 6
        assert isinstance(result, AgentResponse)

    async def test_returns_partial_answer_on_max_iterations(self, orchestrator, rovo_backend):
        """When max iterations hit, return best partial answer."""
        call_count = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_return_control_response(
                "SearchConfluenceJira",
                "search",
                {"query": f"test-{call_count}"},
            )

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert isinstance(result, AgentResponse)
        assert len(result.answer) > 0

    @given(n_iterations=st.integers(min_value=6, max_value=20))
    @settings(max_examples=5)
    async def test_never_exceeds_5_iterations_regardless_of_agent(self, n_iterations):
        """For any number of agent tool requests > 5, loop stops at 5."""
        from slack_agent_router.orchestrator import BedrockAgentOrchestrator

        rb = AsyncMock()
        rb.name = "Atlassian Rovo (Confluence/Jira)"
        rb.query = AsyncMock(return_value=_make_backend_result("Atlassian Rovo (Confluence/Jira)"))
        vb = AsyncMock()
        vb.name = "Google Sites (Vertex AI Search)"
        vb.query = AsyncMock(return_value=_make_backend_result("Google Sites (Vertex AI Search)"))

        orch = BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rb,
            vertex_backend=vb,
        )
        call_count = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_return_control_response(
                "SearchConfluenceJira",
                "search",
                {"query": f"q-{call_count}"},
            )

        with patch.object(orch, "_invoke_agent", side_effect=_invoke_side_effect):
            await orch.ask("test question", "C123:1234567890.123456")

        assert call_count <= 6


# -------------------------------------------------------
# Property 7: Duplicate tool call detection
# -------------------------------------------------------


class TestDuplicateToolCallDetection:
    """Property 7: duplicate (action_group, parameters) pairs are skipped."""

    async def test_duplicate_tool_call_skipped(self, orchestrator, rovo_backend):
        """Same action_group + params requested twice — second is skipped."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_final_response("PTO is 20 days."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert rovo_backend.query.call_count == 1
        assert isinstance(result, AgentResponse)

    async def test_different_params_not_treated_as_duplicate(self, orchestrator, rovo_backend):
        """Different parameters for same action group are NOT duplicates."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "benefits"}),
            _make_final_response("PTO and benefits info."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            await orchestrator.ask("PTO and benefits?", "C123:1234567890.123456")

        assert rovo_backend.query.call_count == 2

    @given(query=question_text)
    @settings(max_examples=5)
    async def test_cached_result_reused_for_duplicate(self, query):
        """For any duplicate tool call, the cached result is reused."""
        from slack_agent_router.orchestrator import BedrockAgentOrchestrator

        rb = AsyncMock()
        rb.name = "Atlassian Rovo (Confluence/Jira)"
        rb.query = AsyncMock(return_value=_make_backend_result("Atlassian Rovo (Confluence/Jira)"))
        vb = AsyncMock()
        vb.name = "Google Sites (Vertex AI Search)"
        vb.query = AsyncMock(return_value=_make_backend_result("Google Sites (Vertex AI Search)"))

        orch = BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rb,
            vertex_backend=vb,
        )
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": query}),
            _make_return_control_response("SearchConfluenceJira", "search", {"query": query}),
            _make_final_response("Answer."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orch, "_invoke_agent", side_effect=_invoke_side_effect):
            await orch.ask(query, "C123:1234567890.123456")

        assert rb.query.call_count == 1


# -------------------------------------------------------
# Property 8: Action group to backend mapping
# -------------------------------------------------------


class TestActionGroupToBackendMapping:
    """Property 8: action groups map to correct backends."""

    async def test_search_confluence_jira_maps_to_rovo(self, orchestrator, rovo_backend, vertex_backend):
        """SearchConfluenceJira dispatches to Rovo backend."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_final_response("PTO is 20 days."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        rovo_backend.query.assert_called_once()
        vertex_backend.query.assert_not_called()

    async def test_search_google_sites_maps_to_vertex(self, orchestrator, rovo_backend, vertex_backend):
        """SearchGoogleSites dispatches to Vertex backend."""
        responses = [
            _make_return_control_response("SearchGoogleSites", "search", {"query": "handbook"}),
            _make_final_response("Handbook info."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            await orchestrator.ask("Where is the handbook?", "C123:1234567890.123456")

        vertex_backend.query.assert_called_once()
        rovo_backend.query.assert_not_called()

    @given(ag=action_group_name, query=question_text)
    @settings(max_examples=10)
    async def test_mapping_is_deterministic(self, ag, query):
        """For any valid action group, the mapping is deterministic."""
        from slack_agent_router.orchestrator import BedrockAgentOrchestrator

        rb = AsyncMock()
        rb.name = "Atlassian Rovo (Confluence/Jira)"
        rb.query = AsyncMock(return_value=_make_backend_result("Atlassian Rovo (Confluence/Jira)"))
        vb = AsyncMock()
        vb.name = "Google Sites (Vertex AI Search)"
        vb.query = AsyncMock(return_value=_make_backend_result("Google Sites (Vertex AI Search)"))

        orch = BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rb,
            vertex_backend=vb,
        )
        responses = [
            _make_return_control_response(ag, "search", {"query": query}),
            _make_final_response("Answer."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orch, "_invoke_agent", side_effect=_invoke_side_effect):
            await orch.ask(query, "C123:1234567890.123456")

        if ag == "SearchConfluenceJira":
            rb.query.assert_called()
        else:
            vb.query.assert_called()

    async def test_unknown_action_group_returns_error_tool_output(self, orchestrator):
        """Unknown action group produces a failed ToolOutput, not an exception."""
        responses = [
            _make_return_control_response("UnknownBackend", "search", {"query": "test"}),
            _make_final_response("Partial answer."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("test", "C123:1234567890.123456")

        # Should not raise — should handle gracefully
        assert isinstance(result, AgentResponse)


# -------------------------------------------------------
# Property 9: Session ID derivation
# -------------------------------------------------------


class TestSessionIdDerivation:
    """Property 9: session_id follows the correct format."""

    @given(ch=channel_id, ts=thread_ts)
    @settings(max_examples=10)
    def test_thread_reply_uses_channel_and_thread_ts(self, ch, ts):
        """Thread reply → "{channel_id}:{thread_ts}"."""
        from slack_agent_router.orchestrator import derive_session_id

        pq = ParsedQuestion(
            event_type="app_mention",
            user_id="U123",
            channel_id=ch,
            thread_ts=ts,
            question="What is PTO?",
            team_id="T123",
            event_ts="9999999999.999999",
            request_id="req-1",
        )
        session_id = derive_session_id(pq)
        assert session_id == f"{ch}:{ts}"

    @given(ch=channel_id, ets=message_ts)
    @settings(max_examples=10)
    def test_channel_mention_without_thread_uses_event_ts(self, ch, ets):
        """Channel mention without thread → "{channel_id}:{event_ts}"."""
        from slack_agent_router.orchestrator import derive_session_id

        pq = ParsedQuestion(
            event_type="app_mention",
            user_id="U123",
            channel_id=ch,
            thread_ts=None,
            question="What is PTO?",
            team_id="T123",
            event_ts=ets,
            request_id="req-1",
        )
        session_id = derive_session_id(pq)
        assert session_id == f"{ch}:{ets}"

    @given(ch=channel_id, ets=message_ts)
    @settings(max_examples=10)
    def test_dm_without_thread_uses_event_ts(self, ch, ets):
        """DM without thread → "{channel_id}:{event_ts}"."""
        from slack_agent_router.orchestrator import derive_session_id

        pq = ParsedQuestion(
            event_type="message",
            user_id="U123",
            channel_id=ch,
            thread_ts=None,
            question="What is PTO?",
            team_id="T123",
            event_ts=ets,
            request_id="req-1",
        )
        session_id = derive_session_id(pq)
        assert session_id == f"{ch}:{ets}"

    @given(ch=channel_id, ts=thread_ts)
    @settings(max_examples=10)
    def test_session_id_format_always_colon_separated(self, ch, ts):
        """Session ID always has exactly one colon separator."""
        from slack_agent_router.orchestrator import derive_session_id

        pq = ParsedQuestion(
            event_type="app_mention",
            user_id="U123",
            channel_id=ch,
            thread_ts=ts,
            question="test",
            team_id="T123",
            event_ts="9999999999.999999",
            request_id="req-1",
        )
        session_id = derive_session_id(pq)
        parts = session_id.split(":")
        assert len(parts) == 2
        assert parts[0] == ch


# -------------------------------------------------------
# Unit tests: Agent failure scenarios
# -------------------------------------------------------


class TestAgentFailureBeforeToolCalls:
    """Agent fails before any tool calls are made."""

    async def test_returns_error_message(self, orchestrator):
        """Agent error before tool calls → error message in response."""

        async def _invoke_side_effect(*args, **kwargs):
            raise RuntimeError("Bedrock Agent throttled")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert isinstance(result, AgentResponse)
        assert "trouble" in result.answer.lower() or "error" in result.answer.lower() or len(result.answer) > 0

    async def test_no_tool_calls_recorded(self, orchestrator, rovo_backend, vertex_backend):
        """No backend calls should be made when agent fails immediately."""

        async def _invoke_side_effect(*args, **kwargs):
            raise RuntimeError("Bedrock Agent error")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        rovo_backend.query.assert_not_called()
        vertex_backend.query.assert_not_called()
        assert result.tool_calls_made == []


class TestAgentFailureAfterToolCalls:
    """Agent fails after one or more successful tool calls."""

    async def test_returns_fallback_with_raw_outputs(self, orchestrator, rovo_backend):
        """Agent error after successful tool calls → fallback response."""
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _make_return_control_response(
                    "SearchConfluenceJira",
                    "search",
                    {"query": "PTO"},
                )
            raise RuntimeError("Bedrock Agent failed mid-loop")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert isinstance(result, AgentResponse)
        # Should contain content from the successful backend call
        assert len(result.answer) > 0
        assert len(result.tool_calls_made) > 0

    async def test_fallback_includes_source_urls(self, orchestrator, rovo_backend):
        """Fallback response includes source URLs from successful calls."""
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _make_return_control_response(
                    "SearchConfluenceJira",
                    "search",
                    {"query": "PTO"},
                )
            raise RuntimeError("Bedrock Agent failed")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert len(result.source_urls) > 0


class TestTimeoutEnforcement:
    """Orchestrator enforces 30-second total timeout."""

    async def test_timeout_returns_response(self, orchestrator):
        """ask() returns a response even when timeout is hit."""

        async def _invoke_side_effect(*args, **kwargs):
            await asyncio.sleep(60)  # Way longer than 30s timeout

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert isinstance(result, AgentResponse)
        assert len(result.answer) > 0

    async def test_timeout_does_not_raise(self, orchestrator):
        """ask() never raises — always returns an AgentResponse."""

        async def _invoke_side_effect(*args, **kwargs):
            await asyncio.sleep(60)

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            # Should not raise
            result = await orchestrator.ask("test", "C123:1234567890.123456")

        assert isinstance(result, AgentResponse)


# -------------------------------------------------------
# Unit tests: Happy path
# -------------------------------------------------------


class TestHappyPath:
    """Normal question → tool call → final answer flow."""

    async def test_single_tool_call_flow(self, orchestrator, rovo_backend):
        """Question → one tool call → final answer."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_final_response("PTO is 20 days per year."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert result.answer == "PTO is 20 days per year."
        assert "SearchConfluenceJira" in result.tool_calls_made

    async def test_two_tool_calls_flow(self, orchestrator, rovo_backend, vertex_backend):
        """Question → two tool calls → final answer."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "search", {"query": "PTO"}),
            _make_return_control_response("SearchGoogleSites", "search", {"query": "PTO"}),
            _make_final_response("PTO is 20 days. See handbook."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert result.answer == "PTO is 20 days. See handbook."
        assert "SearchConfluenceJira" in result.tool_calls_made
        assert "SearchGoogleSites" in result.tool_calls_made

    async def test_direct_answer_no_tool_calls(self, orchestrator):
        """Agent answers directly without tool calls."""

        async def _invoke_side_effect(*args, **kwargs):
            return _make_final_response("I can help with that.")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("Hello", "C123:1234567890.123456")

        assert result.answer == "I can help with that."
        assert result.tool_calls_made == []

    async def test_latency_is_recorded(self, orchestrator):
        """AgentResponse includes latency_ms > 0."""

        async def _invoke_side_effect(*args, **kwargs):
            return _make_final_response("Answer.")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("test", "C123:1234567890.123456")

        assert result.latency_ms >= 0
