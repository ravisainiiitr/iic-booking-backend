"""Control-plane constants for Department Sync Agent APIs."""

from __future__ import annotations

from django.conf import settings

from iic_booking.sync.admin.constants import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    heartbeat_timeout_seconds,
)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60
DEFAULT_ACCESS_TOKEN_LIFETIME_HOURS = 24 * 30  # 30 days; rotation supported via re-enroll/rotate
BOOTSTRAP_SCHEMA_VERSION = 1  # portal bootstrap document shape


def heartbeat_interval_seconds() -> int:
    return int(
        getattr(
            settings,
            "DSA_HEARTBEAT_INTERVAL_SECONDS",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )
    )


def access_token_lifetime_hours() -> int:
    return int(
        getattr(
            settings,
            "DSA_ACCESS_TOKEN_LIFETIME_HOURS",
            DEFAULT_ACCESS_TOKEN_LIFETIME_HOURS,
        )
    )


def bootstrap_schema_version() -> int:
    return int(
        getattr(
            settings,
            "DSA_BOOTSTRAP_SCHEMA_VERSION",
            BOOTSTRAP_SCHEMA_VERSION,
        )
    )


DEFAULT_UPLOAD_CHUNK_SIZE_BYTES = 1024 * 1024  # 1 MiB
DEFAULT_UPLOAD_SESSION_TTL_HOURS = 24


def upload_chunk_size_bytes() -> int:
    return int(
        getattr(
            settings,
            "DSA_UPLOAD_CHUNK_SIZE_BYTES",
            DEFAULT_UPLOAD_CHUNK_SIZE_BYTES,
        )
    )


def upload_session_ttl_hours() -> int:
    return int(
        getattr(
            settings,
            "DSA_UPLOAD_SESSION_TTL_HOURS",
            DEFAULT_UPLOAD_SESSION_TTL_HOURS,
        )
    )


DEFAULT_SIGNATURE_REPLAY_WINDOW_SECONDS = 300
DEFAULT_CERTIFICATE_RENEWAL_DAYS = 30


def signature_replay_window_seconds() -> int:
    return int(
        getattr(
            settings,
            "DSA_SIGNATURE_REPLAY_WINDOW_SECONDS",
            DEFAULT_SIGNATURE_REPLAY_WINDOW_SECONDS,
        )
    )


# Milestone 15 — monitoring defaults
DEFAULT_MONITORING_HISTORY_RETENTION_DAYS = 90
DEFAULT_ALERT_EXPIRY_HOURS = 24


def monitoring_history_retention_days() -> int:
    return int(
        getattr(
            settings,
            "DSA_MONITORING_HISTORY_RETENTION_DAYS",
            DEFAULT_MONITORING_HISTORY_RETENTION_DAYS,
        )
    )


def alert_expiry_hours() -> int:
    return int(
        getattr(
            settings,
            "DSA_ALERT_EXPIRY_HOURS",
            DEFAULT_ALERT_EXPIRY_HOURS,
        )
    )


def certificate_renewal_days() -> int:
    return int(
        getattr(
            settings,
            "DSA_CERTIFICATE_RENEWAL_DAYS",
            DEFAULT_CERTIFICATE_RENEWAL_DAYS,
        )
    )


# Milestone 13 recovery event codes
EVENT_RECOVERY_RECONCILED = "REC-2001"
EVENT_RECOVERY_CONFLICT = "REC-3001"
EVENT_RECOVERY_INTEGRITY = "REC-4001"

# Milestone 16 update event codes
EVENT_RELEASE_CREATED = "UPD-CREATE"
EVENT_RELEASE_PUBLISHED = "UPD-PUBLISH"
EVENT_UPDATE_DEPLOYED = "UPD-DEPLOY"
EVENT_UPDATE_ROLLBACK = "UPD-ROLLBACK"
EVENT_UPDATE_DISCOVER = "UPD-DISCOVER"
EVENT_UPDATE_STATUS = "UPD-STATUS"

__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "heartbeat_timeout_seconds",
    "heartbeat_interval_seconds",
    "access_token_lifetime_hours",
    "bootstrap_schema_version",
    "upload_chunk_size_bytes",
    "upload_session_ttl_hours",
    "signature_replay_window_seconds",
    "certificate_renewal_days",
    "monitoring_history_retention_days",
    "alert_expiry_hours",
    "EVENT_RECOVERY_RECONCILED",
    "EVENT_RECOVERY_CONFLICT",
    "EVENT_RECOVERY_INTEGRITY",
]
