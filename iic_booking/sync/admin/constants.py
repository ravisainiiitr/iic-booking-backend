"""Shared constants for the Department Sync Operations Console."""

from __future__ import annotations

from django.conf import settings

# Online/offline is derived: last_heartbeat_at within this window ⇒ Online.
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 180


def heartbeat_timeout_seconds() -> int:
    return int(
        getattr(
            settings,
            "DSA_HEARTBEAT_TIMEOUT_SECONDS",
            DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        )
    )
