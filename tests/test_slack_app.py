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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.dedup import EventDeduplicator
from slack_agent_router.models import AgentResponse
from slack_agent_router.rate_limiter import RateLimiter
from slack_agent_router.slack_app import (
    _AGENT_FAILURE_MSG,
    _ALL_BACKENDS_FAILED_MSG,
    _PLACEHOLDER_THINKING,
    _REACTION_DONE,
    _REACTION_WORKING,
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
    deduplicator=None,
) -> SlackAgentApp:
    """Build a SlackAgentApp with sensible mock defaults."""
    return SlackAgentApp(
        bot_token=_FAKE_BOT_TOKEN,
        app_token=_FAKE_APP_TOKEN,
        orchestrator=orchestrator or AsyncMock(),
        rate_limiter=rate_limiter or MagicMock(spec=RateLimiter),
        auth_check=auth_check or AsyncMock(return_value=True),
        deduplicator=deduplicator,
    )


# Placeholder timestamp returned by the mock chat_postMessage.
_PLACEHOLDER_TS = "1234500000.000001"


def _make_client() -> AsyncMock:
    """Build a mock Slack Web client whose chat_postMessage returns a ts.

    With a real ``ts``, the progressive-UX path is exercised: a placeholder
    is posted and later updated in place via ``chat_update``.
    """
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": _PLACEHOLDER_TS, "channel": "C99999"}
    return client


def _delivered_text(say: AsyncMock, client: AsyncMock) -> str:
    """Return the answer text delivered to the user, via either path.

    The answer is delivered by updating the placeholder (``chat_update``)
    when one exists, or by ``say`` as a fallback. This helper returns the
    text regardless of which path was taken.

    We check ``say`` first because when ``chat_update`` raises and the
    code falls back to ``say()``, ``chat_update.call_args`` is still set
    from the failed attempt.  Preferring ``say`` ensures we return the
    text that was *actually* delivered.
    """
    if say.call_args is not None:
        return say.call_args.kwargs["text"]
    if client.chat_update.call_args is not None:
        return client.chat_update.call_args.kwargs["text"]
    raise AssertionError("No answer was delivered via chat_update or say")


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

        async def fake_ask(question: str, session_id: str, on_progress=None) -> AgentResponse:
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
        client = _make_client()

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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        orch.ask.assert_called_once()
        assert "Answer" in _delivered_text(say, client)


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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        assert _delivered_text(say, client) == _AGENT_FAILURE_MSG

    @pytest.mark.asyncio
    async def test_all_backends_failed_posts_specific_message(self, mock_auth_check) -> None:
        """When orchestrator returns failed=True, post all-backends-failed message."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="Some error from orchestrator",
                source_urls=[],
                tool_calls_made=[],
                latency_ms=500.0,
                failed=True,
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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        assert _delivered_text(say, client) == _ALL_BACKENDS_FAILED_MSG

    @pytest.mark.asyncio
    async def test_successful_tool_calls_with_failure_answer_not_treated_as_all_failed(self, mock_auth_check) -> None:
        """When orchestrator returns failed=False with tool calls, it's not all-backends-failed."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(
                answer="Partial answer from fallback",
                source_urls=[],
                tool_calls_made=["SearchConfluenceJira"],
                latency_ms=500.0,
                failed=False,
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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        posted_text = _delivered_text(say, client)
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

        async def flaky_call() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise error_429

        with patch("slack_agent_router.slack_app.asyncio.sleep", new_callable=AsyncMock):
            await SlackAgentApp._slack_call_with_retry(flaky_call)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_non_429_error_raises_immediately(self) -> None:
        """A non-429 error should raise immediately without retry."""

        async def failing_call() -> None:
            raise ValueError("something else")

        with pytest.raises(ValueError, match="something else"):
            await SlackAgentApp._slack_call_with_retry(failing_call)

    @pytest.mark.asyncio
    async def test_429_exhausts_retries(self) -> None:
        """After exhausting retries, the 429 error is re-raised."""
        error_429 = Exception("rate_limited")
        error_429.response = MagicMock(status_code=429, headers={"Retry-After": "0"})

        call_count = 0

        async def always_429() -> None:
            nonlocal call_count
            call_count += 1
            raise error_429

        with patch("slack_agent_router.slack_app.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception, match="rate_limited"):
                await SlackAgentApp._slack_call_with_retry(always_429)

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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        # The placeholder is posted in-thread; the answer updates it in place.
        client.chat_postMessage.assert_called_once()
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "1234567890.123456"
        answer_text = _delivered_text(say, client)
        assert "PTO is 20 days" in answer_text
        assert "https://example.com/pto" in answer_text

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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        # The placeholder is posted into the existing thread.
        client.chat_postMessage.assert_called_once()
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "1234567890.123456"


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
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        # The fallback text should be included in the delivered output
        assert "I had trouble synthesizing" in _delivered_text(say, client)


# -------------------------------------------------------
# Unit tests: Event deduplication (task 12.1)
# -------------------------------------------------------


def _mention_event() -> dict[str, Any]:
    """A representative app_mention event with a stable event_id."""
    return {
        "type": "app_mention",
        "event_id": "Ev123ABC",
        "user": "U12345",
        "text": "<@UBOTID> What is PTO?",
        "channel": "C99999",
        "ts": "1234567890.123456",
        "team": "T00001",
        "event_ts": "1234567890.123456",
    }


class TestEventDeduplication:
    """Requirements 1.5, 1.6: duplicate events/commands are skipped silently."""

    @pytest.mark.asyncio
    async def test_duplicate_event_skipped_silently(self, mock_auth_check) -> None:
        """The same event delivered twice only reaches the orchestrator once."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="PTO is 20 days.", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(
            orchestrator=orch,
            rate_limiter=limiter,
            auth_check=mock_auth_check,
            deduplicator=EventDeduplicator(),
        )
        event = _mention_event()
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")
        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        # Only the first delivery is processed.
        orch.ask.assert_called_once()
        # The answer is delivered exactly once (via the placeholder update).
        client.chat_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_runs_before_auth_and_rate_limit(self, mock_auth_check) -> None:
        """A duplicate is dropped before auth and rate-limit checks run."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="A", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        auth_check = AsyncMock(return_value=True)
        app = _make_app(
            orchestrator=orch,
            rate_limiter=limiter,
            auth_check=auth_check,
            deduplicator=EventDeduplicator(),
        )
        event = _mention_event()
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")
        # Reset call counts; the second (duplicate) delivery must not
        # invoke auth or the rate limiter at all.
        auth_check.reset_mock()
        limiter.check.reset_mock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        auth_check.assert_not_called()
        limiter.check.assert_not_called()

    @pytest.mark.asyncio
    async def test_distinct_events_both_processed(self, mock_auth_check) -> None:
        """Two different events are both processed."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="A", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(
            orchestrator=orch,
            rate_limiter=limiter,
            auth_check=mock_auth_check,
            deduplicator=EventDeduplicator(),
        )
        first = _mention_event()
        second = dict(first, event_id="Ev999ZZZ", event_ts="1234567891.000000", ts="1234567891.000000")
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(first, say=say, client=client, bot_user_id="UBOTID")
        await app.handle_event(second, say=say, client=client, bot_user_id="UBOTID")

        assert orch.ask.call_count == 2

    @pytest.mark.asyncio
    async def test_event_dedup_falls_back_to_channel_ts(self, mock_auth_check) -> None:
        """Events without an event_id dedupe on the channel:event_ts composite."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="A", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(
            orchestrator=orch,
            rate_limiter=limiter,
            auth_check=mock_auth_check,
            deduplicator=EventDeduplicator(),
        )
        event = _mention_event()
        del event["event_id"]
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")
        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        orch.ask.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_deduplicator_processes_all(self, mock_auth_check) -> None:
        """With no deduplicator configured, duplicates are still processed."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="A", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check, deduplicator=None)
        event = _mention_event()
        say = AsyncMock()
        client = AsyncMock()

        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")
        await app.handle_event(event, say=say, client=client, bot_user_id="UBOTID")

        assert orch.ask.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_slash_command_skipped_after_ack(self, mock_auth_check) -> None:
        """A retried slash command (same trigger_id) is acked but processed once."""
        orch = AsyncMock()
        orch.ask = AsyncMock(
            return_value=AgentResponse(answer="A", source_urls=[], tool_calls_made=["t"], latency_ms=10.0)
        )
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(
            orchestrator=orch,
            rate_limiter=limiter,
            auth_check=mock_auth_check,
            deduplicator=EventDeduplicator(),
        )
        command = {
            "command": "/sage-ask",
            "text": "What is PTO?",
            "user_id": "U12345",
            "channel_id": "C99999",
            "team_id": "T00001",
            "trigger_id": "trigger_abc123",
        }
        ack = AsyncMock()
        say = AsyncMock()
        client = _make_client()

        await app._handle_slash_command(ack, command, say, client)
        await app._handle_slash_command(ack, command, say, client)

        # Both deliveries are acknowledged, but only one is processed.
        assert ack.call_count == 2
        orch.ask.assert_called_once()
        # The answer is delivered exactly once (via the placeholder update).
        client.chat_update.assert_called_once()


# -------------------------------------------------------
# Unit tests: Progressive UX feedback (task 12.3)
# -------------------------------------------------------


def _pto_event() -> dict[str, Any]:
    """A representative app_mention event."""
    return {
        "type": "app_mention",
        "user": "U12345",
        "text": "<@UBOTID> What is PTO?",
        "channel": "C99999",
        "ts": "1234567890.123456",
        "team": "T00001",
        "event_ts": "1234567890.123456",
    }


def _answer_response() -> AgentResponse:
    return AgentResponse(
        answer="PTO is 20 days.",
        source_urls=[],
        tool_calls_made=["SearchConfluenceJira"],
        latency_ms=100.0,
    )


class TestProgressiveUX:
    """Requirement 4: reactions and placeholder-message progress feedback."""

    @pytest.mark.asyncio
    async def test_working_reaction_added_on_receipt(self, mock_auth_check) -> None:
        """Requirement 4.1: a 👀 reaction is added to the user's message."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        client.reactions_add.assert_any_call(channel="C99999", timestamp="1234567890.123456", name=_REACTION_WORKING)

    @pytest.mark.asyncio
    async def test_thinking_placeholder_posted_in_thread(self, mock_auth_check) -> None:
        """Requirement 4.2: a "⏳ Thinking..." placeholder is posted in the thread."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["text"] == _PLACEHOLDER_THINKING
        assert kwargs["thread_ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_placeholder_updated_as_backend_searched(self, mock_auth_check) -> None:
        """Requirement 4.3: the placeholder is updated to show the backend being searched."""

        # The orchestrator fires on_progress with the action group name.
        async def fake_ask(question: str, session_id: str, on_progress=None) -> AgentResponse:
            if on_progress is not None:
                await on_progress("SearchConfluenceJira")
            return _answer_response()

        orch = AsyncMock()
        orch.ask = AsyncMock(side_effect=fake_ask)
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        # One update for progress ("Searching...") and one for the final answer.
        update_texts = [c.kwargs["text"] for c in client.chat_update.call_args_list]
        assert any("Searching Confluence and Jira" in t for t in update_texts)

    @pytest.mark.asyncio
    async def test_final_answer_updates_placeholder(self, mock_auth_check) -> None:
        """Requirement 4.4: the final answer updates the placeholder via chat.update."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        # The last chat_update carries the formatted answer, targeting the placeholder ts.
        last_update = client.chat_update.call_args
        assert last_update.kwargs["ts"] == _PLACEHOLDER_TS
        assert "PTO is 20 days" in last_update.kwargs["text"]
        # The answer is delivered in place, not as a fresh say() message.
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_working_reaction_swapped_for_done(self, mock_auth_check) -> None:
        """Requirement 4.5: 👀 is removed and ✅ is added when the answer is posted."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        client.reactions_remove.assert_called_once_with(
            channel="C99999", timestamp="1234567890.123456", name=_REACTION_WORKING
        )
        client.reactions_add.assert_any_call(channel="C99999", timestamp="1234567890.123456", name=_REACTION_DONE)

    @pytest.mark.asyncio
    async def test_slash_command_skips_reactions(self, mock_auth_check) -> None:
        """Slash commands have no message ts, so no reactions are added."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        command = {
            "command": "/sage-ask",
            "text": "What is PTO?",
            "user_id": "U12345",
            "channel_id": "C99999",
            "team_id": "T00001",
            "trigger_id": "trigger_abc123",
        }
        ack = AsyncMock()
        say = AsyncMock()
        client = _make_client()

        await app._handle_slash_command(ack, command, say, client)

        client.reactions_add.assert_not_called()
        client.reactions_remove.assert_not_called()
        # But the placeholder + answer flow still runs.
        client.chat_postMessage.assert_called_once()
        client.chat_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_say_when_placeholder_fails(self, mock_auth_check) -> None:
        """If posting the placeholder fails, the answer is posted via say()."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()
        # Placeholder post fails outright.
        client.chat_postMessage.side_effect = RuntimeError("cannot post")

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        # No placeholder ts → no chat_update; answer falls back to say().
        client.chat_update.assert_not_called()
        say.assert_called_once()
        assert "PTO is 20 days" in say.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_reaction_failure_does_not_block_answer(self, mock_auth_check) -> None:
        """A failing reactions_add must not prevent the answer from being delivered."""
        orch = AsyncMock()
        orch.ask = AsyncMock(return_value=_answer_response())
        limiter = MagicMock(spec=RateLimiter)
        limiter.check.return_value = (True, None)
        app = _make_app(orchestrator=orch, rate_limiter=limiter, auth_check=mock_auth_check)
        say = AsyncMock()
        client = _make_client()
        client.reactions_add.side_effect = RuntimeError("reaction denied")

        await app.handle_event(_pto_event(), say=say, client=client, bot_user_id="UBOTID")

        # Answer still delivered despite reaction failures.
        assert "PTO is 20 days" in _delivered_text(say, client)
