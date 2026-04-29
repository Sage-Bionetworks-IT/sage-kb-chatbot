"""Tests for SlackAgentApp.

Property 2: Bot mention prefix stripping — for any text with bot mention
            prefix, the extracted question does not contain the prefix
            and preserves the rest.

Unit tests: event parsing for app_mention, DM, and slash command; empty
question rejection with ephemeral message; unauthorized user receives
ephemeral rejection; rate-limited user receives ephemeral message;
rate limiter acquire/release bracketing; orchestrator error handling;
Slack 429 retry with exponential backoff; thread reply posting;
agent failure fallback.

Validates: Requirements 1.1, 1.2, 1.3, 2.2, 3.7, 9.1, 9.2, 10.3, 10.4, 10.5, 10.6, 10.7
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.models import AgentResponse
from slack_agent_router.rate_limiter import RateLimiter
from slack_agent_router.slack_app import (
    _AGENT_FAILURE_MSG,
    _ALL_BACKENDS_FAILED_MSG,
    SlackAgentApp,
    _extract_retry_after,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

bot_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "N"), min_codepoint=48, max_codepoint=90),
    min_size=9,
    max_size=11,
).map(lambda s: f"U{s}")

plain_question = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Zs"), min_codepoint=32, max_codepoint=122),
    min_size=1,
    max_size=120,
).filter(lambda s: s.strip() != "")

# Placeholder token values used only in tests — not real credentials.
_FAKE_BOT_TOKEN = "xoxb-fake-test-placeholder"
_FAKE_APP_TOKEN = "xapp-fake-test-placeholder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    orchestrator=None,
    rate_limiter=None,
    auth_check=None,
) -> SlackAgentApp:
    """Build a SlackAgentApp with sensible mock defaults."""
    return SlackAgentApp(
        bot_token=_FAKE_BOT_TOKEN,
        app_token=_FAKE_APP_TOKEN,
        orchestrator=orchestrator or AsyncMock(),
        rate_limiter=rate_limiter or MagicMock(spec=RateLimiter),
        auth_check=auth_check or AsyncMock(return_value=True),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_orchestrator() -> AsyncMock:
    """A mock BedrockAgentOrchestrator."""
    orch = AsyncMock()
    orch.ask = AsyncMock()
    return orch


@pytest.fixture()
def mock_rate_limiter() -> MagicMock:
    """A mock RateLimiter that allows all requests by default."""
    limiter = MagicMock(spec=RateLimiter)
    limiter.check.return_value = (True, None)
    return limiter


@pytest.fixture()
def mock_auth_check() -> AsyncMock:
    """A mock authorization check that authorizes all users by default."""
    return AsyncMock(return_value=True)


@pytest.fixture()
def slack_app(mock_orchestrator, mock_rate_limiter, mock_auth_check) -> SlackAgentApp:
    """Create a SlackAgentApp with mocked dependencies."""
    return SlackAgentApp(
        bot_token=_FAKE_BOT_TOKEN,
        app_token=_FAKE_APP_TOKEN,
        orchestrator=mock_orchestrator,
        rate_limiter=mock_rate_limiter,
        auth_check=mock_auth_check,
    )


# -------------------------------------------------------
# Property 2: Bot mention prefix stripping
# -------------------------------------------------------


class TestBotMentionStripping:
    """Property 2: bot mention prefix is stripped, rest preserved."""

    @given(bid=bot_id, question=plain_question)
    @settings(max_examples=30)
    def test_mention_prefix_removed(self, bid: str, question: str) -> None:
        """Text with <@BOT_ID> prefix should have the prefix stripped."""
        text = f"<@{bid}> {question}"
        result = SlackAgentApp.strip_bot_mention(text, bid)
        assert f"<@{bid}>" not in result

    @given(bid=bot_id, question=plain_question)
    @settings(max_examples=30)
    def test_question_text_preserved(self, bid: str, question: str) -> None:
        """The question text after the mention should be preserved."""
        text = f"<@{bid}> {question}"
        result = SlackAgentApp.strip_bot_mention(text, bid)
        assert result.strip() == question.strip()

    @given(question=plain_question)
    @settings(max_examples=20)
    def test_text_without_mention_unchanged(self, question: str) -> None:
        """Text without a bot mention should be returned unchanged."""
        result = SlackAgentApp.strip_bot_mention(question, "UBOTID1234")
        assert result.strip() == question.strip()

    def test_mention_at_start_only(self) -> None:
        """Only the leading mention is stripped, not mentions mid-text."""
        text = "<@UBOT123> hello <@UOTHER> world"
        result = SlackAgentApp.strip_bot_mention(text, "UBOT123")
        assert "<@UBOT123>" not in result
        assert "<@UOTHER>" in result
        assert "hello" in result
        assert "world" in result


# -------------------------------------------------------
# Unit tests: Event parsing
# -------------------------------------------------------


class TestEventParsing:
    """Unit tests for parsing Slack events into ParsedQuestion."""

    def test_app_mention_parsed_correctly(self, slack_app: SlackAgentApp) -> None:
        """app_mention event is parsed into a ParsedQuestion with bot mention stripped."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is our PTO policy?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        parsed = slack_app.parse_event(event, bot_user_id="UBOTID")
        assert parsed.event_type == "app_mention"
        assert parsed.user_id == "U12345"
        assert parsed.channel_id == "C99999"
        assert "<@UBOTID>" not in parsed.question
        assert "PTO policy" in parsed.question
        assert parsed.team_id == "T00001"

    def test_dm_event_parsed_correctly(self, slack_app: SlackAgentApp) -> None:
        """Direct message event is parsed into a ParsedQuestion."""
        event = {
            "type": "message",
            "channel_type": "im",
            "user": "U12345",
            "text": "What is our PTO policy?",
            "channel": "D99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        parsed = slack_app.parse_event(event, bot_user_id="UBOTID")
        assert parsed.event_type == "message"
        assert parsed.user_id == "U12345"
        assert parsed.channel_id == "D99999"
        assert parsed.question == "What is our PTO policy?"

    def test_slash_command_parsed_correctly(self, slack_app: SlackAgentApp) -> None:
        """Slash command payload is parsed into a ParsedQuestion."""
        command = {
            "command": "/sage-ask",
            "text": "What is our PTO policy?",
            "user_id": "U12345",
            "channel_id": "C99999",
            "team_id": "T00001",
            "trigger_id": "trigger_abc123",
        }
        parsed = slack_app.parse_command(command)
        assert parsed.event_type == "slash_command"
        assert parsed.user_id == "U12345"
        assert parsed.channel_id == "C99999"
        assert parsed.question == "What is our PTO policy?"
        assert parsed.team_id == "T00001"

    def test_thread_reply_preserves_thread_ts(self, slack_app: SlackAgentApp) -> None:
        """Thread reply event preserves thread_ts for session continuity."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> follow-up question",
            "channel": "C99999",
            "ts": "1234567891.000000",
            "thread_ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567891.000000",
        }
        parsed = slack_app.parse_event(event, bot_user_id="UBOTID")
        assert parsed.thread_ts == "1234567890.123456"


# -------------------------------------------------------
# Unit tests: Empty question rejection
# -------------------------------------------------------


class TestEmptyQuestionRejection:
    """Requirement 10.5: empty question gets ephemeral hint."""

    @pytest.mark.asyncio
    async def test_empty_mention_sends_ephemeral_hint(self, slack_app: SlackAgentApp) -> None:
        """Mentioning the bot with no question text triggers an ephemeral hint."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID>",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await slack_app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        client.chat_postEphemeral.assert_called_once()
        call_kwargs = client.chat_postEphemeral.call_args.kwargs
        assert call_kwargs["channel"] == "C99999"
        assert call_kwargs["user"] == "U12345"
        assert "Try asking me something" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_whitespace_only_mention_sends_ephemeral_hint(self, slack_app: SlackAgentApp) -> None:
        """Mentioning the bot with only whitespace triggers an ephemeral hint."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID>   ",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await slack_app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        client.chat_postEphemeral.assert_called_once()
        assert "Try asking me something" in client.chat_postEphemeral.call_args.kwargs["text"]


# -------------------------------------------------------
# Unit tests: Authorization rejection
# -------------------------------------------------------


class TestAuthorizationRejection:
    """Requirement 2.2: unauthorized user receives ephemeral rejection."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_ephemeral_rejection(self, mock_orchestrator, mock_rate_limiter) -> None:
        """A user not in the authorized group gets an ephemeral rejection message."""
        app = _make_app(
            orchestrator=mock_orchestrator,
            rate_limiter=mock_rate_limiter,
            auth_check=AsyncMock(return_value=False),
        )
        event = {
            "type": "app_mention",
            "user": "U_EXTERNAL",
            "text": "<@UBOTID> What is our PTO policy?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        client.chat_postEphemeral.assert_called_once()
        call_kwargs = client.chat_postEphemeral.call_args.kwargs
        assert "only available to Sage staff" in call_kwargs["text"]
        mock_orchestrator.ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_user_is_not_rejected(self, slack_app: SlackAgentApp, mock_orchestrator) -> None:
        """An authorized user's question proceeds to the orchestrator."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is our PTO policy?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()
        mock_orchestrator.ask.return_value = MagicMock(
            answer="PTO is 20 days.", source_urls=[], tool_calls_made=[], latency_ms=100.0
        )

        await slack_app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        mock_orchestrator.ask.assert_called_once()


# -------------------------------------------------------
# Unit tests: Rate limiting rejection
# -------------------------------------------------------


class TestRateLimitRejection:
    """Requirement 3.7: rate-limited user receives ephemeral message."""

    @pytest.mark.asyncio
    async def test_rate_limited_user_gets_ephemeral_message(self, mock_orchestrator, mock_auth_check) -> None:
        """A rate-limited user gets an ephemeral message and no backend calls."""
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (False, "You've reached the per-minute request limit.")
        app = _make_app(
            orchestrator=mock_orchestrator,
            rate_limiter=limiter,
            auth_check=mock_auth_check,
        )
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is our PTO policy?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        client.chat_postEphemeral.assert_called_once()
        call_kwargs = client.chat_postEphemeral.call_args.kwargs
        assert "per-minute" in call_kwargs["text"]
        mock_orchestrator.ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_rate_limited_user_proceeds(self, slack_app: SlackAgentApp, mock_orchestrator) -> None:
        """A user within rate limits proceeds to the orchestrator."""
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is our PTO policy?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()
        mock_orchestrator.ask.return_value = MagicMock(
            answer="PTO is 20 days.", source_urls=[], tool_calls_made=[], latency_ms=100.0
        )

        await slack_app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        mock_orchestrator.ask.assert_called_once()


# -------------------------------------------------------
# Unit tests: Rate limiter acquire/release integration
# -------------------------------------------------------


class TestRateLimiterAcquireRelease:
    """Requirement 3.7: rate limiter acquire/release brackets orchestrator call."""

    @pytest.mark.asyncio
    async def test_acquire_called_before_orchestrator(self, mock_auth_check) -> None:
        """Rate limiter acquire() is called before the orchestrator runs."""
        call_order: list[str] = []

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        limiter.acquire.side_effect = lambda uid: call_order.append("acquire")
        limiter.release.side_effect = lambda uid: call_order.append("release")

        async def fake_ask(question: str, session_id: str) -> AgentResponse:
            call_order.append("ask")
            return AgentResponse(answer="Answer", source_urls=[], tool_calls_made=["tool"], latency_ms=100.0)

        orch = AsyncMock()
        orch.ask = AsyncMock(side_effect=fake_ask)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> question",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        assert call_order == ["acquire", "ask", "release"]

    @pytest.mark.asyncio
    async def test_release_called_even_on_orchestrator_exception(self, mock_auth_check) -> None:
        """Rate limiter release() is called even when the orchestrator raises."""
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        orch = AsyncMock()
        orch.ask = AsyncMock(side_effect=RuntimeError("boom"))

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> question",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        limiter.acquire.assert_called_once_with("U12345")
        limiter.release.assert_called_once_with("U12345")

    @pytest.mark.asyncio
    async def test_no_acquire_when_rate_limiter_is_none(self, mock_auth_check) -> None:
        """When no rate limiter is configured, acquire/release are not called."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="Answer", source_urls=[], tool_calls_made=["tool"], latency_ms=100.0)
        )

        # Construct directly to pass rate_limiter=None (bypassing _make_app defaults)
        app = SlackAgentApp(
            bot_token=_FAKE_BOT_TOKEN,
            app_token=_FAKE_APP_TOKEN,
            orchestrator=orch,
            rate_limiter=None,
            auth_check=mock_auth_check,
        )
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> question",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        orch.ask.assert_called_once()
        say.assert_called_once()


# -------------------------------------------------------
# Unit tests: Orchestrator error handling
# -------------------------------------------------------


class TestOrchestratorErrorHandling:
    """Requirements 10.3, 10.6: error handling for orchestrator failures."""

    @pytest.mark.asyncio
    async def test_orchestrator_exception_posts_agent_failure_message(self, mock_auth_check) -> None:
        """When the orchestrator raises, the agent failure message is posted."""
        orch = AsyncMock()
        orch.ask = AsyncMock(side_effect=RuntimeError("Bedrock exploded"))

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is PTO?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        posted_text = say.call_args.kwargs["text"]
        assert posted_text == _AGENT_FAILURE_MSG

    @pytest.mark.asyncio
    async def test_all_backends_failed_posts_specific_message(self, mock_auth_check) -> None:
        """When orchestrator returns no tool calls and no sources, post all-backends-failed message."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="Some error from orchestrator",
                source_urls=[],
                tool_calls_made=[],
                latency_ms=500.0,
            )
        )

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is PTO?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        posted_text = say.call_args.kwargs["text"]
        assert posted_text == _ALL_BACKENDS_FAILED_MSG

    @pytest.mark.asyncio
    async def test_successful_tool_calls_with_failure_answer_not_treated_as_all_failed(self, mock_auth_check) -> None:
        """When orchestrator made tool calls, even with no sources, it's not all-backends-failed."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="Partial answer from fallback",
                source_urls=[],
                tool_calls_made=["SearchConfluenceJira"],
                latency_ms=500.0,
            )
        )

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is PTO?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        posted_text = say.call_args.kwargs["text"]
        # Should be formatted normally, not the all-backends-failed message
        assert posted_text != _ALL_BACKENDS_FAILED_MSG


# -------------------------------------------------------
# Unit tests: Slack 429 retry
# -------------------------------------------------------


class TestSlack429Retry:
    """Requirement 10.4: Slack 429 retry with exponential backoff."""

    @pytest.mark.asyncio
    async def test_retry_on_429_then_succeed(self) -> None:
        """A 429 error followed by success should post the message."""
        error_429 = Exception("rate_limited")
        error_429.response = MagicMock(status_code=429, headers={"Retry-After": "0"})

        call_count = 0

        async def flaky_say(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise error_429

        await SlackAgentApp._post_with_retry(flaky_say, text="hello", thread_ts="123")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_non_429_error_raises_immediately(self) -> None:
        """A non-429 error should raise immediately without retry."""

        async def failing_say(**kwargs: Any) -> None:
            raise ValueError("something else")

        with pytest.raises(ValueError, match="something else"):
            await SlackAgentApp._post_with_retry(failing_say, text="hello")

    @pytest.mark.asyncio
    async def test_429_exhausts_retries(self) -> None:
        """After exhausting retries, the 429 error is re-raised."""
        error_429 = Exception("rate_limited")
        error_429.response = MagicMock(status_code=429, headers={"Retry-After": "0"})

        call_count = 0

        async def always_429(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise error_429

        with pytest.raises(Exception, match="rate_limited"):
            await SlackAgentApp._post_with_retry(always_429, text="hello")

        # 1 initial + 3 retries = 4 total
        assert call_count == 4

    def test_extract_retry_after_from_slack_api_error(self) -> None:
        """_extract_retry_after reads Retry-After from a SlackApiError-like exception."""
        exc = Exception("rate_limited")
        exc.response = MagicMock(status_code=429, headers={"Retry-After": "5"})
        assert _extract_retry_after(exc) == 5.0

    def test_extract_retry_after_returns_none_for_non_429(self) -> None:
        """_extract_retry_after returns None for non-429 errors."""
        exc = Exception("server error")
        exc.response = MagicMock(status_code=500, headers={})
        assert _extract_retry_after(exc) is None

    def test_extract_retry_after_returns_none_for_plain_exception(self) -> None:
        """_extract_retry_after returns None for exceptions without response."""
        exc = ValueError("plain error")
        assert _extract_retry_after(exc) is None

    def test_extract_retry_after_fallback_status_attribute(self) -> None:
        """_extract_retry_after checks .status attribute as fallback."""
        exc = Exception("rate limited")
        exc.status = 429
        assert _extract_retry_after(exc) is not None


# -------------------------------------------------------
# Unit tests: Thread reply posting
# -------------------------------------------------------


class TestThreadReplyPosting:
    """Requirement 9.1, 9.2: answers posted as thread replies."""

    @pytest.mark.asyncio
    async def test_answer_posted_as_thread_reply(self, mock_auth_check) -> None:
        """The answer is posted as a thread reply using the correct thread_ts."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="PTO is 20 days.",
                source_urls=["https://example.com/pto"],
                tool_calls_made=["SearchConfluenceJira"],
                latency_ms=5100.0,
            )
        )

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is PTO?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        call_kwargs = say.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234567890.123456"
        assert "PTO is 20 days" in call_kwargs["text"]
        assert "https://example.com/pto" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_thread_reply_uses_thread_ts_when_present(self, mock_auth_check) -> None:
        """When replying in an existing thread, thread_ts is used."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="Follow-up answer.",
                source_urls=[],
                tool_calls_made=["SearchConfluenceJira"],
                latency_ms=100.0,
            )
        )

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> follow-up",
            "channel": "C99999",
            "ts": "1234567891.000000",
            "thread_ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567891.000000",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        assert say.call_args.kwargs["thread_ts"] == "1234567890.123456"


# -------------------------------------------------------
# Unit tests: Agent failure fallback
# -------------------------------------------------------


class TestAgentFailureFallback:
    """Requirement 10.7: agent failure after tool calls returns fallback."""

    @pytest.mark.asyncio
    async def test_fallback_response_posted_when_agent_fails_after_tool_calls(self, mock_auth_check) -> None:
        """When the orchestrator returns a fallback response, it's posted as-is."""
        fallback_text = (
            "I had trouble synthesizing a complete answer, "
            "but here's what I found from each source:\n\n"
            "PTO is 20 days.\n<https://example.com/pto|PTO Policy> (Confluence)"
        )
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer=fallback_text,
                source_urls=["https://example.com/pto"],
                tool_calls_made=["SearchConfluenceJira"],
                latency_ms=8000.0,
            )
        )

        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)

        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        event = {
            "type": "app_mention",
            "user": "U12345",
            "text": "<@UBOTID> What is PTO?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "team": "T00001",
            "event_ts": "1234567890.123456",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        say.assert_called_once()
        posted_text = say.call_args.kwargs["text"]
        # The fallback text should be included in the formatted output
        assert "I had trouble synthesizing" in posted_text
