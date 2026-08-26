"""Tests for EventDeduplicator.

Property 1: Event deduplication prevents reprocessing — for any event
            ID submitted to the deduplication cache, checking the same
            ID again within the 60-second TTL window returns "duplicate",
            while any previously unseen ID is accepted.

Unit tests: unseen IDs accepted; TTL expiry re-accepts; empty/None IDs
never treated as duplicates; distinct IDs are independent; cleanup does
not evict live entries.

Validates: Requirements 1.5, 1.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from slack_agent_router.dedup import EventDeduplicator

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

event_id = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=40,
)

distinct_event_ids = st.lists(event_id, min_size=2, max_size=10, unique=True)


class _MutableClock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Property 1: Event deduplication prevents reprocessing
# ---------------------------------------------------------------------------


class TestEventDeduplicationProperty:
    """Property 1: same ID within the TTL is a duplicate; unseen IDs are accepted."""

    @given(eid=event_id)
    @settings(max_examples=50)
    def test_first_seen_accepted_second_is_duplicate(self, eid: str) -> None:
        """The first check of an ID is accepted; an immediate re-check is a duplicate."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate(eid) is False
        assert dedup.is_duplicate(eid) is True

    @given(eids=distinct_event_ids)
    @settings(max_examples=30)
    def test_distinct_ids_are_all_accepted(self, eids: list[str]) -> None:
        """Every previously unseen ID is accepted (not a duplicate)."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        for eid in eids:
            assert dedup.is_duplicate(eid) is False

    @given(eid=event_id)
    @settings(max_examples=30)
    def test_id_reaccepted_after_ttl_window(self, eid: str) -> None:
        """After the TTL window elapses, the same ID is accepted again."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate(eid) is False
        clock.advance(60.0)  # exactly at the window boundary -> expired
        assert dedup.is_duplicate(eid) is False


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestEventDeduplicatorUnit:
    """Unit tests for edge cases and internal behavior."""

    def test_duplicate_within_window(self) -> None:
        """An ID re-checked before TTL expiry is a duplicate."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate("evt-1") is False
        clock.advance(59.0)
        assert dedup.is_duplicate("evt-1") is True

    def test_empty_string_never_duplicate(self) -> None:
        """An empty identifier is never treated as a duplicate."""
        dedup = EventDeduplicator()
        assert dedup.is_duplicate("") is False
        assert dedup.is_duplicate("") is False

    def test_none_never_duplicate(self) -> None:
        """A None identifier is never treated as a duplicate."""
        dedup = EventDeduplicator()
        assert dedup.is_duplicate(None) is False
        assert dedup.is_duplicate(None) is False

    def test_distinct_ids_independent(self) -> None:
        """Seeing one ID does not affect another."""
        dedup = EventDeduplicator()
        assert dedup.is_duplicate("a") is False
        assert dedup.is_duplicate("b") is False
        assert dedup.is_duplicate("a") is True
        assert dedup.is_duplicate("b") is True

    def test_ttl_measured_from_first_sight_not_refreshed(self) -> None:
        """The TTL window is anchored to when an ID was first seen.

        Repeated duplicate hits do not extend the window, so the entry
        expires 60s after the first sighting regardless of intervening
        checks.
        """
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate("live") is False  # first sight at t=0
        clock.advance(30.0)
        assert dedup.is_duplicate("live") is True  # still within window
        clock.advance(30.0)
        # 60s from first sight -> window has elapsed, accepted again.
        assert dedup.is_duplicate("live") is False

    def test_cleanup_bounds_memory_without_dropping_live_entries(self) -> None:
        """A cleanup pass evicts only expired entries, keeping live ones."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate("old") is False  # seen at t=0
        clock.advance(59.0)
        assert dedup.is_duplicate("new") is False  # seen at t=59
        # Cross the window for "old" and trigger cleanup.
        clock.advance(2.0)  # t=61: "old" expired, "new" still live (t=59)
        assert dedup.is_duplicate("new") is True
        assert dedup.is_duplicate("old") is False

    def test_expired_entry_evicted_and_reaccepted(self) -> None:
        """An entry past its TTL is treated as unseen again."""
        clock = _MutableClock()
        dedup = EventDeduplicator(ttl_seconds=60.0, clock=clock)

        assert dedup.is_duplicate("evt") is False
        clock.advance(61.0)
        assert dedup.is_duplicate("evt") is False
