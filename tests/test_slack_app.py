"""Tests for SlackAgentApp (RED).

Property 1: Event deduplication prevents reprocessing — submitting the
            same event ID within 60s returns duplicate; unseen IDs are
            accepted.
Property 2: Bot mention prefix stripping — for any text with bot mention
            prefix, the extracted question does not contain the prefix
            and preserves the rest.

Unit tests: event parsing for app_mention, DM, and slash command; empty
question rejection with ephemeral message; unauthorized user receives
ephemeral rejection; rate-limited user receives ephemeral message.

These tests should FAIL until tasks 9.2–9.6 implement the SlackAgentApp.

Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 2.2, 3.7, 10.5
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Lazy import — SlackAgentApp doesn't exist yet (RED phase).
# Import at module level so tests fail with ImportError, confirming RED.
# ---------------------------------------------------------------------------
from slack_agent_router.slack_app import SlackAgentApp

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

event_id = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=48, max_codepoint=122),
    min_size=10,
    max_size=20,
)

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
    clock=None,
) -> SlackAgentApp:
    """Build a SlackAgentApp with sensible mock defaults."""
    return SlackAgentApp(
        bot_token=_FAKE_BOT_TOKEN,
        app_token=_FAKE_APP_TOKEN,
        orchestrator=orchestrator or AsyncMock(),
        rate_limiter=rate_limiter or MagicMock(spec=RateLimiter),
        auth_check=auth_check or AsyncMock(return_value=True),
        clock=clock,
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
# Property 1: Event deduplication prevents reprocessing
# -------------------------------------------------------


class TestEventDeduplication:
    """Property 1: duplicate event IDs are rejected; unseen IDs accepted."""

    @given(eid=event_id)
    @settings(max_examples=30)
    def test_first_submission_is_accepted(self, eid: str) -> None:
        """An unseen event ID should be accepted (not duplicate)."""
        app = _make_app()
        assert app.is_duplicate(eid) is False

    @given(eid=event_id)
    @settings(max_examples=30)
    def test_second_submission_within_ttl_is_duplicate(self, eid: str) -> None:
        """The same event ID submitted again within 60s is a duplicate."""
        app = _make_app()
        app.is_duplicate(eid)
        assert app.is_duplicate(eid) is True

    @given(eid=event_id)
    @settings(max_examples=10)
    def test_submission_after_ttl_expires_is_accepted(self, eid: str) -> None:
        """After the 60s TTL expires, the same event ID is accepted again."""
        frozen = time.monotonic()
        clock = MagicMock(side_effect=[frozen, frozen, frozen + 61.0])
        app = _make_app(clock=clock)
        assert app.is_duplicate(eid) is False
        assert app.is_duplicate(eid) is True
        assert app.is_duplicate(eid) is False

    @given(ids=st.lists(event_id, min_size=2, max_size=10, unique=True))
    @settings(max_examples=20)
    def test_distinct_ids_are_all_accepted(self, ids: list[str]) -> None:
        """Different event IDs should all be accepted independently."""
        app = _make_app()
        for eid in ids:
            assert app.is_duplicate(eid) is False


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
