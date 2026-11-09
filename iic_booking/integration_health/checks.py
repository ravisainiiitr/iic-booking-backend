"""Integration health checks over the portal's own records (database reads only; nothing is sent anywhere).

Each check answers one question an administrator would otherwise only find out by testing: did paid bookings reach
I-STEM, did completed ones get their FBR executed, is the I-STEM queue moving, are WhatsApp messages being delivered,
are payments being applied. Items name bookings by their booking ID only: no names, emails or phone numbers.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from django.utils import timezone

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
_RANK = {OFF: 0, OK: 1, WARN: 2, FAIL: 3}
MAX_ITEMS = 10

# How long a step may take before it counts as stuck.
REGISTER_GRACE = timedelta(minutes=30)
EXECUTE_GRACE = timedelta(hours=2)
QUEUE_GRACE = timedelta(minutes=15)
WHATSAPP_QUEUE_GRACE = timedelta(minutes=15)
LOOKBACK = timedelta(days=7)
WHATSAPP_LOOKBACK = timedelta(hours=24)

_META_CODE = re.compile(r"\((\d{3,6})\)")


@dataclass
class Check:
    key: str
    component: str  # istem | whatsapp | payments | email
    title: str
    status: str = OK
    summary: str = ""
    items: list[str] = field(default_factory=list)
    action: str = ""
    count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def worst(statuses) -> str:
    statuses = list(statuses)
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else OK


def _ref(booking) -> str:
    return getattr(booking, "virtual_booking_id", None) or f"#{booking.pk}"


# --- I-STEM ----------------------------------------------------------------------------------------------------------

def istem_checks(now=None) -> list[Check]:
    from iic_booking.istem import conf

    if not conf.automatic_fbr_enabled():
        return [Check("istem", "istem", "I-STEM", status=OFF, summary="Automatic I-STEM records are switched off.")]
    now = now or timezone.now()
    return [_istem_registered(now), _istem_held(now), _istem_executed(now), _istem_queue(now), _istem_user_links()]


def _istem_registered(now) -> Check:
    """Paid bookings that should be on I-STEM but have not been registered within REGISTER_GRACE."""
    from iic_booking.equipment.models import Booking
    from iic_booking.istem import eligibility
    from iic_booking.istem.choices import SyncState
    from iic_booking.istem.models import IstemBookingLink

    check = Check("istem_registered", "istem", "Paid bookings registered on I-STEM")
    start = now - LOOKBACK
    sync_start = eligibility.sync_start_at()
    if sync_start and sync_start > start:
        start = sync_start
    stuck = []
    # Linked but still waiting to be created or confirmed.
    waiting = (IstemBookingLink.objects.filter(
        sync_state__in=[SyncState.CREATE_READY, SyncState.USER_PENDING, SyncState.CONFIRM_READY, SyncState.CREATED],
        booking__created_at__gte=start, updated_at__lt=now - REGISTER_GRACE, needs_review=False)
        .select_related("booking").order_by("pk")[:200])
    stuck += [f"{_ref(link.booking)}: waiting at {link.sync_state}" for link in waiting]
    # Never linked although eligible.
    candidates = (Booking.objects.filter(created_at__gte=start, created_at__lt=now - REGISTER_GRACE,
                                         status__in=["BOOKED", "COMPLETED"], istem_link__isnull=True)
                  .select_related("charge_profile", "user", "equipment").order_by("pk")[:300])
    for booking in candidates:
        try:
            ok, _reason = eligibility.evaluate(booking)
        except Exception:  # noqa: BLE001 - one odd booking must not hide the others
            continue
        if ok:
            stuck.append(f"{_ref(booking)}: paid but not sent to I-STEM")
    check.count = len(stuck)
    if stuck:
        check.status = FAIL
        check.summary = f"{len(stuck)} paid booking(s) not on I-STEM after {int(REGISTER_GRACE.total_seconds() // 60)} minutes."
        check.items = stuck[:MAX_ITEMS]
        check.action = "Use \"Check and fix now\" (re-runs the I-STEM queue); if they stay, open the booking's I-STEM record."
    else:
        check.summary = "Every paid booking reached I-STEM in time."
    return check


def _istem_held(now) -> Check:
    """Records I-STEM refused, or that are held for the team (attention states / needs_review)."""
    from iic_booking.istem.booking_status import _ATTENTION
    from iic_booking.istem.models import IstemBookingLink

    check = Check("istem_held", "istem", "I-STEM records held for the team")
    held = (IstemBookingLink.objects.filter(booking__created_at__gte=now - timedelta(days=30))
            .filter(_attention_q(_ATTENTION)).select_related("booking").order_by("-updated_at")[:200])
    items = [f"{_ref(link.booking)}: {link.sync_state} - {(link.review_reason or '').splitlines()[0][:140]}" for link in held]
    check.count = len(items)
    if items:
        check.status = FAIL
        check.summary = f"{len(items)} I-STEM record(s) need the team."
        check.items = items[:MAX_ITEMS]
        check.action = "Open each booking's I-STEM record. \"Check and fix now\" retries the ones that are safe to retry."
    else:
        check.summary = "No I-STEM record is held."
    return check


def _attention_q(states):
    from django.db.models import Q

    return Q(sync_state__in=list(states)) | Q(needs_review=True)


def _istem_executed(now) -> Check:
    from iic_booking.istem.choices import SyncState
    from iic_booking.istem.models import IstemBookingLink

    check = Check("istem_executed", "istem", "Completed bookings with FBR executed")
    late = (IstemBookingLink.objects.filter(
        sync_state__in=[SyncState.CONFIRMED, SyncState.EXECUTE_READY], booking__status="COMPLETED",
        booking__completed_at__lt=now - EXECUTE_GRACE, booking__completed_at__gte=now - timedelta(days=30),
        needs_review=False).select_related("booking").order_by("pk")[:200])
    items = [f"{_ref(link.booking)}: completed, FBR not executed" for link in late]
    check.count = len(items)
    if items:
        check.status = FAIL
        check.summary = f"{len(items)} completed booking(s) without FBR Execute after {int(EXECUTE_GRACE.total_seconds() // 3600)} hours."
        check.items = items[:MAX_ITEMS]
        check.action = "Use \"Check and fix now\" (queues and runs FBR Execute)."
    else:
        check.summary = "Every completed booking has its FBR executed (or is within the first hours)."
    return check


def _istem_queue(now) -> Check:
    from iic_booking.istem.choices import JobState
    from iic_booking.istem.models import IstemOutboxJob

    check = Check("istem_queue", "istem", "I-STEM queue is moving")
    overdue = IstemOutboxJob.objects.filter(state__in=[JobState.PENDING, JobState.RETRY_WAIT],
                                            next_attempt_at__lt=now - QUEUE_GRACE)
    n = overdue.count()
    check.count = n
    if n:
        check.status = FAIL
        oldest = overdue.order_by("next_attempt_at").values_list("next_attempt_at", flat=True).first()
        minutes = int((now - oldest).total_seconds() // 60) if oldest else 0
        check.summary = f"{n} I-STEM job(s) overdue; the oldest has waited {minutes} minutes. The scheduled sync may not be running."
        check.action = "\"Check and fix now\" runs the queue. If it keeps happening, check the scheduler (Celery beat) on the server."
    else:
        check.summary = "No I-STEM job is overdue."
    return check


def _istem_user_links() -> Check:
    from iic_booking.istem.models import IstemUserLink

    check = Check("istem_users", "istem", "I-STEM accounts linked")
    n = IstemUserLink.objects.filter(state="NEEDS_REVIEW").count()
    check.count = n
    if n:
        check.status = WARN
        check.summary = f"{n} user account(s) need the team before their I-STEM link is complete."
        check.action = "Open I-STEM user links and settle the ones waiting for review."
    else:
        check.summary = "No I-STEM account is waiting for review."
    return check


# --- WhatsApp --------------------------------------------------------------------------------------------------------

def whatsapp_checks(now=None) -> list[Check]:
    from iic_booking.whatsapp import conf

    if not conf.enabled():
        return [Check("whatsapp", "whatsapp", "WhatsApp", status=OFF, summary="WhatsApp messages are switched off.")]
    now = now or timezone.now()
    return [_whatsapp_delivery(now), _whatsapp_queue(now)]


def meta_code(error: str) -> str:
    match = _META_CODE.search(error or "")
    return match.group(1) if match else ""


META_HINTS = {
    "131037": "The WhatsApp number's display name is not approved. Approve it in Meta WhatsApp Manager (Phone numbers).",
    "131026": "The recipient cannot receive this message (not on WhatsApp or an old app version).",
    "131047": "More than 24 hours since the user last wrote: only approved templates can be sent.",
    "131049": "Meta held the message back to protect the user experience (too many marketing messages).",
    "130429": "Meta's sending rate limit was reached.",
    "131056": "Too many messages to the same number in a short time.",
    "132001": "The template does not exist or is not approved in this language.",
    "190": "The WhatsApp access token has expired or is invalid.",
}


def _whatsapp_delivery(now) -> Check:
    from iic_booking.whatsapp.models import WhatsAppMessage

    check = Check("whatsapp_delivery", "whatsapp", "WhatsApp messages delivered")
    recent = WhatsAppMessage.objects.filter(created_at__gte=now - WHATSAPP_LOOKBACK)
    failed = list(recent.filter(status=WhatsAppMessage.Status.FAILED).order_by("-created_at")
                  .values("event", "error", "created_at")[:500])
    ok_statuses = [WhatsAppMessage.Status.SENT, WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ]
    sent = recent.filter(status__in=ok_statuses).count()
    check.count = len(failed)
    if not failed:
        check.summary = f"{sent} message(s) sent in the last 24 hours, none refused."
        return check
    codes = Counter(meta_code(f["error"]) or "other" for f in failed)
    latest_fail = failed[0]["created_at"]
    any_after = recent.filter(status__in=ok_statuses, created_at__gt=latest_fail).exists()
    check.status = WARN if any_after else FAIL
    check.summary = (f"{len(failed)} of {len(failed) + sent} message(s) refused in the last 24 hours"
                     + ("" if any_after else "; nothing has gone out since the last refusal") + ".")
    check.items = [f"Meta {code}: {n} message(s) - {META_HINTS.get(code, 'see the error on the message')}"
                   for code, n in codes.most_common(MAX_ITEMS)]
    top = codes.most_common(1)[0][0]
    check.action = META_HINTS.get(top, "Check the refused messages.") + " Refused messages are resent automatically once Meta accepts messages again (last 48 hours)."
    return check


def _whatsapp_queue(now) -> Check:
    from iic_booking.whatsapp.models import WhatsAppMessage

    check = Check("whatsapp_queue", "whatsapp", "WhatsApp queue is moving")
    n = WhatsAppMessage.objects.filter(status=WhatsAppMessage.Status.QUEUED,
                                       created_at__lt=now - WHATSAPP_QUEUE_GRACE,
                                       created_at__gte=now - timedelta(days=2)).count()
    check.count = n
    if n:
        check.status = FAIL
        check.summary = f"{n} WhatsApp message(s) queued for more than 15 minutes; the task worker may be down."
        check.action = "\"Check and fix now\" sends them again; if it keeps happening, check the Celery worker."
    else:
        check.summary = "No WhatsApp message is stuck in the queue."
    return check


# --- Payments --------------------------------------------------------------------------------------------------------

def payment_checks(now=None) -> list[Check]:
    from iic_booking.payments import gateway_mode
    from iic_booking.payments.models import PaymentIssue, PaymentIssueStatus, RazorpayWebhookEvent, WebhookEventStatus

    if not gateway_mode.enabled():
        return [Check("payments", "payments", "Online payments", status=OFF, summary="Online payments are switched off.")]
    now = now or timezone.now()
    issues = Check("payments_issues", "payments", "Payments applied to bookings")
    open_issues = Counter(PaymentIssue.objects.filter(status=PaymentIssueStatus.OPEN).values_list("kind", flat=True))
    issues.count = sum(open_issues.values())
    serious = {k: n for k, n in open_issues.items() if k not in ("UNSETTLED",)}
    if serious:
        issues.status = FAIL if any(k in ("NOT_APPLIED", "DUPLICATE_CAPTURE", "MISMATCH") for k in serious) else WARN
        issues.summary = f"{sum(serious.values())} open payment issue(s)."
        issues.items = [f"{kind}: {n}" for kind, n in sorted(serious.items())]
        issues.action = ("These need Finance: refund or apply the money by hand, then close the issue with a note "
                         "(POST /api/payments/issues/<id>/resolve/). \"Authorised but not captured\" issues close "
                         "themselves once Razorpay's capture is recorded.")
    else:
        issues.summary = "No open payment issue."
    hooks = Check("payments_webhooks", "payments", "Razorpay notifications processed")
    n = RazorpayWebhookEvent.objects.filter(status=WebhookEventStatus.FAILED, received_at__gte=now - timedelta(hours=24)).count() \
        if _has_field(RazorpayWebhookEvent, "received_at") else \
        RazorpayWebhookEvent.objects.filter(status=WebhookEventStatus.FAILED).count()
    hooks.count = n
    if n:
        hooks.status = WARN
        hooks.summary = f"{n} Razorpay notification(s) failed to apply (Razorpay retries them)."
        hooks.action = "If they do not clear within an hour, check the payments log."
    else:
        hooks.summary = "Razorpay notifications are being applied."
    return [issues, hooks]


def _has_field(model, name: str) -> bool:
    return any(f.name == name for f in model._meta.get_fields())


def database_checks(now=None) -> list[Check]:
    now = now or timezone.now()
    out: list[Check] = []
    for fn in (istem_checks, whatsapp_checks, payment_checks):
        try:
            out.extend(fn(now))
        except Exception as exc:  # noqa: BLE001 - a broken check is itself a finding, never a page error
            out.append(Check(fn.__name__, fn.__name__.split("_")[0], fn.__name__.replace("_", " ").title(),
                             status=WARN, summary=f"The check could not run ({type(exc).__name__})."))
    return out
