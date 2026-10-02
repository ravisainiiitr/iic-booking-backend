"""
Mobile app device sessions: issue, rotate, verify and revoke.

Access tokens ("iicm_…") are short-lived bearer keys sent as ``Authorization: Token iicm_…``.
Refresh tokens ("iicr_…") rotate on every use; presenting an already-rotated refresh token
outside the lost-response grace window revokes the whole session. Only SHA-256 hex digests
are persisted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from iic_booking.users.models import MobileDeviceSession, UserType

logger = logging.getLogger(__name__)

ACCESS_PREFIX = "iicm_"
REFRESH_PREFIX = "iicr_"
TOUCH_INTERVAL = timedelta(minutes=5)

REASON_LOGOUT = "logout"
REASON_RE_ENROLLED = "re_enrolled"
REASON_DEVICE_LIMIT = "device_limit"
REASON_REFRESH_REUSE = "refresh_reuse"
REASON_USER_REVOKED = "user_revoked"
REASON_USER_REVOKED_ALL = "user_revoked_all"
REASON_PASSWORD_CHANGED = "password_changed"
REASON_ADMIN_FORCE_LOGOUT = "admin_force_logout"
REASON_ADMIN_REVOKED = "admin_revoked"


class MobileSessionError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class IssuedTokens:
    session: MobileDeviceSession
    access_token: str
    refresh_token: str

    def as_response(self) -> dict:
        s = self.session
        return {
            "device_session_id": s.pk,
            "access_token": self.access_token,
            "access_expires_at": s.access_expires_at.isoformat(),
            "refresh_token": self.refresh_token,
            "refresh_expires_at": s.refresh_expires_at.isoformat(),
            "require_biometric": s.require_biometric,
        }


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def sessions_enabled() -> bool:
    return bool(getattr(settings, "MOBILE_DEVICE_SESSIONS_ENABLED", True))


def login_too_old_to_enroll(token_created) -> bool:
    """Enrolment needs a recent web login so a long-lived stolen web Token can't mint device sessions."""
    if token_created is None:
        return True
    max_age = timedelta(hours=_int_setting("MOBILE_ENROLL_MAX_TOKEN_AGE_HOURS", 12))
    return token_created < timezone.now() - max_age


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_access_token() -> str:
    return ACCESS_PREFIX + secrets.token_urlsafe(32)


def _new_refresh_token() -> str:
    return REFRESH_PREFIX + secrets.token_urlsafe(48)


def is_mobile_session(auth) -> bool:
    return isinstance(auth, MobileDeviceSession)


def is_admin_role(user) -> bool:
    """Superusers, Django staff and portal Main Administrators get the stricter mobile policy."""
    if user is None:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return getattr(user, "user_type", None) == UserType.ADMIN


def _lifetimes(user) -> tuple[timedelta, timedelta]:
    """(sliding refresh lifetime, absolute session lifetime) for this user."""
    if is_admin_role(user):
        return (
            timedelta(days=_int_setting("MOBILE_ADMIN_REFRESH_TOKEN_LIFETIME_DAYS", 14)),
            timedelta(days=_int_setting("MOBILE_ADMIN_SESSION_ABSOLUTE_MAX_DAYS", 30)),
        )
    return (
        timedelta(days=_int_setting("MOBILE_REFRESH_TOKEN_LIFETIME_DAYS", 60)),
        timedelta(days=_int_setting("MOBILE_SESSION_ABSOLUTE_MAX_DAYS", 180)),
    )


def _access_lifetime() -> timedelta:
    return timedelta(hours=_int_setting("MOBILE_ACCESS_TOKEN_LIFETIME_HOURS", 24))


def client_ip(request) -> str | None:
    """First parseable IP from X-Forwarded-For / REMOTE_ADDR, safe for GenericIPAddressField."""
    if request is None:
        return None
    meta = getattr(request, "META", {}) or {}
    candidates: list[str] = []
    forwarded = meta.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        candidates.extend(part.strip() for part in forwarded.split(",") if part.strip())
    remote = meta.get("REMOTE_ADDR")
    if remote:
        candidates.append(str(remote).strip())
    for raw in candidates:
        if raw.count(":") == 1 and raw.rsplit(":", 1)[-1].isdigit():
            raw = raw.rsplit(":", 1)[0]
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return None


def active_sessions_qs(user, now=None):
    now = now or timezone.now()
    return MobileDeviceSession.objects.filter(
        user=user,
        revoked_at__isnull=True,
        refresh_expires_at__gt=now,
        absolute_expires_at__gt=now,
    )


def enroll_device(
    user,
    *,
    device_id: str,
    platform: str,
    device_name: str = "",
    app_version: str = "",
    ip: str | None = None,
) -> IssuedTokens:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    max_devices = max(1, _int_setting("MOBILE_SESSION_MAX_DEVICES", 5))
    refresh_lifetime, absolute_lifetime = _lifetimes(user)

    with transaction.atomic():
        # Serialise concurrent enrolments for one user so the device limit holds.
        User.objects.select_for_update().filter(pk=user.pk).first()
        now = timezone.now()
        active = active_sessions_qs(user, now)

        for old_pk in active.filter(device_id=device_id).values_list("pk", flat=True):
            MobileDeviceSession.objects.filter(pk=old_pk, revoked_at__isnull=True).update(
                revoked_at=now, revoke_reason=REASON_RE_ENROLLED
            )
            logger.info("Mobile session revoked: user_id=%s session_id=%s reason=%s", user.pk, old_pk, REASON_RE_ENROLLED)

        remaining = list(
            active_sessions_qs(user, now)
            .annotate(_last_seen=Coalesce("last_used_at", "created_at"))
            .order_by("_last_seen", "pk")
            .values_list("pk", flat=True)
        )
        overflow = len(remaining) - max_devices + 1
        for old_pk in remaining[: max(0, overflow)]:
            MobileDeviceSession.objects.filter(pk=old_pk, revoked_at__isnull=True).update(
                revoked_at=now, revoke_reason=REASON_DEVICE_LIMIT
            )
            logger.info("Mobile session revoked: user_id=%s session_id=%s reason=%s", user.pk, old_pk, REASON_DEVICE_LIMIT)

        access_token = _new_access_token()
        refresh_token = _new_refresh_token()
        absolute_expires_at = now + absolute_lifetime
        session = MobileDeviceSession.objects.create(
            user=user,
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            app_version=app_version,
            access_hash=hash_token(access_token),
            access_expires_at=min(now + _access_lifetime(), absolute_expires_at),
            refresh_hash=hash_token(refresh_token),
            refresh_expires_at=min(now + refresh_lifetime, absolute_expires_at),
            absolute_expires_at=absolute_expires_at,
            require_biometric=is_admin_role(user),
            last_used_at=now,
            last_ip=ip,
        )

    logger.info(
        "Mobile session enrolled: user_id=%s session_id=%s platform=%s", user.pk, session.pk, platform
    )
    return IssuedTokens(session=session, access_token=access_token, refresh_token=refresh_token)


def refresh_session(*, device_id: str, refresh_token: str, ip: str | None = None) -> IssuedTokens:
    """Rotate a session's tokens. Raises MobileSessionError (HTTP 401) on any failure."""
    if not device_id or not refresh_token or not refresh_token.startswith(REFRESH_PREFIX):
        raise MobileSessionError("INVALID_REFRESH", "Invalid refresh token.")

    digest = hash_token(refresh_token)
    error: MobileSessionError | None = None
    issued: IssuedTokens | None = None

    with transaction.atomic():
        session = (
            MobileDeviceSession.objects.select_for_update()
            .filter(Q(refresh_hash=digest) | Q(prev_refresh_hash=digest))
            .first()
        )
        now = timezone.now()
        if session is None or not constant_time_compare(session.device_id, device_id):
            error = MobileSessionError("INVALID_REFRESH", "Invalid refresh token.")
        elif session.revoked_at is not None:
            error = MobileSessionError("SESSION_REVOKED", "This device has been signed out. Please sign in again.")
        elif session.refresh_expires_at <= now or session.absolute_expires_at <= now:
            error = MobileSessionError("SESSION_EXPIRED", "Your session has expired. Please sign in again.")
        elif not session.user.is_active:
            error = MobileSessionError("SESSION_REVOKED", "This account is not active.")
        else:
            reused = not constant_time_compare(session.refresh_hash, digest)
            grace = timedelta(seconds=_int_setting("MOBILE_REFRESH_REUSE_GRACE_SECONDS", 60))
            if reused and not (session.refreshed_at and now - session.refreshed_at <= grace):
                session.revoked_at = now
                session.revoke_reason = REASON_REFRESH_REUSE
                session.save(update_fields=["revoked_at", "revoke_reason"])
                logger.warning(
                    "Mobile refresh token reuse detected; session revoked: user_id=%s session_id=%s",
                    session.user_id,
                    session.pk,
                )
                error = MobileSessionError(
                    "REFRESH_REUSED", "This sign-in was used elsewhere and has been revoked. Please sign in again."
                )
            else:
                issued = _rotate(session, now=now, ip=ip)

    if error is not None:
        raise error
    return issued


def _rotate(session: MobileDeviceSession, *, now, ip: str | None) -> IssuedTokens:
    user = session.user
    refresh_lifetime, absolute_lifetime = _lifetimes(user)
    absolute_expires_at = min(session.absolute_expires_at, session.created_at + absolute_lifetime)
    access_grace = timedelta(seconds=_int_setting("MOBILE_ACCESS_GRACE_SECONDS", 120))

    access_token = _new_access_token()
    refresh_token = _new_refresh_token()

    session.prev_refresh_hash = session.refresh_hash
    session.refresh_hash = hash_token(refresh_token)
    session.prev_access_hash = session.access_hash
    # Never extend the old access token beyond its own expiry.
    session.prev_access_valid_until = min(now + access_grace, session.access_expires_at)
    session.access_hash = hash_token(access_token)
    session.absolute_expires_at = absolute_expires_at
    session.access_expires_at = min(now + _access_lifetime(), absolute_expires_at)
    session.refresh_expires_at = min(now + refresh_lifetime, absolute_expires_at)
    session.require_biometric = session.require_biometric or is_admin_role(user)
    session.refreshed_at = now
    session.last_used_at = now
    if ip:
        session.last_ip = ip
    session.save(
        update_fields=[
            "prev_refresh_hash",
            "refresh_hash",
            "prev_access_hash",
            "prev_access_valid_until",
            "access_hash",
            "absolute_expires_at",
            "access_expires_at",
            "refresh_expires_at",
            "require_biometric",
            "refreshed_at",
            "last_used_at",
            "last_ip",
        ]
    )
    return IssuedTokens(session=session, access_token=access_token, refresh_token=refresh_token)


def authenticate_mobile_access_key(key: str, *, ip: str | None = None, touch: bool = True):
    """Return (user, session) for a valid ``iicm_`` access key, else None."""
    if not key or not key.startswith(ACCESS_PREFIX):
        return None
    digest = hash_token(key)
    now = timezone.now()
    session = (
        MobileDeviceSession.objects.select_related("user")
        .filter(
            Q(access_hash=digest, access_expires_at__gt=now)
            | Q(prev_access_hash=digest, prev_access_valid_until__gt=now),
            revoked_at__isnull=True,
            refresh_expires_at__gt=now,
            absolute_expires_at__gt=now,
        )
        .first()
    )
    if session is None or not session.user.is_active:
        return None
    if touch and (session.last_used_at is None or session.last_used_at <= now - TOUCH_INTERVAL):
        _touch(session, now=now, ip=ip)
    return session.user, session


def _touch(session: MobileDeviceSession, *, now, ip: str | None) -> None:
    """Record activity at most once per TOUCH_INTERVAL so busy devices don't write on every request."""
    fields = {"last_used_at": now}
    if ip:
        fields["last_ip"] = ip
    updated = (
        MobileDeviceSession.objects.filter(pk=session.pk)
        .filter(Q(last_used_at__isnull=True) | Q(last_used_at__lte=now - TOUCH_INTERVAL))
        .update(**fields)
    )
    if updated:
        session.last_used_at = now
        if ip:
            session.last_ip = ip


def revoke_session(session: MobileDeviceSession, reason: str) -> bool:
    now = timezone.now()
    updated = MobileDeviceSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
        revoked_at=now, revoke_reason=reason
    )
    if updated:
        session.revoked_at = now
        session.revoke_reason = reason
        logger.info(
            "Mobile session revoked: user_id=%s session_id=%s reason=%s", session.user_id, session.pk, reason
        )
    return bool(updated)


def revoke_user_sessions(user, reason: str, *, exclude_session_id: int | None = None) -> int:
    qs = MobileDeviceSession.objects.filter(user=user, revoked_at__isnull=True)
    if exclude_session_id is not None:
        qs = qs.exclude(pk=exclude_session_id)
    count = qs.update(revoked_at=timezone.now(), revoke_reason=reason)
    if count:
        logger.info(
            "Mobile sessions revoked: user_id=%s count=%s reason=%s", getattr(user, "pk", user), count, reason
        )
    return count
