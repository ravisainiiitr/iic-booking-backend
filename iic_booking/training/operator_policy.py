"""Operator duty and fairness policy: resolution (equipment → department → global → defaults) and versioned edits."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import DatabaseError, transaction
from django.utils import timezone

from .errors import TrainingError
from .models import DEFAULT_DUTY_WEIGHTS, OperatorPolicy, PolicyScope

logger = logging.getLogger(__name__)

FIELDS = (
    "selection_cooldown_days",
    "selection_cooldown_blocks",
    "group_repeat_penalty",
    "duty_confirmation_required",
    "duty_confirm_hours",
    "duty_reminder_hours",
    "duty_max_hours_week",
    "duty_max_hours_term",
    "duty_cooling_days",
    "duty_fairness_weights",
    "duty_hourly_rate",
    "expiry_reminder_days",
    "notes",
)

INT_RANGES = {
    "selection_cooldown_days": (0, 1095),
    "group_repeat_penalty": (0, 30),
    "duty_confirm_hours": (1, 168),
    "duty_reminder_hours": (0, 72),
    "duty_max_hours_week": (1, 80),
    "duty_max_hours_term": (1, 1000),
    "duty_cooling_days": (0, 60),
    "expiry_reminder_days": (0, 180),
}
BOOL_FIELDS = ("selection_cooldown_blocks", "duty_confirmation_required")


def _latest(qs):
    return qs.filter(is_active=True).order_by("-version", "-id").first()


def effective(equipment=None) -> OperatorPolicy:
    """Most specific active row; an unsaved default (version 0) when none exists or the table is not migrated yet."""
    try:
        with transaction.atomic():
            if equipment is not None:
                row = _latest(OperatorPolicy.objects.filter(scope=PolicyScope.EQUIPMENT, equipment=equipment))
                if row:
                    return row
                dept_id = getattr(equipment, "internal_department_id", None)
                if dept_id:
                    row = _latest(OperatorPolicy.objects.filter(scope=PolicyScope.DEPARTMENT, department_id=dept_id))
                    if row:
                        return row
            row = _latest(OperatorPolicy.objects.filter(scope=PolicyScope.GLOBAL))
            if row:
                return row
    except DatabaseError:
        logger.warning("operator policy unavailable; using defaults", exc_info=True)
    return OperatorPolicy(scope=PolicyScope.GLOBAL, version=0)


def snapshot(policy: OperatorPolicy) -> dict:
    data = {}
    for f in FIELDS:
        value = getattr(policy, f)
        data[f] = str(value) if isinstance(value, Decimal) else value
    data["duty_fairness_weights"] = policy.duty_weights()
    data["operator_policy_id"] = policy.pk
    data["operator_policy_version"] = policy.version
    return data


def out(policy: OperatorPolicy) -> dict:
    from .serializers import equipment_brief, iso, user_brief

    return {
        "id": policy.pk,
        "scope": policy.scope,
        "department": {"id": policy.department_id, "name": policy.department.name} if policy.department_id else None,
        "equipment": equipment_brief(policy.equipment) if policy.equipment_id else None,
        "version": policy.version,
        "is_active": policy.is_active,
        **snapshot(policy),
        "published_at": iso(policy.published_at),
        "created_by": user_brief(policy.created_by)["name"] if policy.created_by_id else None,
    }


def clean(raw: dict) -> dict:
    data = {}
    for f, (lo, hi) in INT_RANGES.items():
        if f in raw and raw[f] not in (None, ""):
            try:
                v = int(raw[f])
            except (TypeError, ValueError):
                raise TrainingError(f"{f} must be a whole number.") from None
            if not lo <= v <= hi:
                raise TrainingError(f"{f} must be between {lo} and {hi}.")
            data[f] = v
    for f in BOOL_FIELDS:
        if f in raw and raw[f] is not None:
            v = raw[f]
            if isinstance(v, str):
                v = v.strip().lower() in {"1", "true", "yes", "on"}
            data[f] = bool(v)
    if "duty_hourly_rate" in raw and raw["duty_hourly_rate"] not in (None, ""):
        try:
            rate = Decimal(str(raw["duty_hourly_rate"]))
        except InvalidOperation:
            raise TrainingError("duty_hourly_rate must be a number.") from None
        if rate < 0 or rate > 100000:
            raise TrainingError("duty_hourly_rate must be between 0 and 100000.")
        data["duty_hourly_rate"] = rate.quantize(Decimal("0.01"))
    if "duty_fairness_weights" in raw:
        weights = raw.get("duty_fairness_weights") or {}
        if not isinstance(weights, dict):
            raise TrainingError("duty_fairness_weights must be an object.")
        clean_w = {}
        for k, v in weights.items():
            if k not in DEFAULT_DUTY_WEIGHTS:
                continue
            try:
                clean_w[k] = float(v)
            except (TypeError, ValueError):
                raise TrainingError(f"Weight {k} must be a number.") from None
        data["duty_fairness_weights"] = clean_w
    if "notes" in raw:
        data["notes"] = (raw.get("notes") or "").strip()
    return data


@transaction.atomic
def publish(*, scope: str, department=None, equipment=None, data: dict, actor) -> OperatorPolicy:
    filters = {"scope": scope, "department": department, "equipment": equipment}
    previous = OperatorPolicy.objects.select_for_update().filter(**filters).order_by("-version", "-id").first()
    base = previous if previous is not None else (None if scope == PolicyScope.GLOBAL else effective(equipment))
    values = {f: getattr(base, f) for f in FIELDS} if base is not None and base.pk else {}
    values.update({f: data[f] for f in FIELDS if f in data})
    OperatorPolicy.objects.filter(**filters, is_active=True).update(is_active=False)
    return OperatorPolicy.objects.create(
        **filters,
        **values,
        version=(previous.version + 1) if previous else 1,
        is_active=True,
        published_at=timezone.now(),
        created_by=actor,
    )
