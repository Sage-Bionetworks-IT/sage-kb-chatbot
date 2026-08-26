"""Authorization check via Slack User Group membership.

Checks whether a user belongs to a configured Slack User Group
(e.g. "sage-all") by calling the Slack ``usergroups.users.list`` API
and caching the membership set with a short TTL to avoid hammering
the API on every incoming event.

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


class UserGroupAuthorizer:
    """Checks user membership in a Slack User Group with a cached member list.

    The authorizer resolves the User Group handle (e.g. ``sage-all``) to its
    ID on first use, then periodically refreshes the member list from Slack.

    Parameters:
        slack_client: An async Slack WebClient (``slack_sdk.web.async_client.AsyncWebClient``).
        usergroup_handle: The handle of the Slack User Group to authorize against
            (without the ``@`` prefix). Defaults to ``"sage-all"``.
        cache_ttl_seconds: How long to cache the member list before refreshing.
    """

    def __init__(
        self,
        slack_client: Any,
        usergroup_handle: str = "sage-all",
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._client = slack_client
        self._usergroup_handle = usergroup_handle
        self._cache_ttl = cache_ttl_seconds

        # Resolved lazily on first check.
        self._usergroup_id: str | None = None

        # Cached member set and its expiry timestamp.
        self._members: set[str] = set()
        self._cache_expires_at: float = 0.0

        # Serializes concurrent refresh attempts.
        self._refresh_lock = asyncio.Lock()

    async def is_authorized(self, user_id: str) -> bool:
        """Return True if *user_id* is a member of the authorized User Group.

        Transparently refreshes the cached member list when the TTL expires.
        On API errors the stale cache is used (fail-open for existing members,
        fail-closed for unknown users when the cache is empty).
        """
        now = time.monotonic()
        if now >= self._cache_expires_at:
            await self._refresh_members()

        return user_id in self._members

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_members(self) -> None:
        """Refresh the cached member list from the Slack API.

        Uses a lock so that concurrent callers don't all hit the API at
        once after TTL expiry.
        """
        async with self._refresh_lock:
            # Double-check after acquiring the lock — another coroutine
            # may have already refreshed while we were waiting.
            now = time.monotonic()
            if now < self._cache_expires_at:
                return

            try:
                if self._usergroup_id is None:
                    self._usergroup_id = await self._resolve_usergroup_id()

                if self._usergroup_id is None:
                    logger.warning(
                        "Could not resolve User Group '%s' — denying all users",
                        self._usergroup_handle,
                    )
                    self._members = set()
                    # Set a short TTL so we retry soon rather than permanently blocking.
                    self._cache_expires_at = now + min(self._cache_ttl, 30.0)
                    return

                response = await self._client.usergroups_users_list(usergroup=self._usergroup_id)
                if response.get("ok"):
                    self._members = set(response.get("users", []))
                    logger.info(
                        "Refreshed User Group '%s' membership: %d members",
                        self._usergroup_handle,
                        len(self._members),
                    )
                else:
                    logger.warning(
                        "Slack API error refreshing User Group '%s': %s",
                        self._usergroup_handle,
                        response.get("error", "unknown"),
                    )
            except Exception:
                logger.exception(
                    "Failed to refresh User Group '%s' membership — using stale cache",
                    self._usergroup_handle,
                )

            # Always bump the expiry so we don't retry on every request.
            self._cache_expires_at = time.monotonic() + self._cache_ttl

    async def _resolve_usergroup_id(self) -> str | None:
        """Resolve the User Group handle to its Slack ID.

        Calls ``usergroups.list`` and finds the group matching the
        configured handle.
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
                if group.get("handle") == self._usergroup_handle:
                    logger.info(
                        "Resolved User Group '%s' → ID '%s'",
                        self._usergroup_handle,
                        group["id"],
                    )
                    return group["id"]

            logger.warning(
                "User Group with handle '%s' not found in workspace",
                self._usergroup_handle,
            )
            return None
        except Exception:
            logger.exception("Failed to resolve User Group '%s'", self._usergroup_handle)
            return None
