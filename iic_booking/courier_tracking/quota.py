"""Central quota control for ``GET /v1/track`` (the only counted endpoint).

Every lookup reserves a slot under a row lock on the month's ``TrackingQuotaMonth``, so concurrent requests cannot
overspend. Effective usage is the larger of the provider's last reported ``used`` (authoritative when present) and our
own conservative count, plus requests in flight. Lookups stop when:

* usage would reach the quota (``provider_quota`` when reported, else the configured fallback);
* routine work (scheduled checks, user refreshes) would eat into the safety reserve kept for first lookups of new
  shipments and staff refreshes;
* the provider said QUOTA_EXCEEDED this month, or the account/key needs attention (until an admin clears it).

Months are UTC calendar months; a new month starts a new row (the provider's ``usage`` in its first answer of the
month then becomes authoritative). Uncertain quota fails safe: no request, last known status shown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from .client import ApiResult, tracking_hash
from .constants import ACCOUNT_BLOCKING_OUTCOMES, NOT_CONSUMED_OUTCOMES, LookupOutcome, LookupTrigger
from .models import CourierTrackingSettings, TrackingLookup, TrackingQuotaMonth

logger = logging.getLogger(__name__)

# Reservations older than this never completed (worker died mid-request); they are counted as consumed.
STALE_RESERVATION = timedelta(minutes=10)
RESERVE_EXEMPT_TRIGGERS = (LookupTrigger.INITIAL, LookupTrigger.STAFF)


def current_period(now: datetime | None = None) -> str:
    now = (now or timezone.now()).astimezone(dt_timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


@dataclass
class QuotaStatus:
    period: str
    quota: int
    quota_source: str  # "provider" or "configured"
    used: int
    local_consumed: int
    provider_used: int | None
    in_flight: int
    remaining: int
    reserve: int
    blocked_outcome: str
    plan: str
    provider_seen_at: str | None
    percent: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _month(period: str, *, lock: bool) -> TrackingQuotaMonth:
    TrackingQuotaMonth.objects.get_or_create(period=period)
    qs = TrackingQuotaMonth.objects
    return (qs.select_for_update() if lock else qs).get(period=period)


def _effective(month: TrackingQuotaMonth, cfg: CourierTrackingSettings) -> tuple[int, int, str]:
    quota = month.provider_quota if month.provider_quota else cfg.monthly_quota_fallback
    source = "provider" if month.provider_quota else "configured"
    used = max(month.provider_used or 0, month.local_consumed) + month.in_flight
    return quota, used, source


def status(period: str | None = None) -> QuotaStatus:
    period = period or current_period()
    cfg = CourierTrackingSettings.load()
    month = TrackingQuotaMonth.objects.filter(period=period).first() or TrackingQuotaMonth(period=period)
    quota, used, source = _effective(month, cfg)
    return QuotaStatus(
        period=period,
        quota=quota,
        quota_source=source,
        used=used,
        local_consumed=month.local_consumed,
        provider_used=month.provider_used,
        in_flight=month.in_flight,
        remaining=max(0, quota - used),
        reserve=cfg.safety_reserve,
        blocked_outcome=month.blocked_outcome,
        plan=month.provider_plan,
        provider_seen_at=month.provider_seen_at.isoformat() if month.provider_seen_at else None,
        percent=round(100.0 * used / quota, 1) if quota else 100.0,
    )


@dataclass
class Reservation:
    lookup: TrackingLookup | None
    refused: str = ""  # why no slot was given (empty when reserved)


def release_stale_reservations(period: str | None = None) -> int:
    """Close reservations whose request never completed. They may have reached the provider: count them."""
    period = period or current_period()
    cutoff = timezone.now() - STALE_RESERVATION
    closed = 0
    with transaction.atomic():
        month = _month(period, lock=True)
        stale = list(
            TrackingLookup.objects.select_for_update().filter(
                period=period, outcome=LookupOutcome.RESERVED, reserved_at__lt=cutoff
            )
        )
        for lookup in stale:
            lookup.outcome = LookupOutcome.ABANDONED
            lookup.consumed = None
            lookup.completed_at = timezone.now()
            lookup.save(update_fields=["outcome", "consumed", "completed_at"])
            month.in_flight = max(0, month.in_flight - 1)
            month.local_consumed += 1
            closed += 1
        if closed:
            month.save(update_fields=["in_flight", "local_consumed", "updated_at"])
    return closed


def reserve(*, trigger: str, courier_slug: str, tracking_number: str, arrival=None, requested_by=None,
            allow_reserve: bool = True) -> Reservation:
    """``allow_reserve=False`` keeps a reserve-exempt trigger out of the reserve (e.g. a re-registration's first
    lookup)."""
    period = current_period()
    cfg = CourierTrackingSettings.load()
    with transaction.atomic():
        month = _month(period, lock=True)
        if month.blocked_outcome:
            return Reservation(None, refused=f"blocked:{month.blocked_outcome}")
        quota, used, _ = _effective(month, cfg)
        exempt = trigger in RESERVE_EXEMPT_TRIGGERS and allow_reserve
        limit = quota if exempt else quota - cfg.safety_reserve
        if used + 1 > limit:
            return Reservation(None, refused="quota" if used + 1 > quota else "reserve")
        lookup = TrackingLookup.objects.create(
            arrival=arrival,
            period=period,
            trigger=trigger,
            requested_by=requested_by,
            courier_slug=courier_slug,
            tracking_number_hash=tracking_hash(tracking_number),
        )
        month.in_flight += 1
        month.local_requests += 1
        month.save(update_fields=["in_flight", "local_requests", "updated_at"])
    return Reservation(lookup)


def complete(lookup: TrackingLookup, result: ApiResult, *, shared_with: int = 0) -> TrackingQuotaMonth:
    """Record the answer, reconcile the provider's usage figures and set or clear the month's block."""
    with transaction.atomic():
        month = _month(lookup.period, lock=True)
        lookup = TrackingLookup.objects.select_for_update().get(pk=lookup.pk)
        if lookup.outcome != LookupOutcome.RESERVED:
            return month  # already closed (e.g. as abandoned)
        consumed = None if result.outcome in (LookupOutcome.NETWORK_ERROR,) else result.outcome not in NOT_CONSUMED_OUTCOMES
        lookup.outcome = result.outcome
        lookup.provider_error_code = result.error_code[:40]
        lookup.http_status = result.http_status
        lookup.consumed = consumed
        lookup.duration_ms = result.duration_ms
        lookup.completed_at = timezone.now()
        lookup.shared_with = shared_with
        if result.usage:
            lookup.provider_used = result.usage.used
            lookup.provider_quota = result.usage.quota
            lookup.provider_plan = result.usage.plan
        lookup.save()
        month.in_flight = max(0, month.in_flight - 1)
        if consumed is not False:
            month.local_consumed += 1  # unknown counts too: the local figure errs high
        if result.usage and result.usage.used is not None:
            # Usage reported with this request: authoritative for the month it belongs to (ours).
            month.provider_used = result.usage.used
            if result.usage.quota:
                month.provider_quota = result.usage.quota
            month.provider_plan = result.usage.plan or month.provider_plan
            month.provider_seen_at = timezone.now()
        if result.outcome in ACCOUNT_BLOCKING_OUTCOMES:
            month.blocked_outcome = result.outcome
            month.blocked_at = timezone.now()
        month.save()
    _maybe_alert(month)
    return month


def clear_block(actor=None) -> bool:
    """Main Administrator: allow lookups again after fixing the key/account (quota blocks clear at month end)."""
    with transaction.atomic():
        month = _month(current_period(), lock=True)
        if not month.blocked_outcome:
            return False
        logger.warning("Courier tracking lookup block %s cleared by %s", month.blocked_outcome, getattr(actor, "pk", None))
        month.blocked_outcome = ""
        month.blocked_at = None
        month.save(update_fields=["blocked_outcome", "blocked_at", "updated_at"])
    return True


def _maybe_alert(month: TrackingQuotaMonth) -> None:
    from .alerts import raise_alert
    from .models import AlertKind

    cfg = CourierTrackingSettings.load()
    quota, used, _ = _effective(month, cfg)
    if month.blocked_outcome == LookupOutcome.QUOTA_EXCEEDED:
        raise_alert(AlertKind.QUOTA_EXHAUSTED, f"quota:{month.period}",
                    f"TrackCourier.io reports the monthly quota is used up for {month.period}. Lookups are paused "
                    "until the month ends; registrations still work and show their last known status.")
    elif month.blocked_outcome in (LookupOutcome.AUTH_ERROR, LookupOutcome.ACCOUNT_NEEDS_ATTENTION):
        raise_alert(AlertKind.ACCOUNT_PROBLEM, f"account:{month.period}:{month.blocked_outcome}",
                    "TrackCourier.io rejected the API key or the account needs attention. Lookups are paused; fix "
                    "the key or account, then clear the block in Courier Tracking settings.")
    if quota and used * 100 >= quota * cfg.alert_threshold_percent and not month.alert_sent_at:
        raise_alert(AlertKind.QUOTA_THRESHOLD, f"threshold:{month.period}",
                    f"Courier tracking has used {used} of {quota} lookups for {month.period} "
                    f"({cfg.alert_threshold_percent}% alert threshold).")
        TrackingQuotaMonth.objects.filter(pk=month.pk).update(alert_sent_at=timezone.now())
