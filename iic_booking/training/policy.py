"""Effective policy resolution, versioning and working-day arithmetic."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import PolicyScope, TrainingPolicy

POLICY_FIELDS = (
    "per_faculty_cap",
    "per_department_pct",
    "reserved_pct",
    "underrepresented_override_department_ids",
    "scoring_weights",
    "cooldown_months",
    "min_tenure_months_after_training",
    "suspension_lookback_months",
    "trained_validity_months",
    "dormancy_months",
    "seat_confirm_hours",
    "appeal_working_days",
    "proposal_expiry_working_days",
    "review_sla_working_days",
    "demo_rate_per_hour",
    "demo_max_minutes",
    "demo_refund_full_days",
    "demo_refund_half_days",
    "notes",
)


def _latest(qs):
    return qs.filter(is_active=True).order_by("-version", "-id").first()


def effective_policy(equipment=None) -> TrainingPolicy:
    """Most specific active row: equipment, then its internal department, then global, then code defaults."""
    if equipment is not None:
        row = _latest(TrainingPolicy.objects.filter(scope=PolicyScope.EQUIPMENT, equipment=equipment))
        if row:
            return row
        dept_id = getattr(equipment, "internal_department_id", None)
        if dept_id:
            row = _latest(TrainingPolicy.objects.filter(scope=PolicyScope.DEPARTMENT, department_id=dept_id))
            if row:
                return row
    row = _latest(TrainingPolicy.objects.filter(scope=PolicyScope.GLOBAL))
    return row or TrainingPolicy(scope=PolicyScope.GLOBAL, version=0)


def policy_snapshot(policy: TrainingPolicy) -> dict:
    data = {}
    for field in POLICY_FIELDS:
        value = getattr(policy, field)
        data[field] = str(value) if isinstance(value, Decimal) else value
    data["scoring_weights"] = policy.weights()
    data["policy_id"] = policy.pk
    data["version"] = policy.version
    data["scope"] = policy.scope
    return data


@transaction.atomic
def publish_new_version(*, scope: str, department=None, equipment=None, data: dict, actor) -> TrainingPolicy:
    """Edits never mutate a published row: the previous version is deactivated and a new one created."""
    filters = {"scope": scope, "department": department, "equipment": equipment}
    previous = TrainingPolicy.objects.select_for_update().filter(**filters).order_by("-version", "-id").first()
    if previous is not None:
        base = previous
    elif scope == PolicyScope.GLOBAL:
        base = None
    else:
        # A new override starts from whatever currently applies to that target.
        base = effective_policy(equipment)
    values = {f: getattr(base, f) for f in POLICY_FIELDS} if base is not None and base.pk else {}
    for field in POLICY_FIELDS:
        if field in data:
            values[field] = data[field]
    TrainingPolicy.objects.filter(**filters, is_active=True).update(is_active=False)
    return TrainingPolicy.objects.create(
        **filters,
        **values,
        version=(previous.version + 1) if previous else 1,
        is_active=True,
        published_at=timezone.now(),
        created_by=actor,
    )


def _holidays(start, end) -> set:
    from iic_booking.equipment.models import Holiday

    return set(Holiday.objects.filter(date__gte=start, date__lte=end, is_active=True).values_list("date", flat=True))


def add_working_days(start: datetime, days: int) -> datetime:
    """Same wall-clock time ``days`` working days later (skips Saturday, Sunday and active holidays)."""
    local = timezone.localtime(start)
    holidays = _holidays(local.date(), local.date() + timedelta(days=days * 3 + 14))
    current = local
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() >= 5 or current.date() in holidays:
            continue
        remaining -= 1
    return current


def working_days_between(start: datetime, end: datetime) -> int:
    s, e = timezone.localtime(start).date(), timezone.localtime(end).date()
    if e <= s:
        return 0
    holidays = _holidays(s, e)
    count, day = 0, s
    while day < e:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in holidays:
            count += 1
    return count
