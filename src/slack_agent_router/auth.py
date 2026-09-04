"""Authorization check via Slack User Group membership.

Fail-closed authorization: by default **no one** is allowed. Access is
granted only when either:

* the configured group list contains the wildcard ``"*"`` (open to every
  workspace user), or
* the user is a member of one of the configured User Groups.

With no configuration the bot denies everyone, so a missing or forgotten
setting fails safe rather than exposing internal content.

Membership for each configured group is resolved via the Slack
``usergroups.list`` / ``usergroups.users.list`` APIs and cached with a
short TTL to avoid hammering the API on every incoming event.

Requirements: 2.1, 2.2, 2.3
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Default cache TTL in seconds — membership is refreshed at most this often.
_DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes

# Shorter TTL used when one or more groups could not be resolved, so we
# retry resolution soon rather than caching a bad result for the full TTL.
_UNRESOLVED_RETRY_TTL_SECONDS = 30.0


def _normalize_handles(handles: str | list[str] | None) -> list[str]:
    """Normalize a handle argument to a de-duplicated list of non-empty handles.

    Accepts a single handle string, a list of handles, or ``None``.
    """
    if handles is None:
        return []
    if isinstance(handles, str):
        handles = [handles]
    return list(dict.fromkeys(h.strip() for h in handles if h and h.strip()))


class UserGroupAuthorizer:
    """Authorizes users against a list of Slack User Groups (fail-closed).

    Access is **denied by default**. A user is authorized only when:

    * the configured group list contains the wildcard ``"*"`` — the bot is
      explicitly open to everyone; or
    * the user is a member of at least one configured group.

    When the list is empty (and contains no ``"*"``), every user is denied.
    This is the safe failure mode: a missing configuration locks the bot
    down rather than exposing it.

    Each configured group handle is resolved to its ID on first use, and the
    combined member set is refreshed from Slack on a TTL.

    Parameters:
        slack_client: An async Slack WebClient
            (``slack_sdk.web.async_client.AsyncWebClient``).
        include_handles: Handles (without ``@``) of groups whose members are
            allowed, or the single wildcard ``"*"`` to allow everyone.
            Accepts a single handle string or a list.
        cache_ttl_seconds: How long to cache the member list before refreshing.
        usergroup_handle: Deprecated single-handle alias for
            ``include_handles``, kept for backward compatibility.
    """

    #: Wildcard handle that grants access to every workspace user.
    WILDCARD = "*"

    def __init__(
        self,
        slack_client: Any,
        include_handles: str | list[str] | None = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        *,
        usergroup_handle: str | None = None,
    ) -> None:
        self._client = slack_client

        include_source = include_handles if include_handles is not None else usergroup_handle
        self._include_handles = _normalize_handles(include_source)
        self._allow_all = self.WILDCARD in self._include_handles

        self._cache_ttl = cache_ttl_seconds

        # Resolved lazily on first check: handle → ID. Missing keys mean the
        # handle has not been resolved yet or could not be found.
        self._usergroup_ids: dict[str, str] = {}

        # Cached union member set and its expiry timestamp.
        self._include_members: set[str] = set()
        self._cache_expires_at: float = 0.0

        # Serializes concurrent refresh attempts.
        self._refresh_lock = asyncio.Lock()

    @property
    def allow_all(self) -> bool:
        """Whether the bot is explicitly open to all users (wildcard configured)."""
        return self._allow_all

    async def is_authorized(self, user_id: str) -> bool:
        """Return True if *user_id* is allowed to use the bot.

        Authorized when the group list contains the ``"*"`` wildcard, or the
        user is a member of a configured group. Denied otherwise (including
        when nothing is configured).

        Transparently refreshes the cached member list when the TTL expires.
        On API errors the stale cache is used.
        """
        # Wildcard → allow everyone, no API calls needed.
        if self._allow_all:
            return True

        # Fail closed: with no groups configured, deny everyone.
        if not self._include_handles:
            return False

        now = time.monotonic()
        if now >= self._cache_expires_at:
            await self._refresh_members()

        return user_id in self._include_members

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_members(self) -> None:
        """Refresh the cached include member set from the Slack API.

        Uses a lock so that concurrent callers don't all hit the API at
        once after TTL expiry. On any failure the previously cached set is
        retained (stale-cache fallback), and the retry TTL is shortened.
        """
        async with self._refresh_lock:
            # Double-check after acquiring the lock — another coroutine may
            # have already refreshed while we were waiting.
            now = time.monotonic()
            if now < self._cache_expires_at:
                return

            all_resolved = True
            try:
                include_members, include_ok = await self._collect_members(self._include_handles)
                all_resolved = include_ok

                # Only replace the set on a successful refresh so a transient
                # failure doesn't wipe a previously good member set.
                if include_ok:
                    self._include_members = include_members
            except Exception:
                logger.exception("Failed to refresh User Group membership — using stale cache")
                all_resolved = False

            # Retry sooner when something failed to resolve; otherwise cache
            # for the full TTL.
            ttl = self._cache_ttl if all_resolved else min(self._cache_ttl, _UNRESOLVED_RETRY_TTL_SECONDS)
            self._cache_expires_at = time.monotonic() + ttl

    async def _collect_members(self, handles: list[str]) -> tuple[set[str], bool]:
        """Return the union of members across *handles* and whether all resolved.

        The boolean is True only if every handle resolved to an ID and its
        member list was fetched successfully.
        """
        members: set[str] = set()
        if not handles:
            return members, True

        all_ok = True
        for handle in handles:
            group_id = self._usergroup_ids.get(handle)
            if group_id is None:
                group_id = await self._resolve_usergroup_id(handle)
                if group_id is not None:
                    self._usergroup_ids[handle] = group_id

            if group_id is None:
                logger.warning("Could not resolve User Group '%s'", handle)
                all_ok = False
                continue

            group_members = await self._fetch_group_members(handle, group_id)
            if group_members is None:
                all_ok = False
                continue
            members |= group_members

        return members, all_ok

    async def _fetch_group_members(self, handle: str, group_id: str) -> set[str] | None:
        """Fetch the member set for a resolved group, or None on API error."""
        response = await self._client.usergroups_users_list(usergroup=group_id)
        if response.get("ok"):
            members = set(response.get("users", []))
            logger.info("Refreshed User Group '%s' membership: %d members", handle, len(members))
            return members
        logger.warning(
            "Slack API error refreshing User Group '%s': %s",
            handle,
            response.get("error", "unknown"),
        )
        return None

    async def _resolve_usergroup_id(self, handle: str) -> str | None:
        """Resolve a User Group handle to its Slack ID.

        Calls ``usergroups.list`` and finds the group matching *handle*.
        """
        try:
            response = await self._client.usergroups_list()
            if not response.get("ok"):
                logger.warning(
                    "Slack API error listing User Groups: %s",
                    response.get("error", "unknown"),
                )
                return None

            for group in response.get("usergroups", []):
                if group.get("handle") == handle:
                    logger.info("Resolved User Group '%s' → ID '%s'", handle, group["id"])
                    return group["id"]

            logger.warning("User Group with handle '%s' not found in workspace", handle)
            return None
        except Exception:
            logger.exception("Failed to resolve User Group '%s'", handle)
            return None
