"""Per-user notification settings: which categories reach a user by email and in the portal inbox.

Every email and in-app notification falls into one category (from its template code, or for in-app notifications
without a template from their metadata and title). Service messages a user must receive (account and security,
booking changes, results, money) are locked on; everything else can be switched off per channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
)
BY_KEY = {c.key: c for c in CATEGORIES}

# First match wins; template codes are matched in full against these patterns.
_TEMPLATE_RULES: tuple[tuple[str, str], ...] = (
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
        p.category: {EMAIL: p.email_enabled, IN_APP: p.in_app_enabled}
        for p in NotificationPreference.objects.filter(user=user)
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


def preferences_payload(user) -> dict:
    stored = _stored(user)
    staff = is_staff_viewer(user)
    rows = []
    for cat in CATEGORIES:
        if cat.staff_only and not staff:
            continue
        values = stored.get(cat.key, {})
        rows.append(
            {
                "key": cat.key,
                "label": cat.label,
                "description": cat.description,
                "locked": cat.locked,
                EMAIL: True if cat.locked else values.get(EMAIL, True),
                IN_APP: True if cat.locked else values.get(IN_APP, True),
            }
        )
    return {"categories": rows, "channels": list(CHANNELS)}


def save_preferences(user, rows) -> dict:
    """Apply ``[{key, email, in_app}]``; locked and unknown categories are ignored."""
    from .models import NotificationPreference

    if not isinstance(rows, list):
        raise ValueError("categories must be a list.")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each category must be an object.")
        cat = BY_KEY.get(str(row.get("key") or ""))
        if cat is None or cat.locked:
            continue
        defaults = {}
        for channel, field in ((EMAIL, "email_enabled"), (IN_APP, "in_app_enabled")):
            if channel in row:
                defaults[field] = bool(row[channel])
        if defaults:
            NotificationPreference.objects.update_or_create(user=user, category=cat.key, defaults=defaults)
    return preferences_payload(user)
