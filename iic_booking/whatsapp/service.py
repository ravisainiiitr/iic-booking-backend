"""Who gets which WhatsApp message, the opt-in, and the webhook (delivery status, STOP replies)."""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf
from .models import WhatsAppMessage, WhatsAppOptIn

logger = logging.getLogger(__name__)

# Portal events with a WhatsApp template. Body parameters, in order, as the template must declare them
# ({{1}}, {{2}}, ...): see docs/whatsapp.md for the exact wording submitted to Meta.
EVENTS = {
    "booking_confirmed": ("booking ID", "equipment", "slot"),
    "booking_cancelled": ("booking ID", "equipment", "slot"),
    "booking_completed": ("booking ID", "equipment", "link"),
}
STOP_WORDS = {"stop", "unsubscribe", "stop all", "cancel"}
START_WORDS = {"start", "subscribe"}


class OptInError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """'98765 43210' or '+91-98765-43210' -> '+919876543210'. Ten digits are taken as an Indian mobile."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    if not 11 <= len(digits) <= 15:
        raise OptInError("Enter a WhatsApp number with country code, e.g. +91 98765 43210.")
    return "+" + digits


def opt_in_status(user) -> dict:
    row = WhatsAppOptIn.objects.filter(user=user).first()
    default_phone = ""
    try:
        default_phone = normalize_phone(getattr(user, "phone_number", "") or "")
    except OptInError:
        pass
    return {
        "available": conf.enabled(),
        "opted_in": bool(row and row.opted_in),
        "phone": row.phone_e164 if row else default_phone,
        "opted_in_at": row.opted_in_at if row else None,
    }


def set_opt_in(user, *, opted_in: bool, phone: str = "", source: str = "profile") -> dict:
    now = timezone.now()
    row, _ = WhatsAppOptIn.objects.get_or_create(user=user, defaults={"phone_e164": ""})
    if opted_in:
        row.phone_e164 = normalize_phone(phone or row.phone_e164 or getattr(user, "phone_number", "") or "")
        row.opted_in, row.opted_in_at, row.opted_out_at = True, now, None
    else:
        row.opted_in, row.opted_out_at = False, now
    row.source = source
    row.save()
    return opt_in_status(user)


def notify(user, event: str, params: list[str], *, event_key: str) -> WhatsAppMessage | None:
    """Queue one template message for a portal event, once per ``event_key``. Does nothing while WhatsApp is off,
    for users who have not opted in, or (during a pilot) for numbers outside WHATSAPP_PILOT_NUMBERS."""
    if event not in EVENTS or user is None or not conf.enabled():
        return None
    row = WhatsAppOptIn.objects.filter(user=user, opted_in=True).first()
    if row is None or not row.phone_e164:
        return None
    pilot = conf.pilot_numbers()
    if pilot and row.phone_e164 not in pilot:
        return None
    try:
        with transaction.atomic():
            msg = WhatsAppMessage.objects.create(user=user, to_e164=row.phone_e164, event=event,
                                                 template=conf.template_name(event), params=[str(p) for p in params],
                                                 event_key=event_key[:120])
    except IntegrityError:
        return None  # already sent for this event
    from .tasks import deliver_whatsapp

    transaction.on_commit(lambda: deliver_whatsapp.delay(msg.pk))
    return msg


def deliver(message_id: int, *, session=None) -> str:
    """Send a queued message. Returns the new status; a retryable failure is re-raised for the task to retry."""
    from .client import WhatsAppError, send_template

    msg = WhatsAppMessage.objects.filter(pk=message_id).first()
    if msg is None or msg.status != WhatsAppMessage.Status.QUEUED:
        return msg.status if msg else "missing"
    if not conf.enabled():
        msg.status, msg.error = WhatsAppMessage.Status.SKIPPED, "WhatsApp switched off before sending"
        msg.save(update_fields=["status", "error", "updated_at"])
        return msg.status
    if not WhatsAppOptIn.objects.filter(user_id=msg.user_id, opted_in=True).exists():
        msg.status, msg.error = WhatsAppMessage.Status.SKIPPED, "user opted out before sending"
        msg.save(update_fields=["status", "error", "updated_at"])
        return msg.status
    try:
        wamid = send_template(msg.to_e164, msg.template, list(msg.params or []), session=session)
    except WhatsAppError as exc:
        if exc.retryable:
            raise
        msg.status, msg.error = WhatsAppMessage.Status.FAILED, str(exc)[:2000]
        msg.save(update_fields=["status", "error", "updated_at"])
        logger.warning("whatsapp message %s failed: %s", msg.pk, exc)
        return msg.status
    msg.status, msg.wamid, msg.error = WhatsAppMessage.Status.SENT, wamid, ""
    msg.save(update_fields=["status", "wamid", "error", "updated_at"])
    return msg.status


_STATUS_ORDER = {"sent": 1, "delivered": 2, "read": 3}


def handle_webhook(payload: dict) -> dict:
    """Delivery statuses update the message; a STOP / START reply opts the sender out / in."""
    statuses = replies = 0
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for st in value.get("statuses") or []:
                statuses += _apply_status(st)
            for m in value.get("messages") or []:
                replies += _apply_reply(m)
    return {"statuses": statuses, "replies": replies}


def _apply_status(st: dict) -> int:
    wamid, new = str(st.get("id") or ""), str(st.get("status") or "")
    msg = WhatsAppMessage.objects.filter(wamid=wamid).first() if wamid else None
    if msg is None:
        return 0
    if new == "failed":
        errors = st.get("errors") or [{}]
        msg.status, msg.error = WhatsAppMessage.Status.FAILED, str(errors[0].get("title") or errors[0])[:2000]
    elif _STATUS_ORDER.get(new, 0) > _STATUS_ORDER.get(msg.status, 0):
        msg.status = new  # never step back (statuses can arrive out of order)
    else:
        return 0
    msg.save(update_fields=["status", "error", "updated_at"])
    return 1


def _apply_reply(m: dict) -> int:
    text = str(((m.get("text") or {}).get("body")) or ((m.get("button") or {}).get("text")) or "").strip().lower()
    try:
        phone = normalize_phone(str(m.get("from") or ""))
    except OptInError:
        return 0
    if text in STOP_WORDS:
        n = WhatsAppOptIn.objects.filter(phone_e164=phone, opted_in=True).update(
            opted_in=False, opted_out_at=timezone.now(), source="reply_stop")
        return int(n > 0)
    if text in START_WORDS:
        n = WhatsAppOptIn.objects.filter(phone_e164=phone, opted_in=False, opted_in_at__isnull=False).update(
            opted_in=True, opted_out_at=None, source="reply_start")
        return int(n > 0)
    return 0


def status_summary() -> dict:
    from django.db.models import Count

    by_status = dict(WhatsAppMessage.objects.values_list("status").annotate(n=Count("pk")))
    return {
        "enabled": conf.enabled(),
        "switch_on": bool(getattr(settings, "WHATSAPP_ENABLED", False)),
        "missing_settings": conf.missing_settings(),
        "pilot_numbers": len(conf.pilot_numbers()),
        "opted_in_users": WhatsAppOptIn.objects.filter(opted_in=True).count(),
        "messages": by_status,
    }
