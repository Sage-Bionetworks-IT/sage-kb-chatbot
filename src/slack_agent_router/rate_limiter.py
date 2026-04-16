"""In-memory rate limiter with sliding window counters.

Enforces per-user (minute/hour/day), per-user in-flight, and global
per-minute limits.  All state lives in-process — acceptable for a
single ECS Fargate task.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from slack_agent_router.models import RateLimitConfig

# Seconds per window
_MINUTE = 60
_HOUR = 3_600
_DAY = 86_400

# How long a user key can sit idle before we evict it.
_USER_TTL = _DAY + _MINUTE  # slightly longer than the largest window


@dataclass
class _UserState:
    """Mutable per-user counters and timestamps."""

    minute_timestamps: list[float] = field(default_factory=list)
    hour_timestamps: list[float] = field(default_factory=list)
    day_timestamps: list[float] = field(default_factory=list)
    in_flight: int = 0
    last_active: float = 0.0


class RateLimiter:
    """In-memory rate limiter with sliding window counters."""

    def __init__(self, config: RateLimitConfig | None = None, *, clock: Callable[[], float] | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._clock = clock or time.monotonic
        self._users: dict[str, _UserState] = {}
        self._global_minute_timestamps: list[float] = []
        self._last_cleanup: float = self._clock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, user_id: str) -> tuple[bool, str | None]:
        """Check if a request is allowed without consuming a slot.

        Returns:
            (True, None) if allowed.
            (False, reason) if rate-limited.
        """
        now = self._clock()
        state = self._users.get(user_id)

        # In-flight check
        if state is not None and state.in_flight >= self._config.per_user_in_flight:
            return False, "You already have a request in progress. Please wait for it to finish."

        # Per-user sliding window checks
        if state is not None:
            if self._count_in_window(state.minute_timestamps, now, _MINUTE) >= self._config.per_user_per_minute:
                return False, "You've reached the per-minute request limit. Please wait a moment before trying again."

            if self._count_in_window(state.hour_timestamps, now, _HOUR) >= self._config.per_user_per_hour:
                return False, "You've reached the hourly request limit. Please try again later."

            if self._count_in_window(state.day_timestamps, now, _DAY) >= self._config.per_user_per_day:
                return False, "You've reached the daily request limit. Please try again tomorrow."

        # Global per-minute check
        if self._count_in_window(self._global_minute_timestamps, now, _MINUTE) >= self._config.global_per_minute:
            return False, "The bot is experiencing high demand right now. Please try again in a minute."

        return True, None

    def acquire(self, user_id: str) -> None:
        """Record that a request is being processed."""
        now = self._clock()
        state = self._ensure_user(user_id, now)

        state.minute_timestamps.append(now)
        state.hour_timestamps.append(now)
        state.day_timestamps.append(now)
        state.in_flight += 1
        state.last_active = now

        self._global_minute_timestamps.append(now)

        self._maybe_cleanup(now)

    def release(self, user_id: str) -> None:
        """Record that a request has completed (decrement in-flight).

        Safe to call even without a prior acquire.
        """
        state = self._users.get(user_id)
        if state is not None and state.in_flight > 0:
            state.in_flight -= 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _count_in_window(timestamps: list[float], now: float, window: float) -> int:
        """Count timestamps within the sliding window."""
        cutoff = now - window
        return sum(1 for ts in timestamps if ts > cutoff)

    @staticmethod
    def _prune_timestamps(timestamps: list[float], now: float, window: float) -> list[float]:
        """Remove timestamps outside the sliding window."""
        cutoff = now - window
        return [ts for ts in timestamps if ts > cutoff]

    def _ensure_user(self, user_id: str, now: float) -> _UserState:
        """Get or create user state."""
        if user_id not in self._users:
            self._users[user_id] = _UserState(last_active=now)
        return self._users[user_id]

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically evict stale user keys and prune old timestamps."""
        if now - self._last_cleanup < _MINUTE:
            return
        self._last_cleanup = now

        # Prune global timestamps
        self._global_minute_timestamps = self._prune_timestamps(
            self._global_minute_timestamps,
            now,
            _MINUTE,
        )

        # Evict inactive users and prune active ones
        stale_keys = []
        for uid, state in self._users.items():
            if state.in_flight == 0 and (now - state.last_active) > _USER_TTL:
                stale_keys.append(uid)
            else:
                state.minute_timestamps = self._prune_timestamps(state.minute_timestamps, now, _MINUTE)
                state.hour_timestamps = self._prune_timestamps(state.hour_timestamps, now, _HOUR)
                state.day_timestamps = self._prune_timestamps(state.day_timestamps, now, _DAY)

        for uid in stale_keys:
            del self._users[uid]
