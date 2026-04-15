"""Property tests for rate limiter (RED).

Property 3: Per-user rate limit window enforcement — for any user at
            the window limit, the next request is rejected with a
            non-empty reason.
Property 4: Per-user in-flight concurrency limit — if a request is
            in-flight, subsequent requests are rejected; after release,
            next request is accepted.
Property 5: Global rate limit enforcement — when total requests across
            all users reach 50/min, the next request is rejected.

These tests should FAIL until task 3.2 implements the RateLimiter.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import time
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.models import RateLimitConfig
from slack_agent_router.rate_limiter import RateLimiter

# --- Strategies ---

user_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "N"), min_codepoint=48, max_codepoint=90),
    min_size=9,
    max_size=11,
).map(lambda s: f"U{s}")

distinct_user_ids = st.lists(user_id, min_size=2, max_size=10, unique=True)


# -------------------------------------------------------
# Property 3: Per-user rate limit window enforcement
# -------------------------------------------------------


class TestPerUserRateLimitWindows:
    """Property 3: user at the window limit gets rejected with a reason."""

    @given(uid=user_id)
    @settings(max_examples=20)
    def test_per_minute_limit_rejects_at_threshold(self, uid: str) -> None:
        """After 5 requests in a minute, the 6th is rejected."""
        config = RateLimitConfig(per_user_per_minute=5)
        limiter = RateLimiter(config)
        for _ in range(5):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None
        assert len(reason) > 0

    @given(uid=user_id)
    @settings(max_examples=20)
    def test_per_hour_limit_rejects_at_threshold(self, uid: str) -> None:
        """After 30 requests in an hour, the 31st is rejected."""
        config = RateLimitConfig(per_user_per_minute=100, per_user_per_hour=30)
        limiter = RateLimiter(config)
        for _ in range(30):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None
        assert len(reason) > 0

    @given(uid=user_id)
    @settings(max_examples=20)
    def test_per_day_limit_rejects_at_threshold(self, uid: str) -> None:
        """After 100 requests in a day, the 101st is rejected."""
        config = RateLimitConfig(per_user_per_minute=1000, per_user_per_hour=1000, per_user_per_day=100)
        limiter = RateLimiter(config)
        for _ in range(100):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None
        assert len(reason) > 0

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_reason_string_is_user_friendly(self, uid: str) -> None:
        """The rejection reason should be a non-empty, non-whitespace string."""
        config = RateLimitConfig(per_user_per_minute=2)
        limiter = RateLimiter(config)
        for _ in range(2):
            limiter.check(uid)
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert isinstance(reason, str)
        assert len(reason.strip()) > 0

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_requests_allowed_after_window_expires(self, uid: str) -> None:
        """After the minute window slides past, requests are allowed again."""
        config = RateLimitConfig(per_user_per_minute=2)
        limiter = RateLimiter(config)
        for _ in range(2):
            limiter.check(uid)
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, _ = limiter.check(uid)
        assert allowed is False
        with patch("time.time", return_value=time.time() + 61):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_check_is_non_destructive(self, uid: str) -> None:
        """Calling check() alone should not consume a request slot."""
        config = RateLimitConfig(per_user_per_minute=3)
        limiter = RateLimiter(config)
        for _ in range(10):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None


# -------------------------------------------------------
# Property 4: Per-user in-flight concurrency limit
# -------------------------------------------------------


class TestPerUserInFlightLimit:
    """Property 4: in-flight request blocks subsequent; release unblocks."""

    @given(uid=user_id)
    @settings(max_examples=20)
    def test_rejected_while_in_flight(self, uid: str) -> None:
        """While one request is in-flight, the next is rejected."""
        config = RateLimitConfig(per_user_in_flight=1)
        limiter = RateLimiter(config)
        allowed, _ = limiter.check(uid)
        assert allowed is True
        limiter.acquire(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None
        assert len(reason) > 0

    @given(uid=user_id)
    @settings(max_examples=20)
    def test_accepted_after_release(self, uid: str) -> None:
        """After releasing the in-flight request, the next is accepted."""
        config = RateLimitConfig(per_user_in_flight=1)
        limiter = RateLimiter(config)
        limiter.check(uid)
        limiter.acquire(uid)
        limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is True
        assert reason is None

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_acquire_release_cycle_repeatable(self, uid: str) -> None:
        """Multiple acquire/release cycles should all succeed."""
        config = RateLimitConfig(per_user_per_minute=100, per_user_in_flight=1)
        limiter = RateLimiter(config)
        for _ in range(5):
            allowed, reason = limiter.check(uid)
            assert allowed is True
            assert reason is None
            limiter.acquire(uid)
            limiter.release(uid)

    @given(users=distinct_user_ids)
    @settings(max_examples=10)
    def test_in_flight_is_per_user_not_global(self, users: list[str]) -> None:
        """One user's in-flight request should not block another user."""
        config = RateLimitConfig(per_user_in_flight=1)
        limiter = RateLimiter(config)
        limiter.check(users[0])
        limiter.acquire(users[0])
        allowed, reason = limiter.check(users[1])
        assert allowed is True
        assert reason is None


# -------------------------------------------------------
# Property 5: Global rate limit enforcement
# -------------------------------------------------------


class TestGlobalRateLimit:
    """Property 5: global 50/min limit rejects when reached."""

    def test_global_limit_rejects_at_threshold(self) -> None:
        """After 50 requests across all users, the 51st is rejected."""
        config = RateLimitConfig(
            per_user_per_minute=100,
            per_user_per_hour=1000,
            per_user_per_day=10000,
            global_per_minute=50,
        )
        limiter = RateLimiter(config)
        for i in range(50):
            uid = f"U{i:011d}"
            allowed, reason = limiter.check(uid)
            assert allowed is True, f"Request {i + 1} should be allowed"
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check("U99999999999")
        assert allowed is False
        assert reason is not None
        assert len(reason) > 0

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_global_limit_single_user_flood(self, uid: str) -> None:
        """A single user flooding should also hit the global limit."""
        config = RateLimitConfig(
            per_user_per_minute=100,
            per_user_per_hour=1000,
            per_user_per_day=10000,
            global_per_minute=50,
        )
        limiter = RateLimiter(config)
        for _ in range(50):
            limiter.check(uid)
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None

    def test_global_limit_resets_after_window(self) -> None:
        """After the minute window slides, global limit resets."""
        config = RateLimitConfig(
            per_user_per_minute=100,
            per_user_per_hour=1000,
            per_user_per_day=10000,
            global_per_minute=5,
        )
        limiter = RateLimiter(config)
        for i in range(5):
            uid = f"U{i:011d}"
            limiter.check(uid)
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, _ = limiter.check("UNEWUSER0000")
        assert allowed is False
        with patch("time.time", return_value=time.time() + 61):
            allowed, reason = limiter.check("UNEWUSER0000")
            assert allowed is True
            assert reason is None

    @given(users=distinct_user_ids)
    @settings(max_examples=10)
    def test_global_limit_counts_across_users(self, users: list[str]) -> None:
        """Requests from different users all count toward the global limit."""
        global_limit = len(users)
        config = RateLimitConfig(
            per_user_per_minute=100,
            per_user_per_hour=1000,
            per_user_per_day=10000,
            global_per_minute=global_limit,
        )
        limiter = RateLimiter(config)
        for uid in users:
            allowed, _ = limiter.check(uid)
            assert allowed is True
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, reason = limiter.check(users[0])
        assert allowed is False
        assert reason is not None


# -------------------------------------------------------
# Combined behavior tests
# -------------------------------------------------------


class TestRateLimiterCombined:
    """Tests verifying interactions between different limit types."""

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_in_flight_checked_before_window_limits(self, uid: str) -> None:
        """In-flight limit should reject even if window limits are fine."""
        config = RateLimitConfig(per_user_per_minute=100, per_user_in_flight=1)
        limiter = RateLimiter(config)
        limiter.check(uid)
        limiter.acquire(uid)
        allowed, reason = limiter.check(uid)
        assert allowed is False
        assert reason is not None

    def test_default_config_values(self) -> None:
        """RateLimiter with default config uses expected thresholds."""
        limiter = RateLimiter()
        config = RateLimitConfig()
        uid = "UDEFAULT00000"
        for _ in range(config.per_user_per_minute):
            allowed, _ = limiter.check(uid)
            assert allowed is True
            limiter.acquire(uid)
            limiter.release(uid)
        allowed, _ = limiter.check(uid)
        assert allowed is False

    @given(uid=user_id)
    @settings(max_examples=10)
    def test_release_without_acquire_is_safe(self, uid: str) -> None:
        """Calling release without a prior acquire should not raise."""
        limiter = RateLimiter()
        limiter.release(uid)
