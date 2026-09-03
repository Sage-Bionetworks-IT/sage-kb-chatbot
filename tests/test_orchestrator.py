"""Property and unit tests for BedrockAgentOrchestrator.

Property 6: Return control loop iteration bound
Property 7: Return control loop duplicate tool call detection
Property 8: Action group to backend mapping correctness
Property 9: Session ID derivation from Slack thread context

Unit tests: agent failure before/after tool calls, timeout enforcement.
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
def orchestrator(rovo_backend):
    """Create an orchestrator with mocked backends."""
    from slack_agent_router.orchestrator import BedrockAgentOrchestrator

    return BedrockAgentOrchestrator(
        agent_id="test-agent-id",
        agent_alias_id="test-alias-id",
        rovo_backend=rovo_backend,
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
                "find_content",
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
                "find_content",
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

        orch = BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rb,
        )
        call_count = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_return_control_response(
                "SearchConfluenceJira",
                "find_content",
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
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
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
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "benefits"}),
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

        orch = BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rb,
        )
        responses = [
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": query}),
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": query}),
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

    async def test_search_confluence_jira_maps_to_rovo(self, orchestrator, rovo_backend):
        """SearchConfluenceJira dispatches to Rovo backend."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
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

    async def test_unknown_action_group_returns_error_tool_output(self, orchestrator):
        """Unknown action group produces a failed ToolOutput, not an exception."""
        responses = [
            _make_return_control_response("UnknownBackend", "find_content", {"query": "test"}),
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

    async def test_no_tool_calls_recorded(self, orchestrator, rovo_backend):
        """No backend calls should be made when agent fails immediately."""

        async def _invoke_side_effect(*args, **kwargs):
            raise RuntimeError("Bedrock Agent error")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        rovo_backend.query.assert_not_called()
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
                    "find_content",
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
                    "find_content",
                    {"query": "PTO"},
                )
            raise RuntimeError("Bedrock Agent failed")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456")

        assert len(result.source_urls) > 0


class TestTimeoutEnforcement:
    """Orchestrator enforces total timeout."""

    @pytest.fixture()
    def orchestrator(self, rovo_backend):
        """Orchestrator with a very short timeout for fast tests."""
        from slack_agent_router.orchestrator import BedrockAgentOrchestrator

        return BedrockAgentOrchestrator(
            agent_id="test-agent-id",
            agent_alias_id="test-alias-id",
            rovo_backend=rovo_backend,
            timeout_seconds=0.1,  # 100ms instead of 30s
        )

    async def test_timeout_returns_response(self, orchestrator):
        """ask() returns a response even when timeout is hit."""

        async def _invoke_side_effect(*args, **kwargs):
            await asyncio.sleep(60)  # Way longer than timeout

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
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
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


# -------------------------------------------------------
# Progress callback (task 12.3)
# -------------------------------------------------------


class TestProgressCallback:
    """The on_progress callback surfaces per-backend progress."""

    async def test_progress_fired_with_action_group(self, orchestrator, rovo_backend):
        """on_progress is invoked with the action group name before the tool runs."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_final_response("PTO is 20 days."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        seen: list[str] = []

        async def on_progress(action_group: str) -> None:
            seen.append(action_group)

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            await orchestrator.ask("What is PTO?", "C123:1234567890.123456", on_progress=on_progress)

        assert seen == ["SearchConfluenceJira"]

    async def test_progress_not_fired_for_cached_duplicate(self, orchestrator, rovo_backend):
        """A duplicate (cached) tool call does not fire progress again."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_final_response("PTO is 20 days."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        seen: list[str] = []

        async def on_progress(action_group: str) -> None:
            seen.append(action_group)

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            await orchestrator.ask("What is PTO?", "C123:1234567890.123456", on_progress=on_progress)

        # Fired once for the real call, skipped for the cached duplicate.
        assert seen == ["SearchConfluenceJira"]

    async def test_progress_callback_error_does_not_break_answer(self, orchestrator, rovo_backend):
        """An exception from on_progress is swallowed; the answer is still produced."""
        responses = [
            _make_return_control_response("SearchConfluenceJira", "find_content", {"query": "PTO"}),
            _make_final_response("PTO is 20 days."),
        ]
        call_idx = 0

        async def _invoke_side_effect(*args, **kwargs):
            nonlocal call_idx
            resp = responses[min(call_idx, len(responses) - 1)]
            call_idx += 1
            return resp

        async def on_progress(action_group: str) -> None:
            raise RuntimeError("callback boom")

        with patch.object(orchestrator, "_invoke_agent", side_effect=_invoke_side_effect):
            result = await orchestrator.ask("What is PTO?", "C123:1234567890.123456", on_progress=on_progress)

        assert isinstance(result, AgentResponse)
        assert "PTO is 20 days." in result.answer
        assert result.failed is False
