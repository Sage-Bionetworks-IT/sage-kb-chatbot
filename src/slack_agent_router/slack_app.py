"""Slack Socket Mode application.

Maintains a WebSocket connection to Slack, receives events
(app_mention, DM, slash command), and dispatches questions
to the Bedrock Agent orchestrator.

Requirements: 1.1, 1.2, 1.3, 1.4, 3.7, 9.1, 9.2, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from slack_agent_router.formatter import format_answer
from slack_agent_router.models import AgentResponse, ParsedQuestion
from slack_agent_router.sanitize import sanitize_backend_response, strip_slack_formatting

logger = logging.getLogger(__name__)

# Slack 429 retry configuration.
_SLACK_MAX_RETRIES = 3
_SLACK_DEFAULT_RETRY_AFTER = 1.0  # seconds

_EMPTY_QUESTION_HINT = "Try asking me something like: `@bot What is our PTO policy?`"
_UNAUTHORIZED_MSG = "Sorry, this bot is only available to Sage staff."
_ALL_BACKENDS_FAILED_MSG = "I wasn't able to find an answer right now. Please try again in a few minutes."
_AGENT_FAILURE_MSG = "I'm having trouble processing your question right now. Please try again in a few minutes."


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
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._orchestrator = orchestrator
        self._rate_limiter = rate_limiter
        self._auth_check = auth_check
        # Populated lazily by start()
        self.app: Any | None = None
        self.handler: Any | None = None

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
    # Core pipeline (shared by event handlers and slash commands)
    # ------------------------------------------------------------------

    async def _process_question(
        self,
        parsed: ParsedQuestion,
        *,
        say: Any,
        client: Any,
        thread_ts: str | None = None,
    ) -> None:
        """Run the full question pipeline on a ParsedQuestion.

        Pipeline: empty check → auth → rate limit → orchestrate → respond.
        Posts ephemeral messages for empty questions, unauthorized users,
        and rate-limited users. Dispatches valid questions to the orchestrator.

        Rate limiter acquire/release brackets the orchestrator call so
        in-flight tracking is accurate. Slack 429 errors are retried
        with exponential backoff.

        Args:
            parsed: The normalized question from any Slack input method.
            say: Slack Bolt ``say`` callable for posting messages.
            client: Slack Web API client for ephemeral messages.
            thread_ts: Thread timestamp for reply threading. When None
                       (e.g. slash commands), the reply is not threaded.
        """
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

        # Acquire rate limiter slot (tracks in-flight + sliding windows)
        if self._rate_limiter is not None:
            self._rate_limiter.acquire(parsed.user_id)

        try:
            response = await self._dispatch_and_format(parsed)
        finally:
            if self._rate_limiter is not None:
                self._rate_limiter.release(parsed.user_id)

        await self._post_with_retry(say, text=response, thread_ts=thread_ts)

    async def handle_event(
        self,
        event: dict[str, Any],
        *,
        say: Any,
        client: Any,
        bot_user_id: str,
    ) -> None:
        """Parse a Slack event and run it through the shared pipeline."""
        parsed = self.parse_event(event, bot_user_id)
        thread_ts = parsed.thread_ts or parsed.event_ts
        await self._process_question(parsed, say=say, client=client, thread_ts=thread_ts)

    async def _dispatch_and_format(self, parsed: ParsedQuestion) -> str:
        """Call the orchestrator and return a formatted Slack mrkdwn string.

        Handles orchestrator exceptions and returns appropriate error
        messages so the caller always gets a string to post.
        """
        session_id = self._derive_session_id(parsed)

        try:
            response: AgentResponse = await self._orchestrator.ask(parsed.question, session_id)
        except Exception as exc:
            logger.error(
                "Orchestrator raised an exception for request %s: %s",
                parsed.request_id,
                exc,
                exc_info=True,
            )
            return _AGENT_FAILURE_MSG

        # The orchestrator never raises — it returns AgentResponse in all
        # cases. But we still check for the "all backends failed" scenario
        # by inspecting the response: if no tool calls were made and the
        # answer matches the generic agent failure message, treat it as
        # an all-backends-failed case.
        if self._is_all_backends_failed(response):
            return _ALL_BACKENDS_FAILED_MSG

        elapsed_seconds = response.latency_ms / 1000.0
        sanitized = AgentResponse(
            answer=sanitize_backend_response(response.answer),
            source_urls=response.source_urls,
            tool_calls_made=response.tool_calls_made,
            latency_ms=response.latency_ms,
        )
        return format_answer(sanitized, elapsed_seconds)

    @staticmethod
    def _is_all_backends_failed(response: AgentResponse) -> bool:
        """Detect the "all backends failed" scenario.

        When the orchestrator fails before any tool calls succeed, it
        returns an AgentResponse with empty tool_calls_made and no
        source_urls. We use this structural check rather than matching
        on the error message text.
        """
        return not response.tool_calls_made and not response.source_urls

    @staticmethod
    async def _post_with_retry(
        say: Any,
        *,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        """Post a message via ``say()``, retrying on Slack 429 errors.

        Retries up to ``_SLACK_MAX_RETRIES`` times with exponential
        backoff. The delay is ``max(Retry-After, base * 2^attempt)``
        where base is ``_SLACK_DEFAULT_RETRY_AFTER``, so we always
        respect the server's requested delay while still backing off.
        """
        for attempt in range(_SLACK_MAX_RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {"text": text}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                await say(**kwargs)
                return
            except Exception as exc:
                retry_after = _extract_retry_after(exc)
                if retry_after is not None and attempt < _SLACK_MAX_RETRIES:
                    exponential_delay = _SLACK_DEFAULT_RETRY_AFTER * (2**attempt)
                    delay = max(retry_after, exponential_delay)
                    logger.warning(
                        "Slack 429 on attempt %d/%d — retrying in %.1fs",
                        attempt + 1,
                        _SLACK_MAX_RETRIES + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Not a 429 or exhausted retries — re-raise
                raise

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

        Acknowledges within 3 seconds, then processes via the shared pipeline.
        """
        await ack()
        parsed = self.parse_command(command)
        await self._process_question(parsed, say=say, client=client)

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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract a Retry-After delay from a Slack API error.

    Returns the delay in seconds if the exception represents an
    HTTP 429 response, or ``None`` if it's a different error.

    The ``slack_sdk`` raises ``SlackApiError`` with a ``response``
    attribute. We check for status 429 and read the ``Retry-After``
    header. For other exception types we fall back to checking
    common attributes.
    """
    # slack_sdk.errors.SlackApiError
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status == 429:
            headers = getattr(response, "headers", {})
            try:
                return float(headers.get("Retry-After", _SLACK_DEFAULT_RETRY_AFTER))
            except (TypeError, ValueError):
                return _SLACK_DEFAULT_RETRY_AFTER

    # Generic fallback: check for a status attribute (e.g. httpx, aiohttp)
    status = getattr(exc, "status", getattr(exc, "status_code", None))
    if status == 429:
        return _SLACK_DEFAULT_RETRY_AFTER

    return None
