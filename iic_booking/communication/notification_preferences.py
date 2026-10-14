"""Per-user notification settings: which categories reach a user by email and in the portal inbox.

Every email and in-app notification falls into one category (from its template code, or for in-app notifications
without a template from their metadata and title). Service messages a user must receive (account and security,
booking changes, results, money) are locked on; everything else can be switched off per channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings as django_settings
from django.core import signing
from django.utils import timezone

EMAIL = "email"
IN_APP = "in_app"
CHANNELS = (EMAIL, IN_APP)
OTHER = "other"


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str
    locked: bool = False
    staff_only: bool = False
    # Sent by email only (no inbox notice, no daily digest).
    email_only: bool = False
    report_recipients_only: bool = False


MONTHLY_REPORTS = "monthly_reports"

CATEGORIES: tuple[Category, ...] = (
    Category(
        "account",
        "Account and security",
        "Sign-in, registration and approvals, supervisor links, account end date.",
        locked=True,
    ),
    Category(
        "bookings",
        "Booking updates",
        "Booking confirmed, cancelled, rescheduled or refunded, disruptions and charge changes.",
        locked=True,
    ),
    Category(
        "results",
        "Results and completion",
        "Booking completed, results ready to download, sample collection and disposal.",
        locked=True,
    ),
    Category(
        "payments",
        "Wallet and payments",
        "Credits, debits, recharges and wallet join requests.",
        locked=True,
    ),
    Category("reminders", "Reminders", "Upcoming slots, sample submission deadlines and low wallet balance."),
    Category("messages", "Messages and comments", "Messages from the lab and comments on your bookings."),
    Category("waitlist", "Waitlist and slot alerts", "Slots opening up on equipment you are waiting for."),
    Category("training", "Training and duties", "Training calls and nominations, certifications, TA and operator duties."),
    Category(
        "staff",
        "Lab operations",
        "Urgent requests, overdue completions, leave, demo requests and monthly reports for your equipment.",
        staff_only=True,
    ),
    Category("announcements", "Announcements", "New equipment, notices and portal news."),
    Category(
        MONTHLY_REPORTS,
        "Monthly reports",
        "A summary email on the 1st of each month: last month's bookings, hours and spend for you and your "
        "equipment or department.",
        email_only=True,
        report_recipients_only=True,
    ),
)
BY_KEY = {c.key: c for c in CATEGORIES}

# First match wins; template codes are matched in full against these patterns.
_TEMPLATE_RULES: tuple[tuple[str, str], ...] = (
    (r"monthly_usage_report_.*", MONTHLY_REPORTS),
    (r"(registration|account|supervisor_invite|password|otp|login|verification|programme_extension)_.*", "account"),
    (r"wallet_low_balance_email", "reminders"),
    (r"(booking_reminder|sample_submission_deadline_reminder)_.*", "reminders"),
    (r"booking_(lab_message|lab_staff_message|comment)_.*", "messages"),
    (r"(booking_completed|booking_results_available|sample_disposed|sample_collection)_.*", "results"),
    (r"(waitlist_slots_available|waitlist_short_notice_slot_available|waitlist_opt_out)_.*", "waitlist"),
    (r"(urgent_booking_request_oic_alert|urgent_booking_allocated_operator|booking_completion_overdue_staff|oic_monthly_report|"
     r"operator_leave_submitted_oic|operator_unavailability_intimation_oic|demo_request_submitted_oic|"
     r"demo_request_escalated|equipment_addition_request)_?.*", "staff"),
    (r"(ta_|training_|nomination_|student_nomination|operator_leave|operator_unavailability|duty_|demo_|certification).*",
     "training"),
    (r"(wallet|sric|payment|refund_to_wallet|credit_facility)_?.*", "payments"),
    (r"(notice|announcement|equipment_added|new_equipment|newsletter|flash_message)_?.*", "announcements"),
    (r"(booking|urgent_booking|repeat_sample|operator_unavailable|waitlist|disruption)_?.*", "bookings"),
)
_COMPILED = tuple((re.compile(p), c) for p, c in _TEMPLATE_RULES)

_TYPE_RULES = {
    "reminder": "reminders",
    "results_available": "results",
    "equipment_addition": "announcements",
    "ta_duty_allocation": "training",
}
_TITLE_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(results?|completed)\b", re.I), "results"),
    (re.compile(r"\b(messages?|comments?|questions?|repl(y|ied|ies))\b", re.I), "messages"),
    (re.compile(r"\b(waitlist|slots? (available|opened))\b", re.I), "waitlist"),
    (re.compile(r"\b(training|nomination|certif|duty|duties|demo)\w*", re.I), "training"),
    (re.compile(r"\b(wallet|recharges?|credit(ed)?|debit(ed)?|balance|payments?)\b", re.I), "payments"),
    (re.compile(r"\b(remind)\w*", re.I), "reminders"),
    (re.compile(r"\b(notices?|announcements?|new equipment)\b", re.I), "announcements"),
    (re.compile(r"\b(registration|account|approv\w*|supervisor)\b", re.I), "account"),
    (re.compile(r"\b(booking|slot|reschedul|cancel|refund)\w*", re.I), "bookings"),
)


def category_for(template_code: str | None = None, metadata: dict | None = None, title: str = "") -> str:
    """Category key of a notification; ``OTHER`` when nothing matches (always delivered)."""
    meta = metadata or {}
    explicit = str(meta.get("notification_category") or "").strip()
    if explicit in BY_KEY:
        return explicit
    if meta.get("staff_recipient"):
        return "staff" if meta.get("event_type") not in ("COMMENT",) else "messages"
    code = (template_code or "").strip().lower()
    if code:
        for pattern, category in _COMPILED:
            if pattern.fullmatch(code):
                return category
    mapped = _TYPE_RULES.get(str(meta.get("notification_type") or "").strip())
    if mapped:
        return mapped
    for pattern, category in _TITLE_RULES:
        if title and pattern.search(title):
            return category
    return OTHER


def _stored(user) -> dict[str, dict[str, bool]]:
    from .models import NotificationPreference

    return {
        p.category: {EMAIL: p.email_enabled, IN_APP: p.in_app_enabled, DIGEST: p.email_digest}
        for p in NotificationPreference.objects.filter(user=user)
    }


DIGEST = "digest"
SEND = "send"
SKIP = "skip"


@dataclass(frozen=True)
class EmailDecision:
    action: str  # SEND, SKIP or "defer"
    deliver_after: datetime | None = None
    reason: str = ""

    @property
    def deferred(self) -> bool:
        return self.action == "defer"


def delivery_settings(user):
    """The user's quiet hours and digest hour, or None when they never changed them."""
    from .models import NotificationDeliverySettings

    if user is None or not getattr(user, "pk", None):
        return None
    return NotificationDeliverySettings.objects.filter(user=user).first()


def _local(now: datetime) -> datetime:
    return timezone.localtime(now)


def in_quiet_hours(settings_row, now: datetime | None = None) -> bool:
    if settings_row is None or not settings_row.quiet_hours_enabled:
        return False
    t = _local(now or timezone.now()).time()
    start, end = settings_row.quiet_start, settings_row.quiet_end
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def quiet_hours_end(settings_row, now: datetime | None = None) -> datetime:
    """The next moment quiet hours finish (local time)."""
    local_now = _local(now or timezone.now())
    end = local_now.replace(
        hour=settings_row.quiet_end.hour, minute=settings_row.quiet_end.minute, second=0, microsecond=0
    )
    if end <= local_now:
        end += timedelta(days=1)
    return end


def next_digest_time(settings_row, now: datetime | None = None) -> datetime:
    hour = settings_row.digest_hour if settings_row is not None else DEFAULT_DIGEST_HOUR
    local_now = _local(now or timezone.now())
    at = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if at <= local_now:
        at += timedelta(days=1)
    return at


DEFAULT_DIGEST_HOUR = 8


def email_decision(user, category: str, now: datetime | None = None) -> EmailDecision:
    """Send now, skip (switched off), or hold an optional email for the digest or until quiet hours end.

    Locked categories and ``other`` always go out immediately.
    """
    cat = BY_KEY.get(category)
    if cat is None or cat.locked or user is None or not getattr(user, "pk", None):
        return EmailDecision(SEND)
    from .models import NotificationPreference

    pref = NotificationPreference.objects.filter(user=user, category=category).only(
        "email_enabled", "email_digest"
    ).first()
    if pref is not None and not pref.email_enabled:
        return EmailDecision(SKIP)
    settings_row = delivery_settings(user)
    if pref is not None and pref.email_digest and not cat.email_only:
        return EmailDecision("defer", next_digest_time(settings_row, now), "digest")
    if in_quiet_hours(settings_row, now):
        return EmailDecision("defer", quiet_hours_end(settings_row, now), "quiet")
    return EmailDecision(SEND)


def push_allowed_now(user, category: str, now: datetime | None = None) -> bool:
    """Browser push for optional categories waits out quiet hours (the inbox still gets the notice)."""
    cat = BY_KEY.get(category)
    if cat is None or cat.locked:
        return True
    return not in_quiet_hours(delivery_settings(user), now)


_UNSUBSCRIBE_SALT = "notification-unsubscribe"


def unsubscribe_token(user, category: str) -> str:
    return signing.dumps({"u": user.pk, "c": category}, salt=_UNSUBSCRIBE_SALT, compress=True)


def read_unsubscribe_token(token: str):
    """(user, Category) from a token, or None when it is invalid or the category cannot be switched off."""
    from iic_booking.users.models import User

    try:
        data = signing.loads(token, salt=_UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None
    cat = BY_KEY.get(str(data.get("c") or ""))
    if cat is None or cat.locked:
        return None
    user = User.objects.filter(pk=data.get("u"), is_active=True).first()
    if user is None:
        return None
    return user, cat


def unsubscribe(user, category: str) -> None:
    from .models import NotificationPreference

    NotificationPreference.objects.update_or_create(
        user=user, category=category, defaults={"email_enabled": False}
    )


def unsubscribe_headers(user, category: str) -> dict[str, str]:
    """RFC 8058 one-click unsubscribe headers for optional categories (none for service messages)."""
    cat = BY_KEY.get(category)
    base = (getattr(django_settings, "FRONTEND_URL", "") or "").rstrip("/")
    if cat is None or cat.locked or not base or user is None or not getattr(user, "pk", None):
        return {}
    url = f"{base}/api/notifications/unsubscribe/{unsubscribe_token(user, category)}/"
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def allows(user, channel: str, category: str) -> bool:
    """False only when the user switched this channel off for an unlocked category."""
    cat = BY_KEY.get(category)
    if cat is None or cat.locked or user is None or not getattr(user, "pk", None):
        return True
    from .models import NotificationPreference

    pref = NotificationPreference.objects.filter(user=user, category=category).only(
        "email_enabled", "in_app_enabled"
    ).first()
    if pref is None:
        return True
    return pref.email_enabled if channel == EMAIL else pref.in_app_enabled


def is_staff_viewer(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return getattr(user, "user_type", None) in (
        UserType.OPERATOR,
        UserType.MANAGER,
        UserType.DEPT_ADMIN,
        UserType.ADMIN,
    )


def is_report_recipient(user) -> bool:
    """Faculty, Officers in Charge, Heads of Department (and the Main Administrator, who previews them)."""
    from iic_booking.users.models.user_type import UserType

    if user is None or not getattr(user, "pk", None):
        return False
    if getattr(user, "user_type", None) in (UserType.FACULTY, UserType.MANAGER, UserType.HOD, UserType.ADMIN):
        return True
    from iic_booking.communication.monthly_reports import active_hod_user_ids
    from iic_booking.equipment.models import EquipmentManager

    return user.pk in active_hod_user_ids() or EquipmentManager.objects.filter(manager_id=user.pk).exists()


def preferences_payload(user) -> dict:
    stored = _stored(user)
    staff = is_staff_viewer(user)
    rows = []
    for cat in CATEGORIES:
        if cat.staff_only and not staff:
            continue
        if cat.report_recipients_only and not is_report_recipient(user):
            continue
        values = stored.get(cat.key, {})
        rows.append(
            {
                "key": cat.key,
                "label": cat.label,
                "description": cat.description,
                "locked": cat.locked,
                "email_only": cat.email_only,
                EMAIL: True if cat.locked else values.get(EMAIL, True),
                IN_APP: True if cat.locked else (False if cat.email_only else values.get(IN_APP, True)),
                DIGEST: False if (cat.locked or cat.email_only) else values.get(DIGEST, False),
            }
        )
    delivery = delivery_settings(user)
    return {
        "categories": rows,
        "channels": list(CHANNELS),
        "delivery": {
            "quiet_hours_enabled": bool(delivery and delivery.quiet_hours_enabled),
            "quiet_start": (delivery.quiet_start if delivery else time(22, 0)).strftime("%H:%M"),
            "quiet_end": (delivery.quiet_end if delivery else time(7, 0)).strftime("%H:%M"),
            "digest_hour": delivery.digest_hour if delivery else DEFAULT_DIGEST_HOUR,
        },
    }


def _parse_hhmm(value, field: str) -> time:
    try:
        hours, minutes = str(value).strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a time like 22:00.") from None


def _save_delivery(user, delivery) -> None:
    from .models import NotificationDeliverySettings

    if not isinstance(delivery, dict):
        raise ValueError("delivery must be an object.")
    defaults = {}
    if "quiet_hours_enabled" in delivery:
        defaults["quiet_hours_enabled"] = bool(delivery["quiet_hours_enabled"])
    if "quiet_start" in delivery:
        defaults["quiet_start"] = _parse_hhmm(delivery["quiet_start"], "quiet_start")
    if "quiet_end" in delivery:
        defaults["quiet_end"] = _parse_hhmm(delivery["quiet_end"], "quiet_end")
    if "digest_hour" in delivery:
        try:
            hour = int(delivery["digest_hour"])
        except (TypeError, ValueError):
            raise ValueError("digest_hour must be a whole hour from 0 to 23.") from None
        if not 0 <= hour <= 23:
            raise ValueError("digest_hour must be a whole hour from 0 to 23.")
        defaults["digest_hour"] = hour
    if defaults:
        NotificationDeliverySettings.objects.update_or_create(user=user, defaults=defaults)


def save_preferences(user, rows, delivery=None) -> dict:
    """Apply ``[{key, email, in_app, digest}]`` and optional quiet hours / digest hour.

    Locked and unknown categories are ignored.
    """
    from .models import NotificationPreference

    if not isinstance(rows, list):
        raise ValueError("categories must be a list.")
    if delivery is not None:
        _save_delivery(user, delivery)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each category must be an object.")
        cat = BY_KEY.get(str(row.get("key") or ""))
        if cat is None or cat.locked:
            continue
        defaults = {}
        for channel, field in ((EMAIL, "email_enabled"), (IN_APP, "in_app_enabled"), (DIGEST, "email_digest")):
            if cat.email_only and channel != EMAIL:
                continue
            if channel in row:
                defaults[field] = bool(row[channel])
        if defaults:
            NotificationPreference.objects.update_or_create(user=user, category=cat.key, defaults=defaults)
    return preferences_payload(user)
