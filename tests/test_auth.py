"""Unit tests for UserGroupAuthorizer (Slack User Group authorization).

Covers:
- Membership check happy path (member allowed, non-member denied)
- Lazy User Group handle → ID resolution
- Caching / TTL behavior (no re-fetch within TTL, refresh after expiry)
- Concurrent refresh serialization (double-check after lock)
- Failure handling: unresolvable group (deny-all + short retry TTL),
  Slack API error responses, and exceptions (fall back to stale cache)

Validates: Requirements 2.1, 2.2, 2.3
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from slack_agent_router.auth import UserGroupAuthorizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(*, members: list[str] | None = None, handle: str = "sage-all") -> AsyncMock:
    """Build a mock async Slack client that resolves *handle* and lists *members*.

    ``usergroups_list`` returns a single group matching *handle* with ID
    "S123", and ``usergroups_users_list`` returns *members*.
    """
    client = AsyncMock()
    client.usergroups_list.return_value = {
        "ok": True,
        "usergroups": [{"id": "S123", "handle": handle}],
    }
    client.usergroups_users_list.return_value = {
        "ok": True,
        "users": members if members is not None else [],
    }
    return client


def _controllable_clock(start: float = 1000.0):
    """Return (clock_fn, advance_fn) backed by a mutable value.

    The authorizer reads ``time.monotonic()``; tests patch that symbol with
    ``clock_fn`` and move time forward via ``advance_fn``.
    """
    now = {"t": start}

    def clock() -> float:
        return now["t"]

    def advance(seconds: float) -> None:
        now["t"] += seconds

    return clock, advance


# ---------------------------------------------------------------------------
# is_authorized — happy path
# ---------------------------------------------------------------------------


class TestIsAuthorized:
    async def test_member_is_authorized(self) -> None:
        client = _make_client(members=["U1", "U2"])
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U1") is True

    async def test_non_member_is_denied(self) -> None:
        client = _make_client(members=["U1", "U2"])
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U999") is False

    async def test_resolves_configured_handle(self) -> None:
        """The authorizer resolves the handle it was configured with."""
        client = _make_client(members=["U1"], handle="custom-group")
        authorizer = UserGroupAuthorizer(client, usergroup_handle="custom-group")

        assert await authorizer.is_authorized("U1") is True
        client.usergroups_users_list.assert_awaited_once_with(usergroup="S123")

    async def test_resolution_happens_once(self) -> None:
        """The handle → ID resolution is cached across refreshes."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"])
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            await authorizer.is_authorized("U1")
            advance(301)  # expire the cache
            await authorizer.is_authorized("U1")

        # Members refreshed twice, but the group ID was resolved only once.
        assert client.usergroups_list.await_count == 1
        assert client.usergroups_users_list.await_count == 2


# ---------------------------------------------------------------------------
# Caching / TTL
# ---------------------------------------------------------------------------


class TestCaching:
    async def test_no_refetch_within_ttl(self) -> None:
        """Repeated checks within the TTL hit the cache, not the API."""
        client = _make_client(members=["U1"])
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        await authorizer.is_authorized("U1")
        await authorizer.is_authorized("U1")
        await authorizer.is_authorized("U2")

        # One membership fetch total despite three checks.
        assert client.usergroups_users_list.await_count == 1

    async def test_refetch_after_ttl_expiry(self) -> None:
        """After the TTL expires, the member list is refreshed."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"])
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            assert await authorizer.is_authorized("U1") is True
            # Membership changes; new list drops U1 and adds U2.
            client.usergroups_users_list.return_value = {"ok": True, "users": ["U2"]}
            advance(301)
            assert await authorizer.is_authorized("U1") is False
            assert await authorizer.is_authorized("U2") is True

        assert client.usergroups_users_list.await_count == 2

    async def test_concurrent_refresh_serialized(self) -> None:
        """Concurrent expired checks trigger only one API refresh (lock + double-check).

        A gate holds the first refresh open inside the lock while a second
        caller queues on the lock. When the first refresh completes it bumps
        the cache expiry into the future, so the second caller re-checks
        after acquiring the lock and returns early without a second fetch.
        """
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"])
        release = asyncio.Event()

        async def gated_users_list(*args, **kwargs):
            await release.wait()
            return {"ok": True, "users": ["U1"]}

        client.usergroups_users_list.side_effect = gated_users_list
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            first = asyncio.create_task(authorizer.is_authorized("U1"))
            second = asyncio.create_task(authorizer.is_authorized("U1"))
            # Let both tasks run until they park: first inside the gated fetch
            # (holding the lock), second waiting on the lock.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # First refresh completes and pushes expiry to now + 300.
            release.set()
            results = await asyncio.gather(first, second)

        assert all(results)
        # Second caller hit the double-check-after-lock early return: one fetch only.
        assert client.usergroups_list.await_count == 1
        assert client.usergroups_users_list.await_count == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    async def test_unresolvable_group_denies_all(self) -> None:
        """A missing handle resolves to None → deny everyone."""
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S999", "handle": "some-other-group"}],
        }
        authorizer = UserGroupAuthorizer(client, usergroup_handle="sage-all")

        assert await authorizer.is_authorized("U1") is False
        # Never attempts to list members for an unresolved group.
        client.usergroups_users_list.assert_not_awaited()

    async def test_unresolvable_group_uses_short_retry_ttl(self) -> None:
        """When the group can't be resolved, the retry TTL is capped at 30s."""
        clock, advance = _controllable_clock()
        client = AsyncMock()
        client.usergroups_list.return_value = {"ok": True, "usergroups": []}
        # cache_ttl is large, but the deny-all path caps retry at min(ttl, 30).
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            await authorizer.is_authorized("U1")
            # Within 30s: still cached, no retry.
            advance(29)
            await authorizer.is_authorized("U1")
            assert client.usergroups_list.await_count == 1
            # Past 30s: retries resolution.
            advance(2)
            await authorizer.is_authorized("U1")
            assert client.usergroups_list.await_count == 2

    async def test_usergroups_list_api_error_denies(self) -> None:
        """An ok:false from usergroups.list → unresolved → deny."""
        client = AsyncMock()
        client.usergroups_list.return_value = {"ok": False, "error": "ratelimited"}
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U1") is False
        client.usergroups_users_list.assert_not_awaited()

    async def test_users_list_api_error_keeps_empty_cache(self) -> None:
        """An ok:false from usergroups.users.list leaves the member set empty."""
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S123", "handle": "sage-all"}],
        }
        client.usergroups_users_list.return_value = {"ok": False, "error": "fatal_error"}
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U1") is False

    async def test_exception_falls_back_to_stale_cache(self) -> None:
        """On a raised API exception, the previously cached members are retained."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"])
        authorizer = UserGroupAuthorizer(client, cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            # Prime the cache with U1 as a member.
            assert await authorizer.is_authorized("U1") is True
            # Next refresh raises — should keep the stale (U1) cache.
            client.usergroups_users_list.side_effect = RuntimeError("network down")
            advance(301)
            assert await authorizer.is_authorized("U1") is True

    async def test_exception_with_empty_cache_denies(self) -> None:
        """An exception on the very first refresh (empty cache) denies the user."""
        client = AsyncMock()
        client.usergroups_list.side_effect = RuntimeError("boom")
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U1") is False
