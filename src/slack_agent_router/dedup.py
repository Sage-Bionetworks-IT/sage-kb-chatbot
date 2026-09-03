"""In-memory event deduplication with a TTL cache.

Slack may redeliver the same event (or retry a slash command) if the
acknowledgement is slow or a WebSocket reconnect occurs.  This module
tracks recently seen event identifiers in a fixed-window TTL cache so
duplicates can be skipped silently.

All state lives in-process — acceptable for a single ECS Fargate task.
The cache resets on task restart, leaving a brief window for duplicates
during restarts, which is acceptable for the MVP.

Requirements: 1.5, 1.6
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Default deduplication window in seconds.
_DEFAULT_TTL = 60.0


class EventDeduplicator:
    """Fixed-window TTL cache for Slack event/envelope/trigger IDs.

    An identifier is considered a duplicate if it was seen within the
    last ``ttl_seconds``.  The clock is injectable so behavior can be
    tested deterministically without sleeping (mirrors ``RateLimiter``).
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        # Maps identifier -> timestamp it was first seen.
        self._seen: dict[str, float] = {}
        self._last_cleanup: float = self._clock()

    def is_duplicate(self, event_id: str | None) -> bool:
        """Return True if ``event_id`` was seen within the TTL window.

        A previously unseen (or expired) identifier is recorded and
        ``False`` is returned, so a single call both checks and marks.

        An empty or ``None`` identifier is never treated as a duplicate
        (we cannot reliably dedupe without an ID) and is not recorded.
        """
        if not event_id:
            return False

        now = self._clock()
        seen_at = self._seen.get(event_id)

        if seen_at is not None and (now - seen_at) < self._ttl:
            # Still inside the window — genuine duplicate.
            return True

        # Unseen or expired: record (or refresh) and accept.
        self._seen[event_id] = now
        self._maybe_cleanup(now)
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically evict expired identifiers to bound memory.

        Runs at most once per TTL window to keep the common path cheap.
        """
        if now - self._last_cleanup < self._ttl:
            return
        self._last_cleanup = now

        cutoff = now - self._ttl
        expired = [key for key, ts in self._seen.items() if ts <= cutoff]
        for key in expired:
            del self._seen[key]
