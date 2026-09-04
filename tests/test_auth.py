"""Unit tests for UserGroupAuthorizer (Slack User Group authorization).

Authorization is fail-closed. Covers:
- Deny-all default (nothing configured)
- allow_all flag (explicit open access, no API calls)
- Include-list membership (member allowed, non-member denied)
- Multiple include groups (union membership)
- Lazy User Group handle → ID resolution (and caching across refreshes)
- Caching / TTL behavior (no re-fetch within TTL, refresh after expiry)
- Concurrent refresh serialization (double-check after lock)
- Failure handling: unresolvable group (short retry TTL), Slack API
  error responses, and exceptions (fall back to stale cache)

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
    """Build a mock async Slack client resolving a single *handle* with *members*.

    ``usergroups_list`` returns a single group matching *handle* with ID
    "S0", and ``usergroups_users_list`` returns *members* for it.
    """
    return _make_multi_client({handle: members if members is not None else []})


def _make_multi_client(groups: dict[str, list[str]]) -> AsyncMock:
    """Build a mock async Slack client for multiple groups.

    ``groups`` maps handle → member list. Each handle is assigned a synthetic
    ID ("S<index>"); ``usergroups_list`` returns all of them and
    ``usergroups_users_list`` returns the members for the requested ID.
    """
    handle_to_id: dict[str, str] = {}
    id_to_members: dict[str, list[str]] = {}
    for i, (handle, members) in enumerate(groups.items()):
        gid = f"S{i}"
        handle_to_id[handle] = gid
        id_to_members[gid] = members

    client = AsyncMock()
    client.usergroups_list.return_value = {
        "ok": True,
        "usergroups": [{"id": gid, "handle": h} for h, gid in handle_to_id.items()],
    }

    async def _users_list(*, usergroup: str):
        return {"ok": True, "users": id_to_members.get(usergroup, [])}

    client.usergroups_users_list.side_effect = _users_list
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
# Deny-all default (fail closed)
# ---------------------------------------------------------------------------


class TestDenyAllDefault:
    async def test_no_config_denies_everyone(self) -> None:
        """With nothing configured, every user is denied and no API is called."""
        client = _make_client(members=["U1"])
        authorizer = UserGroupAuthorizer(client)

        assert await authorizer.is_authorized("U1") is False
        assert await authorizer.is_authorized("anyone") is False
        # Fail-closed with no groups → no need to hit the Slack API at all.
        client.usergroups_list.assert_not_awaited()

    def test_allow_all_property_defaults_false(self) -> None:
        client = _make_client()
        assert UserGroupAuthorizer(client).allow_all is False
        assert UserGroupAuthorizer(client, include_handles=["*"]).allow_all is True


# ---------------------------------------------------------------------------
# Wildcard "*" (allow all)
# ---------------------------------------------------------------------------


class TestWildcardAllowAll:
    async def test_wildcard_authorizes_everyone(self) -> None:
        """A "*" entry authorizes any user without any API calls."""
        client = _make_client(members=[])
        authorizer = UserGroupAuthorizer(client, include_handles=["*"])

        assert await authorizer.is_authorized("anyone") is True
        client.usergroups_list.assert_not_awaited()

    async def test_wildcard_alongside_groups_still_allows_all(self) -> None:
        """If "*" is present, other listed groups are irrelevant — allow all."""
        client = _make_client(members=["U1"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team", "*"])

        assert authorizer.allow_all is True
        assert await authorizer.is_authorized("someone-else") is True
        client.usergroups_list.assert_not_awaited()


# ---------------------------------------------------------------------------
# Include list
# ---------------------------------------------------------------------------


class TestIncludeList:
    async def test_member_is_authorized(self) -> None:
        client = _make_client(members=["U1", "U2"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U1") is True

    async def test_non_member_is_denied(self) -> None:
        client = _make_client(members=["U1", "U2"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U999") is False

    async def test_union_across_multiple_include_groups(self) -> None:
        """A user in ANY authorized group is allowed."""
        client = _make_multi_client({"it-team": ["U1"], "sec-team": ["U2"]})
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team", "sec-team"])

        assert await authorizer.is_authorized("U1") is True
        assert await authorizer.is_authorized("U2") is True
        assert await authorizer.is_authorized("U3") is False

    async def test_deprecated_usergroup_handle_alias(self) -> None:
        """The legacy usergroup_handle kwarg maps to the include list."""
        client = _make_client(members=["U1"], handle="legacy")
        authorizer = UserGroupAuthorizer(client, usergroup_handle="legacy")

        assert await authorizer.is_authorized("U1") is True
        assert await authorizer.is_authorized("U2") is False


# ---------------------------------------------------------------------------
# Resolution caching
# ---------------------------------------------------------------------------


class TestResolution:
    async def test_resolution_happens_once(self) -> None:
        """The handle → ID resolution is cached across refreshes."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

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
        client = _make_client(members=["U1"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

        await authorizer.is_authorized("U1")
        await authorizer.is_authorized("U1")
        await authorizer.is_authorized("U2")

        # One membership fetch total despite three checks.
        assert client.usergroups_users_list.await_count == 1

    async def test_refetch_after_ttl_expiry(self) -> None:
        """After the TTL expires, the member list is refreshed."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            assert await authorizer.is_authorized("U1") is True
            # Membership changes; new list drops U1 and adds U2.
            client.usergroups_users_list.side_effect = None
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
        client = _make_client(members=["U1"], handle="it-team")
        release = asyncio.Event()

        async def gated_users_list(*args, **kwargs):
            await release.wait()
            return {"ok": True, "users": ["U1"]}

        client.usergroups_users_list.side_effect = gated_users_list
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

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
    async def test_unresolvable_include_group_denies(self) -> None:
        """A missing include handle resolves to nothing → member denied."""
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S999", "handle": "some-other-group"}],
        }
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U1") is False
        # Never attempts to list members for an unresolved group.
        client.usergroups_users_list.assert_not_awaited()

    async def test_not_found_group_uses_full_ttl(self) -> None:
        """A durably missing handle (listed fine, no match) uses the full TTL.

        A not-found handle is not a transient condition — the union already
        reflects it (the group contributes nothing), so there's no reason to
        re-list rapidly.
        """
        clock, advance = _controllable_clock()
        client = AsyncMock()
        client.usergroups_list.return_value = {"ok": True, "usergroups": []}
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            await authorizer.is_authorized("U1")
            # Just past the short (30s) retry window: still cached under the full TTL.
            advance(31)
            await authorizer.is_authorized("U1")
            assert client.usergroups_list.await_count == 1
            # Past the full TTL: refreshes.
            advance(300)
            await authorizer.is_authorized("U1")
            assert client.usergroups_list.await_count == 2

    async def test_transient_list_error_uses_short_retry_ttl(self) -> None:
        """A transient usergroups.list API error caps the retry TTL at 30s."""
        clock, advance = _controllable_clock()
        client = AsyncMock()
        client.usergroups_list.return_value = {"ok": False, "error": "ratelimited"}
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

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
        """An ok:false from usergroups.list → unresolved → deny an include member."""
        client = AsyncMock()
        client.usergroups_list.return_value = {"ok": False, "error": "ratelimited"}
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U1") is False
        client.usergroups_users_list.assert_not_awaited()

    async def test_users_list_api_error_keeps_empty_cache(self) -> None:
        """An ok:false from usergroups.users.list leaves the member set empty."""
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S123", "handle": "it-team"}],
        }
        client.usergroups_users_list.return_value = {"ok": False, "error": "fatal_error"}
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U1") is False

    async def test_exception_falls_back_to_stale_cache(self) -> None:
        """On a raised API exception, the previously cached members are retained."""
        clock, advance = _controllable_clock()
        client = _make_client(members=["U1"], handle="it-team")
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

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
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"])

        assert await authorizer.is_authorized("U1") is False

    async def test_one_missing_group_does_not_deny_resolved_group(self) -> None:
        """A not-found handle drops out; members of the resolvable group still pass.

        Regression: previously a single unresolvable handle set include_ok
        False and left the union uncommitted, denying everyone on a cold cache.
        """
        client = AsyncMock()
        # good-team resolves to S1 with member U1; typo-team is not found.
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S1", "handle": "good-team"}],
        }
        client.usergroups_users_list.return_value = {"ok": True, "users": ["U1"]}
        authorizer = UserGroupAuthorizer(client, include_handles=["good-team", "typo-team"])

        assert await authorizer.is_authorized("U1") is True  # resolved group works
        assert await authorizer.is_authorized("U2") is False  # not a member

    async def test_not_found_group_drops_from_union_when_paired(self) -> None:
        """A not-found handle contributes nothing; a paired resolvable group still
        authorizes its members and its refresh reflects membership changes.

        Regression: a single unresolvable handle no longer blocks committing
        the union built from the groups that did resolve.
        """
        clock, advance = _controllable_clock()
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S1", "handle": "good-team"}],
        }
        client.usergroups_users_list.return_value = {"ok": True, "users": ["U1"]}
        authorizer = UserGroupAuthorizer(client, include_handles=["good-team", "missing-team"], cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            assert await authorizer.is_authorized("U1") is True
            # good-team's membership changes on the next refresh — the union is
            # recomputed and committed (the missing handle doesn't block it).
            client.usergroups_users_list.return_value = {"ok": True, "users": ["U2"]}
            advance(301)
            assert await authorizer.is_authorized("U1") is False
            assert await authorizer.is_authorized("U2") is True

    async def test_transient_fetch_error_keeps_stale_union(self) -> None:
        """A transient members-fetch error retains the previously cached union."""
        clock, advance = _controllable_clock()
        client = AsyncMock()
        client.usergroups_list.return_value = {
            "ok": True,
            "usergroups": [{"id": "S1", "handle": "it-team"}],
        }
        client.usergroups_users_list.return_value = {"ok": True, "users": ["U1"]}
        authorizer = UserGroupAuthorizer(client, include_handles=["it-team"], cache_ttl_seconds=300)

        with patch("slack_agent_router.auth.time.monotonic", side_effect=clock):
            assert await authorizer.is_authorized("U1") is True
            # The group still resolves, but the member fetch now transiently fails.
            client.usergroups_users_list.return_value = {"ok": False, "error": "ratelimited"}
            advance(301)
            # Stale union retained → U1 still authorized despite the blip.
            assert await authorizer.is_authorized("U1") is True
