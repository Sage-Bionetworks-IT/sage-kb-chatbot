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

# Progressive UX feedback (Requirement 4).
_REACTION_WORKING = "eyes"  # 👀 — added on receipt
_REACTION_DONE = "white_check_mark"  # ✅ — added when the answer is posted
_PLACEHOLDER_THINKING = "⏳ Thinking..."

# Maps a Bedrock action group name to a user-facing "searching…" message.
_ACTION_GROUP_PROGRESS: dict[str, str] = {
    "SearchConfluenceJira": "⏳ Searching Confluence and Jira...",
}
_PROGRESS_DEFAULT = "⏳ Searching..."


class SlackAgentApp:
    """Main application using Slack Bolt with async Socket Mode.

    Handles app_mention, DM (channel_type="im"), and /sage-ask
    slash command events. Strips bot mention prefixes, parses
    events into ParsedQuestion, and dispatches to the orchestrator.

    The Bolt ``AsyncApp`` and ``AsyncSocketModeHandler`` are created
    lazily in ``start()`` because they require a running event loop.
    All synchronous helpers (parsing, stripping) work without
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
        deduplicator: Any | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._orchestrator = orchestrator
        self._rate_limiter = rate_limiter
        self._auth_check = auth_check
        self._deduplicator = deduplicator
        # Populated lazily by start()
        self.app: Any | None = None
        self.handler: Any | None = None
        self._bot_user_id: str | None = None

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
        reaction_ts: str | None = None,
    ) -> None:
        """Run the full question pipeline on a ParsedQuestion.

        Pipeline: empty check → auth → rate limit → orchestrate → respond.
        Posts ephemeral messages for empty questions, unauthorized users,
        and rate-limited users. Dispatches valid questions to the orchestrator.

        Rate limiter acquire/release brackets the orchestrator call so
        in-flight tracking is accurate. Slack 429 errors are retried
        with exponential backoff.

        Progressive UX (Requirement 4): a 👀 reaction is added on receipt,
        a "⏳ Thinking..." placeholder is posted in the thread and updated
        as each backend is searched, then replaced with the final answer;
        the 👀 reaction is swapped for ✅ when the answer is posted.

        Args:
            parsed: The normalized question from any Slack input method.
            say: Slack Bolt ``say`` callable for posting messages.
            client: Slack Web API client for messages and reactions.
            thread_ts: Thread timestamp for reply threading. When None
                       (e.g. slash commands), the reply is not threaded.
            reaction_ts: Timestamp of the user's message to react to. When
                       None (e.g. slash commands, which have no message ts),
                       reactions are skipped.
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

        # Rate limit check + acquire. Acquire immediately after a successful
        # check, before any awaits, so the in-flight counter is incremented
        # synchronously. Awaiting between check() and acquire() would let
        # concurrent requests all pass check() before any of them increment
        # in_flight, bypassing the per-user in-flight guard under load.
        if self._rate_limiter is not None:
            allowed, reason = self._rate_limiter.check(parsed.user_id)
            if not allowed:
                await client.chat_postEphemeral(
                    channel=parsed.channel_id,
                    user=parsed.user_id,
                    text=reason,
                )
                return
            self._rate_limiter.acquire(parsed.user_id)

        try:
            # Progressive UX: acknowledge receipt with a reaction + placeholder.
            await self._add_reaction(client, parsed.channel_id, reaction_ts, _REACTION_WORKING)
            placeholder_ts = await self._post_placeholder(client, parsed.channel_id, thread_ts)

            # Progress callback updates the placeholder as each backend is searched.
            on_progress = self._make_progress_callback(client, parsed.channel_id, placeholder_ts)

            response = await self._dispatch_and_format(parsed, on_progress=on_progress)
        finally:
            if self._rate_limiter is not None:
                self._rate_limiter.release(parsed.user_id)

        # Deliver the final answer: update the placeholder in place when we
        # have one, otherwise post a fresh threaded message.
        await self._deliver_answer(
            say,
            client,
            channel_id=parsed.channel_id,
            placeholder_ts=placeholder_ts,
            text=response,
            thread_ts=thread_ts,
        )

        # Swap the working reaction for a done reaction.
        await self._remove_reaction(client, parsed.channel_id, reaction_ts, _REACTION_WORKING)
        await self._add_reaction(client, parsed.channel_id, reaction_ts, _REACTION_DONE)

    async def handle_event(
        self,
        event: dict[str, Any],
        *,
        say: Any,
        client: Any,
        bot_user_id: str,
    ) -> None:
        """Parse a Slack event and run it through the shared pipeline.

        Deduplicates on the event identifier first (before auth and rate
        limiting per Requirement 2.3); duplicates are skipped silently.
        """
        if self._is_duplicate(self._event_dedup_id(event)):
            return
        parsed = self.parse_event(event, bot_user_id)
        thread_ts = parsed.thread_ts or parsed.event_ts
        # parsed.event_ts is the user's message ts — the target for reactions.
        await self._process_question(
            parsed,
            say=say,
            client=client,
            thread_ts=thread_ts,
            reaction_ts=parsed.event_ts,
        )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, dedup_id: str | None) -> bool:
        """Return True when the identifier was already processed recently.

        No-op (always False) when no deduplicator is configured.
        """
        if self._deduplicator is None:
            return False
        return self._deduplicator.is_duplicate(dedup_id)

    @staticmethod
    def _event_dedup_id(event: dict[str, Any]) -> str:
        """Derive a stable deduplication key for an event.

        Prefers Slack's ``event_id``/``client_msg_id`` when present, and
        falls back to a ``channel:event_ts`` composite so redelivered
        events without an ID are still caught.

        Returns an empty string when neither a native ID nor a reliable
        composite can be formed—callers (and the deduplicator) treat
        empty strings as "not dedupable" and allow the event through.
        """
        if event.get("event_id"):
            return event["event_id"]
        if event.get("client_msg_id"):
            return event["client_msg_id"]

        channel = event.get("channel", "")
        ts = event.get("event_ts") or event.get("ts") or ""
        if channel and ts:
            return f"{channel}:{ts}"
        return ""

    async def _dispatch_and_format(
        self,
        parsed: ParsedQuestion,
        on_progress: Callable[[str], Any] | None = None,
    ) -> str:
        """Call the orchestrator and return a formatted Slack mrkdwn string.

        Handles orchestrator exceptions and returns appropriate error
        messages so the caller always gets a string to post.

        Args:
            parsed: The normalized question.
            on_progress: Optional async callback forwarded to the
                orchestrator to surface per-backend progress.
        """
        session_id = self._derive_session_id(parsed)

        try:
            response: AgentResponse = await self._orchestrator.ask(parsed.question, session_id, on_progress=on_progress)
        except Exception as exc:
            logger.error(
                "Orchestrator raised an exception for request %s: %s",
                parsed.request_id,
                exc,
                exc_info=True,
            )
            return _AGENT_FAILURE_MSG

        # The orchestrator sets failed=True on the AgentResponse when it
        # could not produce a useful answer. Check that flag to distinguish
        # total failure from a legitimate direct answer with no tool calls.
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

        The orchestrator sets ``failed=True`` on the AgentResponse when
        it could not produce a useful answer (timeout, exception, or no
        successful tool calls). This explicit flag avoids false positives
        when the agent answers directly without tool calls.
        """
        return response.failed

    @staticmethod
    async def _slack_call_with_retry(call: Callable[[], Any]) -> Any:
        """Invoke a Slack API coroutine factory, retrying on 429 errors.

        ``call`` is a zero-arg callable returning a fresh awaitable each
        time (so retries re-issue the request). Retries up to
        ``_SLACK_MAX_RETRIES`` times with exponential backoff; the delay
        is ``max(Retry-After, base * 2^attempt)`` so we always respect the
        server's requested delay while still backing off. Non-429 errors
        (and exhausted retries) are re-raised.
        """
        for attempt in range(_SLACK_MAX_RETRIES + 1):
            try:
                return await call()
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

    async def _post_with_retry(
        self,
        say: Any,
        *,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        """Post a message via ``say()``, retrying on Slack 429 errors."""

        def _call() -> Any:
            kwargs: dict[str, Any] = {"text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            return say(**kwargs)

        await self._slack_call_with_retry(_call)

    # ------------------------------------------------------------------
    # Progressive UX feedback (Requirement 4)
    # ------------------------------------------------------------------

    async def _add_reaction(
        self,
        client: Any,
        channel_id: str,
        timestamp: str | None,
        name: str,
    ) -> None:
        """Add an emoji reaction to a message (best-effort).

        No-op when ``timestamp`` is falsy (e.g. slash commands, which
        have no message to react to). Failures are logged and swallowed
        so reaction UX never blocks answer delivery.
        """
        if not timestamp:
            return
        try:
            await self._slack_call_with_retry(
                lambda: client.reactions_add(channel=channel_id, timestamp=timestamp, name=name)
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Failed to add reaction '%s': %s", name, exc)

    async def _remove_reaction(
        self,
        client: Any,
        channel_id: str,
        timestamp: str | None,
        name: str,
    ) -> None:
        """Remove an emoji reaction from a message (best-effort)."""
        if not timestamp:
            return
        try:
            await self._slack_call_with_retry(
                lambda: client.reactions_remove(channel=channel_id, timestamp=timestamp, name=name)
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Failed to remove reaction '%s': %s", name, exc)

    async def _post_placeholder(
        self,
        client: Any,
        channel_id: str,
        thread_ts: str | None,
    ) -> str | None:
        """Post the "⏳ Thinking..." placeholder and return its timestamp.

        Returns the message ``ts`` on success so it can be updated later,
        or ``None`` if posting failed (in which case the caller falls back
        to posting the answer as a fresh message).
        """
        try:
            kwargs: dict[str, Any] = {"channel": channel_id, "text": _PLACEHOLDER_THINKING}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            response = await self._slack_call_with_retry(lambda: client.chat_postMessage(**kwargs))
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Failed to post placeholder message: %s", exc)
            return None
        # response behaves like a dict (SlackResponse supports __getitem__).
        try:
            return response["ts"]
        except (KeyError, TypeError):
            logger.warning("Placeholder response missing 'ts'")
            return None

    def _make_progress_callback(
        self,
        client: Any,
        channel_id: str,
        placeholder_ts: str | None,
    ) -> Callable[[str], Any] | None:
        """Build an async progress callback that updates the placeholder.

        Returns ``None`` when there is no placeholder to update, so the
        orchestrator skips progress reporting entirely.
        """
        if placeholder_ts is None:
            return None

        async def _on_progress(action_group: str) -> None:
            text = _ACTION_GROUP_PROGRESS.get(action_group, _PROGRESS_DEFAULT)
            await self._slack_call_with_retry(
                lambda: client.chat_update(channel=channel_id, ts=placeholder_ts, text=text)
            )

        return _on_progress

    async def _deliver_answer(
        self,
        say: Any,
        client: Any,
        *,
        channel_id: str,
        placeholder_ts: str | None,
        text: str,
        thread_ts: str | None,
    ) -> None:
        """Deliver the final answer, preferring an in-place placeholder update.

        When a placeholder exists, update it via ``chat.update``. If that
        fails (or there is no placeholder), fall back to posting a fresh
        threaded message via ``say``.
        """
        if placeholder_ts is not None:
            try:
                await self._slack_call_with_retry(
                    lambda: client.chat_update(channel=channel_id, ts=placeholder_ts, text=text)
                )
                return
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning("Failed to update placeholder with answer — posting fresh: %s", exc)

        await self._post_with_retry(say, text=text, thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Slack Bolt event handlers (registered in start())
    # ------------------------------------------------------------------

    async def _handle_mention(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle @bot mentions in channels."""
        bot_user_id = await self._get_bot_user_id(client)
        await self.handle_event(event, say=say, client=client, bot_user_id=bot_user_id)

    async def _handle_dm(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle direct messages to the bot (channel_type='im' only).

        Ignores non-IM channels, message subtypes (edits, bot_message,
        etc.), bot-authored messages, and the bot's own messages to
        prevent reply loops.
        """
        if event.get("channel_type") != "im":
            return
        # Ignore message subtypes (edits, deletes, bot_message, etc.)
        if event.get("subtype") is not None:
            return
        # Ignore messages from bots (including this bot's own replies)
        if event.get("bot_id"):
            return
        bot_user_id = await self._get_bot_user_id(client)
        # Ignore messages from the bot itself (belt-and-suspenders)
        if event.get("user") == bot_user_id:
            return
        await self.handle_event(event, say=say, client=client, bot_user_id=bot_user_id)

    async def _get_bot_user_id(self, client: Any) -> str:
        """Return the bot's user ID, fetching and caching it on first call."""
        if self._bot_user_id is None:
            result = await client.auth_test()
            self._bot_user_id = result.get("user_id", "")
        return self._bot_user_id

    async def _handle_slash_command(self, ack: Any, command: dict[str, Any], say: Any, client: Any) -> None:
        """Handle /sage-ask slash command.

        Acknowledges within 3 seconds, then processes via the shared
        pipeline. Slack may retry the command if the ack is slow, so we
        deduplicate on ``trigger_id`` (after ack) and skip duplicates
        silently.
        """
        await ack()
        if self._is_duplicate(command.get("trigger_id")):
            return
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
