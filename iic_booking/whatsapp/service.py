"""Who gets which WhatsApp message, the opt-in, and the webhook (delivery status, STOP replies)."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from . import conf
from .models import WhatsAppConsentEvent, WhatsAppInbound, WhatsAppMessage, WhatsAppOptIn, WhatsAppWebhookEvent
from .template_catalogue import BY_EVENT
from .templates import is_approved

logger = logging.getLogger(__name__)

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


def _record_consent(*, user_id, phone: str, action: str, source: str) -> None:
    """The consent itself has already changed: a failure to write the history must not undo it."""
    try:
        with transaction.atomic():
            WhatsAppConsentEvent.objects.create(user_id=user_id, phone_e164=phone, action=action, source=source)
    except DatabaseError as exc:
        logger.warning("whatsapp consent history not stored: %s", type(exc).__name__)


def _skip_queued(phone: str) -> None:
    """STOP takes effect at once, including for messages waiting to be sent or retried."""
    WhatsAppMessage.objects.filter(to_e164=phone, status=WhatsAppMessage.Status.QUEUED).update(
        status=WhatsAppMessage.Status.SKIPPED, error="user opted out before sending", updated_at=timezone.now())


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
    if row.phone_e164:
        _record_consent(user_id=user.pk, phone=row.phone_e164, action="opt_in" if opted_in else "opt_out", source=source)
        if not opted_in:
            _skip_queued(row.phone_e164)
    return opt_in_status(user)


def notify(user, event: str, params: list[str], *, event_key: str) -> WhatsAppMessage | None:
    """Queue one template message for a portal event, once per ``event_key``. Does nothing while WhatsApp is off,
    for users who have not opted in, or (during a pilot) for numbers outside WHATSAPP_PILOT_NUMBERS. A template that is
    not approved at Meta sends nothing: the attempt is recorded as skipped."""
    entry = BY_EVENT.get(event)
    if entry is None or user is None or not conf.enabled():
        return None
    if len(params) != len(entry.params):
        logger.error("whatsapp %s got %d values, its template needs %d", event, len(params), len(entry.params))
        return None
    row = WhatsAppOptIn.objects.filter(user=user, opted_in=True).first()
    if row is None or not row.phone_e164:
        return None
    if not conf.may_send_to(row.phone_e164):
        return None
    template = conf.template_name(event)
    approved = is_approved(template, entry.language)
    try:
        with transaction.atomic():
            msg = WhatsAppMessage.objects.create(
                user=user, to_e164=row.phone_e164, event=event, template=template, params=[str(p) for p in params],
                event_key=event_key[:120], status=WhatsAppMessage.Status.QUEUED if approved else WhatsAppMessage.Status.SKIPPED,
                error="" if approved else "template is not approved at Meta")
    except IntegrityError:
        return None  # already sent for this event
    if approved:
        transaction.on_commit(lambda: _enqueue(msg.pk))
    return msg


def _enqueue(message_id: int) -> None:
    """Runs right after the booking commits: a queue that is down must never surface in the booking request."""
    from .tasks import deliver_whatsapp

    try:
        deliver_whatsapp.delay(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("whatsapp message %s could not be queued: %s", message_id, type(exc).__name__)


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
    if not WhatsAppOptIn.objects.filter(user_id=msg.user_id, opted_in=True, phone_e164=msg.to_e164).exists():
        msg.status, msg.error = WhatsAppMessage.Status.SKIPPED, "user opted out before sending"
        msg.save(update_fields=["status", "error", "updated_at"])
        return msg.status
    if not conf.may_send_to(msg.to_e164):  # checked again here: the pilot list may have changed since it was queued
        msg.status, msg.error = WhatsAppMessage.Status.SKIPPED, "number not allowed to receive messages"
        msg.save(update_fields=["status", "error", "updated_at"])
        return msg.status
    entry = BY_EVENT.get(msg.event)
    language = entry.language if entry else conf.template_language()
    if not is_approved(msg.template, language):  # checked again: Meta may have paused or disabled it since it was queued
        msg.status, msg.error = WhatsAppMessage.Status.SKIPPED, "template is not approved at Meta"
        msg.save(update_fields=["status", "error", "updated_at"])
        return msg.status
    try:
        wamid = send_template(msg.to_e164, msg.template, list(msg.params or []), language=language, session=session)
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


def mark_retries_exhausted(message_id: int, reason: str) -> None:
    """Temporary failures have used up their retries: record it, so the message does not sit in the queue for ever."""
    WhatsAppMessage.objects.filter(pk=message_id, status=WhatsAppMessage.Status.QUEUED).update(
        status=WhatsAppMessage.Status.FAILED, error=f"gave up after retries: {reason}"[:2000], updated_at=timezone.now())


_STATUS_ORDER = {"sent": 1, "delivered": 2, "read": 3}


def handle_webhook(payload: dict) -> dict:
    """Delivery and read statuses update the message; a STOP / START reply opts the sender out / in; every message
    from someone with an opt-in record is kept once. Returns counts only, so logging it never exposes a person."""
    counts = {"statuses": 0, "replies": 0, "inbound": 0, "duplicates": 0}
    if not isinstance(payload, dict):
        return counts
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for st in _dicts(value.get("statuses")):
                _handle_once("status", str(st.get("id") or ""), str(st.get("status") or ""), counts,
                             lambda st=st: counts.__setitem__("statuses", counts["statuses"] + _apply_status(st)))
            for m in _dicts(value.get("messages")):
                _handle_once("message", str(m.get("id") or ""), "", counts, lambda m=m: _handle_message(m, counts))
    return counts


def _handle_message(m: dict, counts: dict) -> None:
    action = _apply_reply(m)
    counts["replies"] += int(bool(action))
    counts["inbound"] += _record_inbound(m, action)


def _handle_once(kind: str, wamid: str, detail: str, counts: dict, apply) -> None:
    """Meta repeats events. Each is applied once, in one transaction with its record, so a failure lets Meta's retry
    apply it again. Without this, an old STOP arriving late could undo a later START."""
    if not wamid:
        apply()
        return
    key = hashlib.sha256(f"{kind}:{wamid}:{detail}".encode()).hexdigest()
    with transaction.atomic():
        try:
            with transaction.atomic():
                WhatsAppWebhookEvent.objects.create(event_key=key, kind=kind, detail=detail[:20])
        except IntegrityError:
            counts["duplicates"] += 1
            return
        except DatabaseError as exc:  # the table is not there yet: an opt-out must still work
            logger.warning("whatsapp webhook record not stored: %s", type(exc).__name__)
        apply()


def _dicts(items) -> list[dict]:
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


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


def _message_text(m: dict) -> str:
    """The words of a typed message, a template button or an interactive button / list choice."""
    interactive = m.get("interactive") if isinstance(m.get("interactive"), dict) else {}
    for part, key in (
        (m.get("text"), "body"),
        (m.get("button"), "text"),
        (interactive.get("button_reply"), "title"),
        (interactive.get("list_reply"), "title"),
    ):
        if isinstance(part, dict) and part.get(key):
            return str(part[key]).strip()
    return ""


def _apply_reply(m: dict) -> str:
    """"opt_out" or "opt_in" when this message changed someone's consent, else ""."""
    text = _message_text(m).lower()
    try:
        phone = normalize_phone(str(m.get("from") or ""))
    except OptInError:
        return ""
    if text in STOP_WORDS:
        # Every account on this number, and anything still waiting to go to it.
        rows = list(WhatsAppOptIn.objects.filter(phone_e164=phone, opted_in=True))
        WhatsAppOptIn.objects.filter(pk__in=[r.pk for r in rows]).update(
            opted_in=False, opted_out_at=timezone.now(), source="reply_stop")
        _skip_queued(phone)
        for r in rows:
            _record_consent(user_id=r.user_id, phone=phone, action="opt_out", source="reply_stop")
        return "opt_out" if rows else ""
    if text in START_WORDS:
        # Only someone who once gave consent: START from an unknown number creates nothing.
        rows = list(WhatsAppOptIn.objects.filter(phone_e164=phone, opted_in=False, opted_in_at__isnull=False))
        WhatsAppOptIn.objects.filter(pk__in=[r.pk for r in rows]).update(
            opted_in=True, opted_in_at=timezone.now(), opted_out_at=None, source="reply_start")
        for r in rows:
            _record_consent(user_id=r.user_id, phone=phone, action="opt_in", source="reply_start")
        return "opt_in" if rows else ""
    return ""


def _meta_timestamp(raw) -> datetime | None:
    """Meta's message time, whole Unix seconds as a string, in UTC. None when missing or malformed: never raises, so a
    bad value cannot cost us the message."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9]{1,12}", text) or int(text) <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(text), tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _record_inbound(m: dict, action: str) -> int:
    """Keep a message from someone with an opt-in record, once (Meta re-sends). Strangers' messages are not stored.
    A failure here never undoes the opt-out above, and the webhook still answers Meta."""
    wamid = str(m.get("id") or "")[:128]
    try:
        phone = normalize_phone(str(m.get("from") or ""))
    except OptInError:
        return 0
    if not wamid:
        return 0
    try:
        with transaction.atomic():
            row = WhatsAppOptIn.objects.filter(phone_e164=phone).first()
            if row is None:
                return 0
            _, created = WhatsAppInbound.objects.get_or_create(wamid=wamid, defaults={
                "user_id": row.user_id, "from_e164": phone, "kind": str(m.get("type") or "")[:20],
                "text": _message_text(m)[:2000], "action": action, "meta_timestamp": _meta_timestamp(m.get("timestamp")),
            })
    except DatabaseError as exc:
        logger.warning("whatsapp incoming message not stored: %s", type(exc).__name__)
        return 0
    return int(created)


def purge_expired(now=None) -> dict:
    """Retention: incoming messages and webhook records older than WHATSAPP_RETENTION_DAYS. Consent history stays."""
    cutoff = (now or timezone.now()) - timedelta(days=conf.retention_days())
    inbound, _ = WhatsAppInbound.objects.filter(received_at__lt=cutoff).delete()
    events, _ = WhatsAppWebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    return {"inbound": inbound, "webhook_events": events}


def status_summary() -> dict:
    from django.db.models import Count

    by_status = dict(WhatsAppMessage.objects.values_list("status").annotate(n=Count("pk")))
    try:
        with transaction.atomic():
            inbound = WhatsAppInbound.objects.count()
    except DatabaseError:
        inbound = None  # migration not applied yet
    return {
        "inbound_messages": inbound,
        "allow_all": conf.allow_all(),
        "retention_days": conf.retention_days(),
        "enabled": conf.enabled(),
        "switch_on": bool(getattr(settings, "WHATSAPP_ENABLED", False)),
        "missing_settings": conf.missing_settings(),
        "pilot_numbers": len(conf.pilot_numbers()),
        "opted_in_users": WhatsAppOptIn.objects.filter(opted_in=True).count(),
        "messages": by_status,
    }
