"""Slack Socket Mode application.

Maintains a WebSocket connection to Slack, receives events
(app_mention, DM, slash command), and dispatches questions
to the Bedrock Agent orchestrator.

Requirements: 1.1, 1.2, 1.3, 1.4
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from slack_agent_router.formatter import format_answer
from slack_agent_router.models import ParsedQuestion
from slack_agent_router.sanitize import strip_slack_formatting

logger = logging.getLogger(__name__)

# TTL for event deduplication (seconds).
_DEDUP_TTL = 60

_EMPTY_QUESTION_HINT = "Try asking me something like: `@bot What is our PTO policy?`"
_UNAUTHORIZED_MSG = "Sorry, this bot is only available to Sage staff."


class SlackAgentApp:
    """Main application using Slack Bolt with async Socket Mode.

    Handles app_mention, DM (channel_type="im"), and /sage-ask
    slash command events. Strips bot mention prefixes, parses
    events into ParsedQuestion, and dispatches to the orchestrator.

    The Bolt ``AsyncApp`` and ``AsyncSocketModeHandler`` are created
    lazily in ``start()`` because they require a running event loop.
    All synchronous helpers (dedup, parsing, stripping) work without
    an event loop so they can be tested with Hypothesis.

    Required credentials:
      * bot_token (xoxb-...) — the Bot User OAuth Token. This is what the
        bot uses to call Slack's Web API (post messages, add reactions,
        read channel info). You get it from OAuth & Permissions in your
        Slack app settings.
      * app_token (xapp-...) — the App-Level Token. This is specifically
        for Socket Mode — it authenticates the WebSocket connection to
        Slack. You generate it under Basic Information → App-Level Tokens
        with the connections:write scope.

    In short: app_token opens the WebSocket pipe, bot_token lets you do things
    through that pipe (send messages, react, etc.).
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        orchestrator: Any,
        rate_limiter: Any | None = None,
        auth_check: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._orchestrator = orchestrator
        self._rate_limiter = rate_limiter
        self._auth_check = auth_check
        self._clock = clock or time.monotonic
        self._seen_events: dict[str, float] = {}
        # Populated lazily by start()
        self.app: Any | None = None
        self.handler: Any | None = None

    # ------------------------------------------------------------------
    # Event deduplication
    # ------------------------------------------------------------------

    def is_duplicate(self, event_id: str) -> bool:
        """Check if an event ID was already seen within the TTL window.

        Returns True if duplicate, False if new. Registers the ID
        on first call so subsequent calls within the TTL return True.
        """
        now = self._clock()
        self._evict_expired(now)

        if event_id in self._seen_events:
            return True

        self._seen_events[event_id] = now
        return False

    def _evict_expired(self, now: float) -> None:
        """Remove event IDs older than the TTL."""
        cutoff = now - _DEDUP_TTL
        expired = [eid for eid, ts in self._seen_events.items() if ts <= cutoff]
        for eid in expired:
            del self._seen_events[eid]

    # ------------------------------------------------------------------
    # Bot mention stripping
    # ------------------------------------------------------------------

    @staticmethod
    def strip_bot_mention(text: str, bot_user_id: str) -> str:
        """Remove the leading <@BOT_ID> mention from message text.

        Only strips the first occurrence at the start of the string.
        Mentions of other users or mid-text mentions are preserved.
        """
        pattern = rf"^\s*<@{re.escape(bot_user_id)}>\s*"
        return re.sub(pattern, "", text)

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def parse_event(self, event: dict[str, Any], bot_user_id: str) -> ParsedQuestion:
        """Parse a Slack event (app_mention or message) into a ParsedQuestion.

        Strips the bot mention prefix for app_mention events and
        applies Slack formatting cleanup to the question text.
        """
        event_type = event.get("type", "message")
        text = event.get("text", "")

        if event_type == "app_mention":
            text = self.strip_bot_mention(text, bot_user_id)

        question = strip_slack_formatting(text)

        return ParsedQuestion(
            event_type=event_type,
            user_id=event.get("user", ""),
            channel_id=event.get("channel", ""),
            thread_ts=event.get("thread_ts"),
            question=question,
            team_id=event.get("team", ""),
            event_ts=event.get("event_ts", event.get("ts", "")),
            request_id=str(uuid.uuid4()),
        )

    def parse_command(self, command: dict[str, Any]) -> ParsedQuestion:
        """Parse a slash command payload into a ParsedQuestion."""
        question = strip_slack_formatting(command.get("text", ""))

        return ParsedQuestion(
            event_type="slash_command",
            user_id=command.get("user_id", ""),
            channel_id=command.get("channel_id", ""),
            thread_ts=None,
            question=question,
            team_id=command.get("team_id", ""),
            event_ts=command.get("trigger_id", ""),
            request_id=str(uuid.uuid4()),
        )

    # ------------------------------------------------------------------
    # Core event handler (shared logic)
    # ------------------------------------------------------------------

    async def handle_event(
        self,
        event: dict[str, Any],
        *,
        say: Any,
        client: Any,
        bot_user_id: str,
    ) -> None:
        """Process a Slack event through the full pipeline.

        Pipeline order: parse → empty check → auth → rate limit → orchestrate → respond.
        Posts ephemeral messages for empty questions, unauthorized users,
        and rate-limited users. Dispatches valid questions to the orchestrator.
        """
        parsed = self.parse_event(event, bot_user_id)

        # Empty question check
        if not parsed.question.strip():
            await client.chat_postEphemeral(
                channel=parsed.channel_id,
                user=parsed.user_id,
                text=_EMPTY_QUESTION_HINT,
            )
            return

        # Authorization check
        if self._auth_check is not None:
            authorized = await self._auth_check(parsed.user_id)
            if not authorized:
                await client.chat_postEphemeral(
                    channel=parsed.channel_id,
                    user=parsed.user_id,
                    text=_UNAUTHORIZED_MSG,
                )
                return

        # Rate limit check
        if self._rate_limiter is not None:
            allowed, reason = self._rate_limiter.check(parsed.user_id)
            if not allowed:
                await client.chat_postEphemeral(
                    channel=parsed.channel_id,
                    user=parsed.user_id,
                    text=reason,
                )
                return

        # Dispatch to orchestrator
        session_id = self._derive_session_id(parsed)
        response = await self._orchestrator.ask(parsed.question, session_id)

        elapsed_seconds = response.latency_ms / 1000.0
        formatted = format_answer(response, elapsed_seconds)

        thread_ts = parsed.thread_ts or parsed.event_ts
        await say(text=formatted, thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Slack Bolt event handlers (registered in start())
    # ------------------------------------------------------------------

    async def _handle_mention(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle @bot mentions in channels."""
        bot_user_id = (await client.auth_test()).get("user_id", "")
        await self.handle_event(event, say=say, client=client, bot_user_id=bot_user_id)

    async def _handle_dm(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle direct messages to the bot (channel_type='im' only)."""
        if event.get("channel_type") != "im":
            return
        bot_user_id = (await client.auth_test()).get("user_id", "")
        await self.handle_event(event, say=say, client=client, bot_user_id=bot_user_id)

    async def _handle_slash_command(self, ack: Any, command: dict[str, Any], say: Any, client: Any) -> None:
        """Handle /sage-ask slash command.

        Acknowledges within 3 seconds, then processes asynchronously.
        """
        await ack()
        parsed = self.parse_command(command)

        if not parsed.question.strip():
            await client.chat_postEphemeral(
                channel=parsed.channel_id,
                user=parsed.user_id,
                text=_EMPTY_QUESTION_HINT,
            )
            return

        if self._auth_check is not None:
            authorized = await self._auth_check(parsed.user_id)
            if not authorized:
                await client.chat_postEphemeral(
                    channel=parsed.channel_id,
                    user=parsed.user_id,
                    text=_UNAUTHORIZED_MSG,
                )
                return

        if self._rate_limiter is not None:
            allowed, reason = self._rate_limiter.check(parsed.user_id)
            if not allowed:
                await client.chat_postEphemeral(
                    channel=parsed.channel_id,
                    user=parsed.user_id,
                    text=reason,
                )
                return

        session_id = self._derive_session_id(parsed)
        response = await self._orchestrator.ask(parsed.question, session_id)

        elapsed_seconds = response.latency_ms / 1000.0
        formatted = format_answer(response, elapsed_seconds)
        await say(text=formatted)

    # ------------------------------------------------------------------
    # Session ID derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_session_id(parsed: ParsedQuestion) -> str:
        """Derive a Bedrock Agent session ID from Slack thread context.

        Thread reply:   {channel_id}:{thread_ts}
        New message:    {channel_id}:{event_ts}
        """
        ts = parsed.thread_ts or parsed.event_ts
        return f"{parsed.channel_id}:{ts}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the Bolt app, register handlers, and start Socket Mode.

        Must be called from within a running event loop.
        """
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self.app = AsyncApp(token=self._bot_token)
        self.app.event("app_mention")(self._handle_mention)
        self.app.event("message")(self._handle_dm)
        self.app.command("/sage-ask")(self._handle_slash_command)

        self.handler = AsyncSocketModeHandler(self.app, self._app_token)
        await self.handler.start_async()

    async def stop(self) -> None:
        """Gracefully disconnect and drain in-flight requests."""
        if self.handler is not None:
            await self.handler.close_async()
