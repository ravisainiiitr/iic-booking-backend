"""
Equipment Group as an alternative-resource pool.

The existing quota-oriented ``EquipmentGroup`` doubles as an administratively defined set of
alternative equipment. Everything here is gated by an environment flag AND a per-group switch
(see docs/equipment_group_alternatives.md); with the flags off none of these code paths run.

This module only *orchestrates* existing booking rules (visibility, department locks, status,
charge profiles, multi-mode schedules, home-department slot rules, availability checker, time
window, time/charge engines). Final booking / rescheduling always re-runs the authoritative
code paths in ``api_views`` inside their own transactions.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Optional

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("iic_booking.equipment_group")

MAX_ALTERNATIVES = 5
ALTERNATIVES_AVAILABLE_CODE = "GROUP_ALTERNATIVES_AVAILABLE"
SLOT_TAKEN_MESSAGE = "The selected slot is no longer available. Please select another available slot."


def _log_event(event: str, **fields: Any) -> None:
    logger.info("equipment_group.%s %s", event, " ".join(f"{k}={v}" for k, v in fields.items()))


# ---------------------------------------------------------------------------
# Feature flags (global env flag AND per-group switch)
# ---------------------------------------------------------------------------


def _group_of(equipment):
    if equipment is None or not getattr(equipment, "equipment_group_id", None):
        return None
    return equipment.equipment_group


def alternative_booking_enabled(equipment) -> bool:
    if not getattr(settings, "EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED", False):
        return False
    group = _group_of(equipment)
    return bool(group and group.alternative_booking_enabled)


def auto_allocation_enabled(equipment) -> bool:
    if not getattr(settings, "EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED", False):
        return False
    if not alternative_booking_enabled(equipment):
        return False
    return bool(_group_of(equipment).auto_allocation_enabled)


def cross_rescheduling_enabled(equipment) -> bool:
    if not getattr(settings, "EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED", False):
        return False
    group = _group_of(equipment)
    return bool(group and group.cross_rescheduling_enabled)


# ---------------------------------------------------------------------------
# Group membership and eligibility
# ---------------------------------------------------------------------------


GROUP_DEPARTMENT_MISMATCH_MESSAGE = "All equipment in an Equipment Group must belong to the same department."

_UNSET = object()


def group_membership_spans_departments(*, group_id=None, equipment_ids=(), exclude_ids=(), department_id=_UNSET) -> bool:
    """
    True when the resulting group membership would contain equipment from more than one
    department (no department counts as its own value). Resulting membership = current members
    of ``group_id`` minus ``exclude_ids``, plus ``equipment_ids``, plus one member whose
    department is ``department_id`` when given (equipment being assigned / re-departmented).
    One query; used by every group-membership write path.
    """
    from django.db.models import Q

    from .models import Equipment

    ids = [int(i) for i in equipment_ids]
    departments = set()
    if ids or group_id:
        q = Q(pk__in=ids)
        if group_id:
            q |= Q(equipment_group_id=group_id) & ~Q(pk__in=[int(i) for i in exclude_ids])
        departments = set(
            Equipment.objects.filter(q).order_by().values_list("internal_department_id", flat=True).distinct()
        )
    if department_id is not _UNSET:
        departments.add(department_id)
    return len(departments) > 1


def get_group_members(equipment, *, exclude_self: bool = True):
    from .models import Equipment

    if not getattr(equipment, "equipment_group_id", None):
        return Equipment.objects.none()
    # Groups are single-department; the department filter only guards against legacy data.
    qs = Equipment.objects.filter(
        equipment_group_id=equipment.equipment_group_id,
        internal_department_id=equipment.internal_department_id,
    ).select_related("internal_department", "equipment_group")
    if exclude_self:
        qs = qs.exclude(pk=equipment.pk)
    return qs.order_by("alternative_priority", "equipment_id")


def get_active_group_members(equipment, *, exclude_self: bool = True):
    from .models import EquipmentStatus

    return get_group_members(equipment, exclude_self=exclude_self).filter(status=EquipmentStatus.ACTIVE)


def _booking_user_type(booking_user) -> str:
    from iic_booking.users.models import UserType

    return getattr(booking_user, "user_type", None) or UserType.STUDENT


def resolve_charge_profile(booking_user, equipment, *, user_type: Optional[str] = None):
    """Same lookup as book-equipment: (equipment, user_type, pricing profile, active)."""
    from .api_views import _get_charge_profile_pricing_profile_for_user
    from .models import ChargeProfile

    ut = user_type or _booking_user_type(booking_user)
    return ChargeProfile.objects.filter(
        equipment=equipment,
        user_type=ut,
        pricing_profile=_get_charge_profile_pricing_profile_for_user(booking_user, equipment),
        is_active=True,
    ).first()


def equipment_eligibility_error(booking_user, equipment, *, user_type: Optional[str] = None) -> Optional[str]:
    """
    Return None when ``booking_user`` may book ``equipment`` under the existing rules, else a reason.
    Mirrors the gates at the top of ``_book_equipment_impl`` (visibility, department lock, status,
    external slot share, charge profile).
    """
    from iic_booking.users.legacy_ledger.booking_lock import department_equipment_booking_blocked
    from iic_booking.users.models import UserType

    from .api_views import user_can_see_equipment
    from .models import EquipmentProfileType, EquipmentStatus

    if (equipment.status or "").strip() != EquipmentStatus.ACTIVE:
        return "Equipment is not operational."
    if getattr(equipment, "profile_type", None) == EquipmentProfileType.PRINT_3D:
        return "3D printing requests are tied to a specific printer."
    if not user_can_see_equipment(booking_user, equipment):
        return "You are not authorized to access this equipment."
    blocked, message = department_equipment_booking_blocked(equipment, booking_user)
    if blocked:
        return message or "Booking is disabled for this department."
    ut = user_type or _booking_user_type(booking_user)
    if UserType.is_external_user(ut) and int(getattr(equipment, "external_slot_quota_percent", 0) or 0) <= 0:
        return "External bookings are not open on this equipment."
    if resolve_charge_profile(booking_user, equipment, user_type=ut) is None:
        return "No active charge profile for your user type."
    return None


def get_eligible_group_equipment(booking_user, equipment, *, user_type: Optional[str] = None) -> list:
    eligible = []
    for member in get_active_group_members(equipment):
        if equipment_eligibility_error(booking_user, member, user_type=user_type) is None:
            eligible.append(member)
    return eligible


# ---------------------------------------------------------------------------
# Input inheritance (field_key + field_type + label, validated against target)
# ---------------------------------------------------------------------------


def _norm_label(label: str) -> str:
    return " ".join(str(label or "").lower().replace("_", " ").split())


def _effective_input_fields(equipment, user_type: str) -> list:
    """Same resolution as the booking form: typed rows for the user type, else legacy '' rows."""
    from .models import DynamicInputField

    qs = DynamicInputField.objects.filter(equipment=equipment)
    typed = list(qs.filter(user_type=user_type).order_by("field_key"))
    if typed:
        return typed
    return list(qs.filter(user_type="").order_by("field_key"))


def _option_values(options) -> Optional[set]:
    if not isinstance(options, list) or not options:
        return None
    values = set()
    for i, opt in enumerate(options):
        if isinstance(opt, (str, int, float, bool)):
            values.add(str(opt))
        elif isinstance(opt, dict):
            for key in ("value", "label", "id", "name"):
                v = opt.get(key)
                if isinstance(v, (str, int, float, bool)):
                    values.add(str(v))
        else:
            values.add(str(i + 1))
    return values or None


def _value_fits_options(field, value) -> bool:
    from .models import DynamicInputFieldType

    if field.field_type not in (
        DynamicInputFieldType.RADIO,
        DynamicInputFieldType.COMBO,
        DynamicInputFieldType.MULTI_SELECT,
    ):
        return True
    allowed = _option_values(field.options)
    if allowed is None:
        return True
    items = value if isinstance(value, list) else [value]
    return all(str(v) in allowed for v in items)


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value is False


def _structured_field_types() -> tuple:
    from .models import DynamicInputFieldType

    return (
        DynamicInputFieldType.PERIODIC_TABLE,
        DynamicInputFieldType.TABLE,
        DynamicInputFieldType.ICPMS_STANDARD_COVERAGE,
    )


def _fields_compatible(src_field, tgt_field) -> bool:
    """Structured fields (tables, element selectors) depend on their options and linked field key,
    so they are only carried over when the target definition is identical."""
    if src_field.field_type != tgt_field.field_type:
        return False
    if src_field.field_type in _structured_field_types():
        return (
            src_field.field_key == tgt_field.field_key
            and (src_field.options or None) == (tgt_field.options or None)
            and (src_field.source_element_field_key or None) == (tgt_field.source_element_field_key or None)
        )
    return True


@dataclass
class InputMapping:
    values: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)
    missing_required: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        return not self.missing_required and not self.error

    def as_dict(self) -> dict:
        return {
            "input_values": self.values,
            "dropped_fields": self.dropped,
            "missing_required_fields": self.missing_required,
            "input_error": self.error,
        }


def map_inputs(source_equipment, target_equipment, user_type: str, values: dict, booking_user=None) -> InputMapping:
    """
    Copy only compatible inputs from source to target. A value is copied when the target has a field
    with the same key, type and (normalised) label, or failing that the same type and label under a
    different key. Choice values must be valid options on the target; numeric limits are checked with
    the booking validator. Unmatched target-required fields are reported, never guessed.
    """
    from .api_views import _validate_dynamic_numeric_input_limits

    values = dict(values or {})
    result = InputMapping()
    if source_equipment.pk == target_equipment.pk:
        result.values = values
        return result

    src_fields = {f.field_key: f for f in _effective_input_fields(source_equipment, user_type)}
    tgt_fields = _effective_input_fields(target_equipment, user_type)
    tgt_by_key = {f.field_key: f for f in tgt_fields}
    tgt_by_label_type = {(_norm_label(f.field_label), f.field_type): f for f in tgt_fields}

    key_map: dict[str, str] = {}
    for src_key, src_field in src_fields.items():
        label = _norm_label(src_field.field_label)
        same_key = tgt_by_key.get(src_key)
        if (
            same_key is not None
            and _norm_label(same_key.field_label) == label
            and _fields_compatible(src_field, same_key)
        ):
            key_map[src_key] = src_key
            continue
        by_label = tgt_by_label_type.get((label, src_field.field_type))
        if by_label is not None and _fields_compatible(src_field, by_label):
            key_map[src_key] = by_label.field_key

    for base in list(key_map):
        if base in values and not _value_fits_options(tgt_by_key[key_map[base]], values[base]):
            del key_map[base]

    for raw_key, value in values.items():
        base, sep, suffix = str(raw_key).partition("_")
        src_field = src_fields.get(base)
        if src_field is None:
            continue
        target_key = key_map.get(base)
        if target_key is None:
            if not sep and not _is_empty(value):
                result.dropped.append({"key": base, "label": src_field.field_label})
            continue
        new_key = f"{target_key}{sep}{suffix}" if sep else target_key
        result.values[new_key] = value

    for f in tgt_fields:
        if f.is_required and _is_empty(result.values.get(f.field_key)):
            result.missing_required.append({"key": f.field_key, "label": f.field_label})

    result.error = _validate_dynamic_numeric_input_limits(
        target_equipment, result.values, booking_user=booking_user
    )
    return result


# ---------------------------------------------------------------------------
# Time / charge (existing engines)
# ---------------------------------------------------------------------------


def required_minutes_for(equipment, charge_profile, input_values: dict, fallback: int) -> int:
    from .calculators import TimeCalculationEngine, build_safe_input_values_for_charge_calculation

    try:
        safe = build_safe_input_values_for_charge_calculation(dict(input_values or {}), equipment=equipment)
        minutes = int(
            TimeCalculationEngine.calculate_time(
                charge_profile, safe, slot_duration_minutes=equipment.slot_duration_minutes
            )
            or 0
        )
        return minutes if minutes > 0 else int(fallback or 0)
    except Exception:
        return int(fallback or 0)


def estimated_charge_for(equipment, charge_profile, input_values: dict, total_minutes: int) -> Optional[str]:
    from .calculators import ChargeCalculationEngine, build_safe_input_values_for_charge_calculation

    try:
        safe = build_safe_input_values_for_charge_calculation(dict(input_values or {}), equipment=equipment)
        charge, _breakdown = ChargeCalculationEngine.calculate_charge(
            charge_profile, safe, total_minutes, selected_parameters=None
        )
        return str(charge)
    except Exception:
        return None


def slots_needed(equipment, minutes: int) -> int:
    from .slot_allocation import slot_tolerance_minutes_for, slots_needed_for_analysis_time

    return max(
        1,
        int(
            slots_needed_for_analysis_time(
                minutes, equipment.slot_duration_minutes, slot_tolerance_minutes_for(equipment)
            )
            or 1
        ),
    )


# ---------------------------------------------------------------------------
# Slot checks (existing availability rules)
# ---------------------------------------------------------------------------


def _slot_in_time_window(equipment, slot) -> bool:
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    if time_from is None and time_to is None:
        return True
    if not slot.start_datetime or not slot.end_datetime:
        return False
    st = timezone.localtime(slot.start_datetime).time() if timezone.is_aware(slot.start_datetime) else slot.start_datetime.time()
    et = timezone.localtime(slot.end_datetime).time() if timezone.is_aware(slot.end_datetime) else slot.end_datetime.time()
    if time_from is not None and st < time_from:
        return False
    if time_to is not None and et > time_to:
        return False
    return True


def slot_passes_booking_rules(equipment, slot, *, booking_user, actor, user_type: str, is_admin: bool,
                              exclude_slot_ids=None) -> bool:
    """The per-slot checks of the book-equipment slot_ids path, without locking."""
    from iic_booking.users.models import UserType

    from .mode_utils import bypasses_multimode_restrictions, equipment_bookable_on_date, family_slots_overlap_conflict
    from .models import SlotStatus
    from .slot_department_access import slot_allows_internal_user
    from .slot_utils import SlotAvailabilityChecker

    if slot.booking_id is not None or slot.status != SlotStatus.AVAILABLE:
        return False
    if not is_admin:
        external = UserType.is_external_user(user_type)
        checker = (
            SlotAvailabilityChecker.is_slot_available_for_external
            if external
            else SlotAvailabilityChecker.is_slot_available
        )
        if not checker(slot):
            return False
        if not external and not slot_allows_internal_user(slot, booking_user, equipment):
            return False
        if not _slot_in_time_window(equipment, slot):
            return False
    if not bypasses_multimode_restrictions(actor):
        at_time = None
        if slot.start_datetime:
            at_time = (timezone.localtime(slot.start_datetime) if timezone.is_aware(slot.start_datetime) else slot.start_datetime).time()
        ok, _err = equipment_bookable_on_date(equipment, slot.date, at_time)
        if not ok:
            return False
        if slot.start_datetime and slot.end_datetime and family_slots_overlap_conflict(
            equipment, slot.start_datetime, slot.end_datetime, exclude_slot_ids=list(exclude_slot_ids or [])
        ) is not None:
            return False
    return True


def slot_window_bounds(equipment, user_type: str, is_admin: bool):
    """Bookable date range used by the slots API for this viewer (without maintenance extensions)."""
    from iic_booking.users.models import UserType

    from .api_views import get_equipment_slot_window_reference_config, get_internal_slot_window_date_bounds
    from .external_slot_quota import ExternalSlotQuotaService

    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    if is_admin:
        return today, monday + timedelta(days=13)
    if UserType.is_external_user(user_type):
        lo, hi, _ = ExternalSlotQuotaService.get_external_slot_window_date_bounds(equipment)
        if lo is None or hi is None:
            lo = monday + timedelta(days=7)
            hi = lo + timedelta(days=6)
        return max(lo, today), hi
    rw, rt = get_equipment_slot_window_reference_config(equipment)
    if rw is not None and rt is not None:
        lo, hi, _ = get_internal_slot_window_date_bounds(equipment, timezone.localtime(timezone.now()))
        if lo is not None and hi is not None:
            return max(lo, today), hi
    return today, monday + timedelta(days=13)


def _ensure_slots(equipment, date_from, date_to) -> None:
    from .slot_utils import SlotGenerator

    try:
        SlotGenerator.ensure_slot_masters_exist(equipment)
        SlotGenerator.generate_slots_for_week(equipment, date_from, date_to, allow_holiday=True)
    except Exception:
        logger.warning("equipment_group slot generation failed equipment=%s", equipment.pk, exc_info=True)


def _pick_consecutive(slots, equipment, required_minutes: int, accept: Optional[Callable] = None):
    """Accumulate ordered slots until capacity covers the analysis time. ``accept`` is evaluated
    lazily so the per-slot booking-rule queries stop at the first usable run."""
    from .slot_allocation import allocated_capacity_covers_analysis, slot_tolerance_minutes_for

    tolerance = slot_tolerance_minutes_for(equipment)
    picked, total = [], 0
    for s in slots:
        if not s.start_datetime or not s.end_datetime:
            continue
        if accept is not None and not accept(s):
            picked, total = [], 0
            continue
        if picked and s.start_datetime != picked[-1].end_datetime:
            picked, total = [], 0
        picked.append(s)
        total += int((s.end_datetime - s.start_datetime).total_seconds() / 60)
        if allocated_capacity_covers_analysis(total, required_minutes, tolerance):
            return picked
    return None


def find_exact_window_slots(equipment, required_minutes: int, window_start, window_end, **rule_kwargs):
    from .models import DailySlot, SlotStatus

    candidates = list(
        DailySlot.objects.filter(
            slot_master__equipment=equipment,
            status=SlotStatus.AVAILABLE,
            booking__isnull=True,
            start_datetime__gte=window_start,
            start_datetime__lt=window_end,
        ).order_by("start_datetime")
    )
    return _pick_consecutive(
        candidates, equipment, required_minutes,
        accept=lambda s: slot_passes_booking_rules(equipment, s, **rule_kwargs),
    )


def find_earliest_slots(equipment, required_minutes: int, date_from, date_to, *, not_before=None, **rule_kwargs):
    from .models import DailySlot, SlotStatus

    qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        status=SlotStatus.AVAILABLE,
        booking__isnull=True,
        date__gte=date_from,
        date__lte=date_to,
    ).order_by("start_datetime")
    if not_before is not None:
        qs = qs.filter(start_datetime__gte=not_before)
    return _pick_consecutive(
        qs[:400], equipment, required_minutes,
        accept=lambda s: slot_passes_booking_rules(equipment, s, **rule_kwargs),
    )


# ---------------------------------------------------------------------------
# Capability A: alternatives during new booking
# ---------------------------------------------------------------------------


def _equipment_summary(equipment) -> dict:
    dept = getattr(equipment, "internal_department", None)
    return {
        "equipment_id": equipment.equipment_id,
        "code": equipment.code,
        "name": equipment.name,
        "make": equipment.make or "",
        "model_information": equipment.model_information or "",
        "internal_department_name": getattr(dept, "name", None),
    }


def find_alternatives(*, actor, booking_user, equipment, input_values: dict,
                      requested_slot_ids=None, visible_week_start=None, visible_week_end=None) -> list[dict]:
    """
    Read-only discovery of alternative (equipment, slots) on other group members.
    Never locks rows or consumes quota. Ordered: exact requested window, earliest start,
    alternative_priority, equipment_id. At most MAX_ALTERNATIVES results.
    """
    from iic_booking.users.models import UserType

    from .models import DailySlot

    user_type = _booking_user_type(booking_user)
    is_admin = getattr(actor, "user_type", None) in UserType.get_admin_panel_codes()
    group = _group_of(equipment)
    if group is None:
        return []

    window_start = window_end = None
    requested_minutes = 0
    if requested_slot_ids:
        req_slots = list(
            DailySlot.objects.filter(id__in=list(requested_slot_ids), slot_master__equipment=equipment).order_by(
                "start_datetime"
            )
        )
        if req_slots:
            window_start = req_slots[0].start_datetime
            window_end = req_slots[-1].end_datetime
            requested_minutes = sum(
                int((s.end_datetime - s.start_datetime).total_seconds() / 60)
                for s in req_slots
                if s.start_datetime and s.end_datetime
            )

    window_date = None
    if window_start is not None:
        window_date = timezone.localtime(window_start).date() if timezone.is_aware(window_start) else window_start.date()

    results = []
    for member in get_active_group_members(equipment):
        reason = equipment_eligibility_error(booking_user, member, user_type=user_type)
        if reason:
            continue
        # Each member has its own slot-window configuration; never search outside what this
        # user could see on that equipment.
        date_from, date_to = slot_window_bounds(member, user_type, is_admin)
        if visible_week_start and visible_week_start > date_from:
            date_from = visible_week_start
        if date_from > date_to:
            continue
        cp = resolve_charge_profile(booking_user, member, user_type=user_type)
        mapping = map_inputs(equipment, member, user_type, input_values, booking_user=booking_user)
        fallback = requested_minutes or int(member.slot_duration_minutes or 60)
        minutes = required_minutes_for(member, cp, mapping.values, fallback)
        rule_kwargs = dict(booking_user=booking_user, actor=actor, user_type=user_type, is_admin=is_admin)

        _ensure_slots(member, date_from, date_to)
        slots = None
        exact = False
        if window_start is not None and window_end is not None and date_from <= window_date <= date_to:
            slots = find_exact_window_slots(member, minutes, window_start, window_end, **rule_kwargs)
            exact = bool(slots)
        if not slots and group.alternative_search_other_slots:
            slots = find_earliest_slots(member, minutes, date_from, date_to, not_before=timezone.now(), **rule_kwargs)
        if not slots:
            continue
        total_minutes = sum(int((s.end_datetime - s.start_datetime).total_seconds() / 60) for s in slots)
        results.append(
            {
                **_equipment_summary(member),
                "alternative_priority": member.alternative_priority,
                "exact_match": exact,
                "slot_ids": [s.id for s in slots],
                "start": slots[0].start_datetime.isoformat(),
                "end": slots[-1].end_datetime.isoformat(),
                "date": slots[0].date.isoformat() if hasattr(slots[0].date, "isoformat") else str(slots[0].date),
                "required_minutes": minutes,
                "estimated_charge": estimated_charge_for(member, cp, mapping.values, total_minutes),
                **mapping.as_dict(),
            }
        )

    results.sort(key=lambda r: (0 if r["exact_match"] else 1, r["start"], r["alternative_priority"], r["equipment_id"]))
    return results[:MAX_ALTERNATIVES]


def validate_alternative_source(source_equipment_id, target_equipment) -> Optional[Any]:
    """Return the source equipment when it is a same-group member with alternatives enabled, else None."""
    from .models import Equipment

    try:
        sid = int(source_equipment_id)
    except (TypeError, ValueError):
        return None
    if sid == target_equipment.pk or not target_equipment.equipment_group_id:
        return None
    source = Equipment.objects.select_related("equipment_group").filter(pk=sid).first()
    if source is None or source.equipment_group_id != target_equipment.equipment_group_id:
        return None
    if not alternative_booking_enabled(source):
        return None
    return source


# --- Deferred waitlist (lets book_equipment offer alternatives before the existing waitlist) ---


@dataclass
class _DeferralContext:
    equipment_id: int
    deferred: Optional[dict] = None


_deferral: contextvars.ContextVar[Optional[_DeferralContext]] = contextvars.ContextVar(
    "equipment_group_waitlist_deferral", default=None
)


def defer_waitlist_for_alternatives(equipment, booking_user, error_message: str, waitlist_on_failure: bool) -> bool:
    """
    Called from ``_enrich_failed_booking_response`` for slot-unavailable failures. When a deferral
    context is active for this equipment, record the failure and skip waitlisting (the caller
    decides after the alternative search). Returns True when deferred.
    """
    ctx = _deferral.get()
    if ctx is None or ctx.equipment_id != equipment.pk:
        return False
    ctx.deferred = {
        "error_message": error_message,
        "waitlist_on_failure": waitlist_on_failure,
        "booking_user": booking_user,
    }
    return True


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _request_body(request) -> dict:
    data = request.data
    if hasattr(data, "dict"):
        return data.dict()
    return dict(data or {})


def _should_offer_alternatives(request, pk):
    if not getattr(settings, "EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED", False):
        return None
    data = request.data or {}
    if not _truthy(data.get("offer_group_alternatives")) or _truthy(data.get("skip_group_alternatives")):
        return None
    if _truthy(data.get("create_as_hold")):
        return None
    from .api_views import is_slot_window_peak_waitlist_period
    from .models import Equipment

    equipment = Equipment.objects.select_related("equipment_group").filter(pk=pk).first()
    if equipment is None or not alternative_booking_enabled(equipment):
        return None
    if is_slot_window_peak_waitlist_period(equipment):
        return None
    return equipment


def _internal_book_request(request, body: dict):
    from rest_framework.parsers import JSONParser
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    django_request = factory.post(request.path, body, format="json")
    force_authenticate(django_request, user=request.user)
    drf_request = Request(django_request, parsers=[JSONParser()])
    drf_request.user = request.user
    return drf_request


def run_booking_with_group_alternatives(request, pk, book_impl: Callable):
    """
    Wraps ``_book_equipment_impl``. With the feature off (or not requested) this is a direct call.
    Otherwise slot-unavailable failures skip the waitlist, alternatives are searched, and the
    response is either an auto-allocated booking, a 409 with alternatives, or the unchanged
    existing waitlist outcome.
    """
    equipment = _should_offer_alternatives(request, pk)
    if equipment is None:
        return book_impl(request, pk)

    from rest_framework import status
    from rest_framework.response import Response

    ctx = _DeferralContext(equipment_id=equipment.pk)
    token = _deferral.set(ctx)
    try:
        response = book_impl(request, pk)
    finally:
        _deferral.reset(token)
    if ctx.deferred is None or int(getattr(response, "status_code", 500)) < 400:
        return response

    deferred = ctx.deferred
    booking_user = deferred["booking_user"]
    data = request.data or {}
    _log_event("alternative_search", equipment=equipment.pk, user=getattr(booking_user, "pk", None))

    alternatives: list[dict] = []
    try:
        from django.utils.dateparse import parse_date

        ws = data.get("visible_week_start")
        we = data.get("visible_week_end")
        alternatives = find_alternatives(
            actor=request.user,
            booking_user=booking_user,
            equipment=equipment,
            input_values=data.get("input_values") or {},
            requested_slot_ids=[int(x) for x in (data.get("slot_ids") or []) if str(x).strip().isdigit()],
            visible_week_start=parse_date(ws) if isinstance(ws, str) else ws,
            visible_week_end=parse_date(we) if isinstance(we, str) else we,
        )
    except Exception:
        logger.exception("equipment_group.alternative_search_error equipment=%s", equipment.pk)
        alternatives = []

    _log_event("alternatives_found", equipment=equipment.pk, count=len(alternatives))

    if alternatives and auto_allocation_enabled(equipment):
        viable = [a for a in alternatives if not a["missing_required_fields"] and not a["input_error"]]
        if viable:
            choice = viable[0]
            body = _request_body(request)
            for k in ("offer_group_alternatives", "book_any_available_slots", "book_even_if_single_slot_available",
                      "request_waitlist_without_slot_selection"):
                body.pop(k, None)
            body.update(
                slot_ids=choice["slot_ids"],
                input_values=choice["input_values"],
                waitlist_on_failure=False,
                alternative_of_equipment_id=equipment.pk,
            )
            internal = _internal_book_request(request, body)
            internal._egs_auto_allocated = True
            # A lost race on the target must not waitlist the user there (e.g. during its peak
            # waitlist window); the outcome is decided below for the original equipment only.
            target_token = _deferral.set(_DeferralContext(equipment_id=choice["equipment_id"]))
            try:
                auto_response = book_impl(internal, choice["equipment_id"])
            finally:
                _deferral.reset(target_token)
            if 200 <= int(getattr(auto_response, "status_code", 500)) < 300:
                _log_event("auto_allocated", equipment=equipment.pk, target=choice["equipment_id"])
                payload = dict(auto_response.data or {})
                payload["allocated_alternative"] = {
                    "original_equipment": _equipment_summary(equipment),
                    "equipment": {k: choice[k] for k in ("equipment_id", "code", "name", "make", "model_information")},
                    "start": choice["start"],
                    "end": choice["end"],
                }
                return Response(payload, status=auto_response.status_code)
            _log_event("auto_allocation_failed", equipment=equipment.pk, target=choice["equipment_id"],
                       status=getattr(auto_response, "status_code", None))
            alternatives = [a for a in alternatives if a["equipment_id"] != choice["equipment_id"]]

    if alternatives:
        return Response(
            {
                "error": "The selected equipment is not available for your requested slot. "
                "Alternative equipment in the same group is available.",
                "code": ALTERNATIVES_AVAILABLE_CODE,
                "original_error": deferred["error_message"],
                "original_equipment": _equipment_summary(equipment),
                "alternatives": alternatives,
            },
            status=status.HTTP_409_CONFLICT,
        )

    from .api_views import _enrich_failed_booking_response

    return Response(
        _enrich_failed_booking_response(
            equipment,
            booking_user,
            deferred["error_message"],
            waitlist_on_failure=deferred["waitlist_on_failure"],
            slot_unavailable_failure=True,
        ),
        status=status.HTTP_400_BAD_REQUEST,
    )


# ---------------------------------------------------------------------------
# Capability B: cross-equipment rescheduling
# ---------------------------------------------------------------------------


def booking_input_user_type(booking) -> str:
    return getattr(booking, "user_type_snapshot", None) or _booking_user_type(booking.user)


def reschedule_target_info(booking, target, *, actor) -> dict:
    """
    Eligibility of ``target`` as the equipment for rescheduling ``booking`` (flags, same group,
    target eligibility for the booking owner, input compatibility) plus required slot count.
    ``ok`` is False with ``reason`` / ``code`` when not selectable.
    """
    source = booking.equipment
    user_type = booking_input_user_type(booking)
    info = {**_equipment_summary(target), "is_original": target.pk == source.pk, "ok": True,
            "reason": None, "code": None}
    booked_minutes = int(booking.total_time_minutes or 0)

    if target.pk == source.pk:
        info.update(
            required_slots=booking.daily_slots.count() or 1,
            required_minutes=booked_minutes,
            input_values=dict(booking.input_values or {}),
            missing_required_fields=[],
            dropped_fields=[],
        )
        return info

    def _fail(code: str, reason: str):
        info.update(ok=False, code=code, reason=reason)
        return info

    if not cross_rescheduling_enabled(source):
        return _fail("CROSS_RESCHEDULING_DISABLED", "Cross-equipment rescheduling is not enabled for this equipment.")
    if not source.equipment_group_id or target.equipment_group_id != source.equipment_group_id:
        return _fail("DIFFERENT_GROUP", "The target equipment is not in the same equipment group.")
    if target.internal_department_id != source.internal_department_id:
        return _fail(
            "DEPARTMENT_MISMATCH",
            "The target equipment belongs to a different department and cannot be used for this booking.",
        )
    reason = equipment_eligibility_error(booking.user, target, user_type=user_type)
    if reason:
        return _fail("TARGET_NOT_ELIGIBLE", reason)
    cp = resolve_charge_profile(booking.user, target, user_type=user_type)
    mapping = map_inputs(source, target, user_type, dict(booking.input_values or {}), booking_user=booking.user)
    info.update(mapping.as_dict())
    if not mapping.complete:
        detail = mapping.error or "Required inputs: " + ", ".join(
            f["label"] for f in mapping.missing_required
        )
        return _fail("INPUTS_INCOMPATIBLE", f"Booking inputs are not compatible with this equipment. {detail}")
    minutes = required_minutes_for(target, cp, mapping.values, booked_minutes)
    required_slots = slots_needed(target, minutes)
    target_charge = estimated_charge_for(
        target, cp, mapping.values, required_slots * int(target.slot_duration_minutes or 60)
    )
    source_charge = source_charge_reference(booking)
    info.update(
        required_minutes=minutes,
        required_slots=required_slots,
        charge_profile_id=cp.pk if cp else None,
        target_charge_reference=target_charge,
        source_charge_reference=source_charge,
    )
    # Group members carry the same charge; the booking keeps its charge and wallet debit, so a
    # target that prices this booking differently is rejected rather than adjusted.
    if source_charge is None or target_charge is None:
        return _fail(
            "CHARGE_NOT_VERIFIED",
            "The charge for this booking could not be verified on the target equipment.",
        )
    if not _same_amount(source_charge, target_charge):
        return _fail(
            "CHARGE_MISMATCH",
            "The target equipment has a different charge for this booking. "
            "Equipment in a group must have the same charge; please contact the lab administrator.",
        )
    return info


def source_charge_reference(booking) -> Optional[str]:
    """Current charge of ``booking`` on its own equipment, computed by the existing charge engine."""
    if not booking.charge_profile_id:
        return None
    minutes = int(booking.total_time_minutes or 0)
    if minutes <= 0:
        minutes = (booking.daily_slots.count() or 1) * int(booking.equipment.slot_duration_minutes or 60)
    return estimated_charge_for(booking.equipment, booking.charge_profile, dict(booking.input_values or {}), minutes)


def _same_amount(a, b) -> bool:
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(a)).quantize(Decimal("0.01")) == Decimal(str(b)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False


def reschedule_equipment_options(booking, *, actor) -> list[dict]:
    """Original equipment first, then selectable same-group members (only when the feature is on)."""
    source = booking.equipment
    options = [reschedule_target_info(booking, source, actor=actor)]
    if not cross_rescheduling_enabled(source):
        return options
    for member in get_group_members(source):
        info = reschedule_target_info(booking, member, actor=actor)
        if info["ok"]:
            options.append(info)
    return options


def wants_cross_equipment_reschedule(request, booking) -> bool:
    """True when the request names a target equipment different from the booking's equipment."""
    raw = (request.data or {}).get("target_equipment_id")
    if raw in (None, ""):
        return False
    return str(raw).strip() != str(booking.equipment_id)


def _error(message: str, code: str, http_status: int = 400, **extra):
    from rest_framework.response import Response

    return Response({"error": message, "code": code, **extra}, status=http_status)


def perform_cross_equipment_reschedule(request, booking, start_time, end_time, *, staff_endpoint: bool):
    """
    Move ``booking`` to another member of its equipment group for [start_time, end_time).

    Called by both reschedule views after their own permission / status / deadline / maintenance
    checks. The target is re-validated server-side (flags, same group, same department, eligibility,
    inputs, same charge), slots are locked and re-checked inside the transaction, and the booking
    keeps its original charge, wallet debit, settlement department and virtual booking id; both
    charge references are stored in the history metadata.
    """
    from decimal import Decimal

    from django.db import transaction
    from django.db.models import Q
    from rest_framework import status
    from rest_framework.response import Response

    from iic_booking.users.models import UserType

    from . import api_views as av
    from .external_slot_quota import ExternalSlotQuotaService
    from .mode_utils import bypasses_multimode_restrictions, equipment_bookable_on_date
    from .models import Booking, BookingEventType, BookingStatus, DailySlot, Equipment, Holiday, SlotStatus
    from .slot_utils import SlotAvailabilityChecker

    source = booking.equipment
    try:
        target_id = int(str(request.data.get("target_equipment_id")).strip())
    except (TypeError, ValueError):
        return _error("Invalid target equipment.", "TARGET_INVALID")
    target = (
        Equipment.objects.select_related("equipment_group", "internal_department").filter(pk=target_id).first()
    )
    if target is None:
        return _error("Target equipment not found.", "TARGET_INVALID")

    info = reschedule_target_info(booking, target, actor=request.user)
    if not info["ok"]:
        _log_event("cross_reschedule_rejected", booking=booking.pk, source=source.pk, target=target.pk,
                   code=info["code"])
        return _error(info["reason"], info["code"])

    required_slots = int(info["required_slots"])
    owner = booking.user
    owner_type = booking_input_user_type(booking)
    is_external = UserType.is_external_user(owner_type)

    base_qs = DailySlot.objects.filter(
        slot_master__equipment=target,
        start_datetime__lt=end_time,
        end_datetime__gt=start_time,
        booking__isnull=True,
    ).order_by("start_datetime")
    if staff_endpoint:
        slot_dates = list(base_qs.values_list("date", flat=True).distinct())
        holiday_dates = [d for d in slot_dates if Holiday.is_holiday(d)[0]]
        allowed_status_q = Q(status=SlotStatus.AVAILABLE) | (Q(status=SlotStatus.BLOCKED) & Q(date__in=holiday_dates))
        candidates = list(base_qs.filter(allowed_status_q))
    else:
        allowed_status_q = Q(status=SlotStatus.AVAILABLE)
        qs = av.filter_queryset_for_home_department(
            base_qs.filter(allowed_status_q), user=owner, equipment=target, is_admin=False, is_external=is_external
        )
        candidates = list(qs)
        checker = (
            SlotAvailabilityChecker.is_slot_available_for_external
            if is_external
            else SlotAvailabilityChecker.is_slot_available
        )
        if any(not checker(s) for s in candidates):
            return _error(SLOT_TAKEN_MESSAGE, "SLOT_UNAVAILABLE")

    if len(candidates) != required_slots:
        return _error(
            f"{target.name} needs {required_slots} consecutive slot(s) for this booking. "
            "Please select a matching time range.",
            "SLOT_COUNT_MISMATCH",
            required_slots=required_slots,
        )
    for prev, cur in zip(candidates, candidates[1:]):
        if prev.end_datetime != cur.start_datetime:
            return _error("The selected slots must be consecutive.", "SLOTS_NOT_CONSECUTIVE")
    if not bypasses_multimode_restrictions(request.user):
        for s in candidates:
            at_time = timezone.localtime(s.start_datetime).time() if s.start_datetime else None
            ok, err = equipment_bookable_on_date(target, s.date, at_time)
            if not ok:
                return _error(err or "This equipment is not bookable at the selected time.", "SLOT_UNAVAILABLE")

    if not staff_endpoint:
        try:
            quota_date = start_time
            if booking.status == BookingStatus.DISRUPTION_PENDING and getattr(booking, "quota_period_anchor_at", None):
                quota_date = booking.quota_period_anchor_at
            if not av.booking_quota_should_skip(target):
                quota_allowed, quota_error = av.QuotaService.validate_booking_quota(
                    user=owner,
                    equipment=target,
                    additional_time_minutes=int(booking.total_time_minutes or 0),
                    additional_bookings=1,
                    additional_charge=Decimal(str(booking.total_charge or "0")),
                    booking_date=quota_date,
                    exclude_booking_id=booking.booking_id,
                )
                if not quota_allowed:
                    return _error(quota_error, "QUOTA_EXCEEDED")
        except Exception:
            logger.exception("Quota check failed during cross-equipment reschedule for booking %s", booking.pk)
            return _error("Quota check failed. Please try again or contact admin.", "QUOTA_CHECK_FAILED")

    ext_quota = ExternalSlotQuotaService.validate_external_booking(
        owner,
        target,
        slot_dates=[s.date for s in candidates],
        slots_requested=len(candidates),
        exclude_booking_id=booking.booking_id,
        bypass=False,
    )
    if not ext_quota.allowed:
        return Response(ext_quota.as_error_payload(), status=status.HTTP_400_BAD_REQUEST)

    slot_ids = [s.id for s in candidates]
    released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    previous_start = booking.daily_slots.order_by("start_datetime").values_list("start_datetime", flat=True).first()
    previous_end = booking.daily_slots.order_by("-end_datetime").values_list("end_datetime", flat=True).first()
    previous_input_values = dict(booking.input_values or {})
    previous_charge_profile_id = booking.charge_profile_id
    previous_total_charge = booking.total_charge
    reschedulable = (BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING)

    class _SlotTaken(Exception):
        pass

    class _BookingChanged(Exception):
        pass

    try:
        with transaction.atomic():
            # Booking row first (same order as cancellation), then target slots.
            current = Booking.objects.select_for_update().get(pk=booking.pk)
            if (
                current.status not in reschedulable
                or current.equipment_id != source.pk
                or current.total_charge != previous_total_charge
                or dict(current.input_values or {}) != previous_input_values
                or set(current.daily_slots.values_list("id", flat=True)) != set(released_slot_ids)
            ):
                raise _BookingChanged()
            booking = current
            locked = list(
                DailySlot.objects.select_for_update()
                .filter(id__in=slot_ids, booking__isnull=True)
                .filter(allowed_status_q)
            )
            if len(locked) != required_slots:
                raise _SlotTaken()
            free_status = (
                av.effective_slot_status_when_freeing_disruption_booking(booking)
                if (
                    booking.status == BookingStatus.DISRUPTION_PENDING
                    or getattr(booking, "maintenance_disruption_flag", False)
                )
                else av.released_slot_status_after_booking_freed(source)
            )
            booking.daily_slots.all().update(booking=None, status=free_status)
            DailySlot.objects.filter(id__in=slot_ids).update(booking=booking, status=SlotStatus.BOOKED)

            previous_status = booking.status
            booking.equipment = target
            if info.get("charge_profile_id"):
                booking.charge_profile_id = info["charge_profile_id"]
            booking.input_values = info["input_values"]
            booking.status = BookingStatus.BOOKED
            if getattr(booking, "maintenance_disruption_flag", False) or previous_status == BookingStatus.DISRUPTION_PENDING:
                av.clear_disruption_policy_fields(booking)
            booking.save()

            av.create_booking_event(
                booking=booking,
                event_type=BookingEventType.RESCHEDULED,
                previous_status=previous_status,
                new_status=booking.status,
                comment=(
                    f"Booking moved from {source.name} to {target.name} and rescheduled to "
                    f"{timezone.localtime(start_time).strftime('%Y-%m-%d %H:%M')} - "
                    f"{timezone.localtime(end_time).strftime('%Y-%m-%d %H:%M')}"
                ),
                created_by=request.user,
                metadata={
                    "cross_equipment": True,
                    "equipment_group_id": source.equipment_group_id,
                    "previous_equipment_id": source.pk,
                    "previous_equipment_code": source.code,
                    "previous_equipment_name": source.name,
                    "new_equipment_id": target.pk,
                    "new_equipment_code": target.code,
                    "new_equipment_name": target.name,
                    "previous_start": previous_start.isoformat() if previous_start else None,
                    "previous_end": previous_end.isoformat() if previous_end else None,
                    "new_start": start_time.isoformat(),
                    "new_end": end_time.isoformat(),
                    "previous_charge_profile_id": previous_charge_profile_id,
                    "new_charge_profile_id": booking.charge_profile_id,
                    "previous_input_values": previous_input_values,
                    "dropped_fields": info.get("dropped_fields") or [],
                    "charged_amount": str(booking.total_charge),
                    "source_charge_reference": info.get("source_charge_reference"),
                    "target_charge_reference": info.get("target_charge_reference"),
                    "target_required_minutes": info.get("required_minutes"),
                },
                send_notification=True,
            )
    except _SlotTaken:
        _log_event("cross_reschedule_slot_taken", booking=booking.pk, target=target.pk)
        return _error(SLOT_TAKEN_MESSAGE, "SLOT_TAKEN", status.HTTP_409_CONFLICT)
    except _BookingChanged:
        _log_event("cross_reschedule_booking_changed", booking=booking.pk, target=target.pk)
        return _error(
            "This booking was changed by another action. Please refresh and try again.",
            "BOOKING_CHANGED",
            status.HTTP_409_CONFLICT,
        )
    except Exception as e:
        logger.exception("Cross-equipment reschedule failed for booking %s", booking.pk)
        return Response({"error": f"Error rescheduling booking: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    _log_event("cross_reschedule_done", booking=booking.pk, source=source.pk, target=target.pk)
    if released_slot_ids:
        try:
            av.notify_waitlist_slots_available(
                source, preferred_slot_ids=released_slot_ids, respect_reschedule_threshold=True
            )
        except Exception:
            logger.warning("Failed waitlist FCFS after cross-equipment reschedule for %s", source.pk, exc_info=True)

    return Response(
        {
            "message": f"Booking moved to {target.name} and rescheduled successfully.",
            "booking": av.BookingSerializer(booking).data,
            "cross_equipment": True,
            "previous_equipment": _equipment_summary(source),
            "new_equipment": _equipment_summary(target),
        },
        status=status.HTTP_200_OK,
    )
