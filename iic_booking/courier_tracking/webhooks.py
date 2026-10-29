"""TrackCourier.io webhooks: verify, store durably, acknowledge, then process.

* ``X-Trackcourier-Signature: sha256=<hex>`` is an HMAC-SHA256 of the exact raw body with the signing secret; compared
  in constant time. During a rotation the previous secret is accepted too (``TRACKCOURIER_WEBHOOK_PREVIOUS_SECRET``).
  The other headers are unsigned and only informational.
* A verified body is saved (``TrackingWebhookEvent``) before 2xx is returned. Deduplication is by the body's SHA-256
  (an exact retry), never by ``X-Webhook-Delivery`` and never by event + courier + number + enqueued_at alone (the
  provider says those can collide for different events). Processing is idempotent anyway: a checkpoint array
  replaces the stored one; ``checkpoints: null`` keeps it.
* Test deliveries (``data.courier`` starting ``synthetic-test-``) are stored and ignored.
* With no signing secret configured the endpoint refuses everything (the free plan has no live webhooks).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .constants import SYNTHETIC_TEST_COURIER_PREFIX, WEBHOOK_EVENTS
from .models import TrackingWebhookEvent, WebhookEventStatus
from .parsing import WebhookPayloadError, parse_webhook_payload

logger = logging.getLogger(__name__)

MAX_PROCESS_ATTEMPTS = 5


def _secrets() -> list[str]:
    return [s for s in (getattr(settings, "TRACKCOURIER_WEBHOOK_SECRET", ""),
                        getattr(settings, "TRACKCOURIER_WEBHOOK_PREVIOUS_SECRET", "")) if s]


def webhooks_configured() -> bool:
    return bool(_secrets())


def verify_signature(raw_body: bytes, header: str) -> bool:
    header = (header or "").strip()
    if not header or not header.isascii():
        return False
    for secret in _secrets():
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, header):
            return True
    return False


class WebhookRejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def accept(raw_body: bytes, signature: str, *, mode_header: str = "") -> TrackingWebhookEvent:
    """Verify and durably store one delivery. Raises WebhookRejected (status, message) on refusal."""
    if not webhooks_configured():
        raise WebhookRejected(503, "Webhooks are not configured.")
    if not verify_signature(raw_body, signature):
        raise WebhookRejected(401, "Invalid signature.")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected(400, "Malformed JSON.") from None
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "Malformed payload.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    digest = hashlib.sha256(raw_body).hexdigest()
    try:
        with transaction.atomic():
            event, created = TrackingWebhookEvent.objects.get_or_create(
                body_sha256=digest,
                defaults={
                    "event": str(payload.get("event") or "")[:40],
                    "mode": (mode_header or "")[:8],
                    "courier_slug": str(data.get("courier") or "")[:64],
                    "tracking_number": str(data.get("tracking_number") or "")[:64],
                    "enqueued_at": str(payload.get("enqueued_at") or data.get("enqueued_at") or "")[:40],
                    "payload": payload,
                },
            )
    except IntegrityError:
        event, created = TrackingWebhookEvent.objects.get(body_sha256=digest), False
    if not created:
        TrackingWebhookEvent.objects.filter(pk=event.pk).update(deliveries=F("deliveries") + 1)
    return event


def process(event_id: int) -> str:
    """Apply a stored event (idempotent). Returns the resulting status."""
    from .services import apply_webhook

    with transaction.atomic():
        event = TrackingWebhookEvent.objects.select_for_update(skip_locked=True).filter(pk=event_id).first()
        if event is None or event.status in (WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED,
                                              WebhookEventStatus.UNMATCHED):
            return event.status if event else "missing"
        event.attempts += 1
        status, outcome, matched = WebhookEventStatus.PROCESSED, "", 0
        try:
            if event.courier_slug.startswith(SYNTHETIC_TEST_COURIER_PREFIX):
                status, outcome = WebhookEventStatus.IGNORED, "synthetic test delivery"
            elif event.event not in WEBHOOK_EVENTS:
                status, outcome = WebhookEventStatus.IGNORED, "event type not handled"
            else:
                parsed = parse_webhook_payload(event.payload)
                matched, applied = apply_webhook(parsed.tracking, courier_slug=parsed.courier_slug,
                                                 tracking_number=parsed.tracking_number,
                                                 enqueued_at=_iso_utc(parsed.enqueued_at))
                if not matched:
                    status, outcome = WebhookEventStatus.UNMATCHED, "no active courier shipment with this number"
                elif not applied:
                    outcome = "older than the state already applied; recorded only"
        except WebhookPayloadError as exc:
            status, outcome = WebhookEventStatus.IGNORED, str(exc)
        except Exception as exc:  # noqa: BLE001 - kept for retry
            logger.exception("Courier webhook %s processing failed", event.pk)
            status = WebhookEventStatus.FAILED if event.attempts < MAX_PROCESS_ATTEMPTS else WebhookEventStatus.IGNORED
            outcome = f"error: {type(exc).__name__}"
        event.status = status
        event.outcome = outcome[:255]
        event.matched = matched
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "outcome", "matched", "attempts", "processed_at"])
    return status


def _iso_utc(value: str) -> str:
    """Provider enqueued_at as sortable ISO 8601 UTC ("" when unparseable: then ordering is not enforced)."""
    from datetime import timezone as dt_timezone

    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime((value or "").strip())
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep(limit: int = 100) -> int:
    ids = list(
        TrackingWebhookEvent.objects.filter(status__in=[WebhookEventStatus.RECEIVED, WebhookEventStatus.FAILED])
        .order_by("received_at")
        .values_list("pk", flat=True)[:limit]
    )
    for pk in ids:
        process(pk)
    return len(ids)
