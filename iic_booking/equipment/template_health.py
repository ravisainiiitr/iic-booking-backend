"""Would a saved booking template book cleanly right now? Read-only checks that reuse the booking rules.

Each issue has a severity:

* ``error``: booking with the template fails (or cannot start) until it is fixed, e.g. a value over the
  equipment maximum, a required input left empty, more time than the weekly limit ever allows.
* ``warning``: the booking goes ahead but not as saved (sample sets dropped, preferred slot not
  pre-selected) or is likely to fail today (not enough quota left next week, wallet balance too low).
* ``info``: something the booking page quietly leaves out (a removed input, an option no longer offered).

Nothing here writes to the database; results are cached briefly per template version so the booking
page can ask for them on every load, including the 9 pm rush.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import DynamicInputField, DynamicInputFieldType, EquipmentProfileType, EquipmentStatus, Holiday, SlotMaster

logger = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"
INFO = "info"

# Errors the user cannot fix by editing the template; the dashboard notice leaves them out.
UNFIXABLE_CODES = frozenset({"equipment_not_operational", "not_allowed", "no_charge_profile"})
CACHE_SECONDS = 900
_CACHE_VERSION = 1
_CHOICE_TYPES = (DynamicInputFieldType.RADIO, DynamicInputFieldType.COMBO, DynamicInputFieldType.MULTI_SELECT)
_IGNORED_KEYS = ("comments",)
_MAX_CHOICES_IN_MESSAGE = 6
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _issue(code, severity, message, *, field=None, sample_set=None, **extra):
    item = {"code": code, "severity": severity, "message": message, "field": field, "set": sample_set}
    item.update(extra)
    return item


def _pretty_number(value):
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


def format_minutes(minutes) -> str:
    minutes = max(0, int(round(minutes or 0)))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    text = f"{hours} h"
    return f"{text} {rest} min" if rest else text


def _is_staff(user) -> bool:
    return getattr(user, "user_type", None) in UserType.get_admin_panel_codes()


def _is_external(user) -> bool:
    return UserType.is_external_user(getattr(user, "user_type", None) or "")


def _peak_paused(user) -> bool:
    from .peak_window import get_peak_settings, is_peak_blockable_user

    try:
        settings = get_peak_settings()
        return bool(settings.enabled and settings.block_external_users and is_peak_blockable_user(user))
    except Exception:
        return False


# --- input fields ---------------------------------------------------------------------------------


def shown_fields(equipment, user_type) -> dict:
    """{field_key: DynamicInputField} the booking form shows: rows typed for the user type, else shared rows."""
    rows = list(DynamicInputField.objects.filter(equipment=equipment).order_by("field_key", "pk"))
    typed = [f for f in rows if user_type and f.user_type == user_type]
    chosen = typed or [f for f in rows if not f.user_type]
    out = {}
    for f in chosen:
        out.setdefault(f.field_key, f)
    return out


def _choices(field) -> list[str]:
    """Values and labels of a choice field's options, as the booking page compares them."""
    options = field.options if isinstance(field.options, list) else []
    out = []
    for index, option in enumerate(options):
        if isinstance(option, dict):
            value = option.get("value", option.get("label", option.get("id", option.get("name"))))
            label = option.get("label", option.get("value", option.get("name", option.get("id"))))
            value = str(value) if isinstance(value, (str, int, float, bool)) else str(index + 1)
            label = str(label) if isinstance(label, (str, int, float, bool)) else value
            out.extend([value, label])
        elif option is None:
            out.append(str(index + 1))
        else:
            out.append(str(option))
    return out


def _choice_labels(field) -> list[str]:
    labels = []
    options = field.options if isinstance(field.options, list) else []
    for option in options:
        if isinstance(option, dict):
            label = option.get("label", option.get("value"))
            if isinstance(label, (str, int, float)):
                labels.append(str(label))
        elif isinstance(option, (str, int, float)):
            labels.append(str(option))
    return labels


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    if isinstance(value, bool):
        return False
    return str(value).strip() == ""


def _field_issues(fields, base, *, is_print_3d):
    """Removed inputs, choices no longer offered and required inputs left empty in sample set 1."""
    issues = []
    present = dict(base)
    for key, value in base.items():
        if key.startswith("_") or key in _IGNORED_KEYS or _is_blank(value):
            continue
        if key.endswith("_elements"):
            parent = fields.get(key[: -len("_elements")])
            if parent is None or parent.field_type != DynamicInputFieldType.PERIODIC_TABLE:
                issues.append(_issue(
                    "field_removed", INFO, "Saved element choices are no longer asked for and will be left out.",
                    field=key,
                ))
            continue
        field = fields.get(key)
        if field is None:
            preview = "" if isinstance(value, (list, dict)) else str(value).strip()
            if len(preview) > 40:
                preview = preview[:39].rstrip() + "…"
            issues.append(_issue(
                "field_removed", INFO,
                f"A saved input{f' (value “{preview}”)' if preview else ''} is no longer on this equipment's form "
                f"and will be left out.",
                field=key,
            ))
            present.pop(key, None)
            continue
        if field.field_type not in _CHOICE_TYPES:
            continue
        choices = _choices(field)
        if not choices:
            continue
        label = field.field_label or key
        offered = ", ".join(_choice_labels(field)[:_MAX_CHOICES_IN_MESSAGE])
        if field.field_type == DynamicInputFieldType.MULTI_SELECT:
            picked = [str(v).strip() for v in (value if isinstance(value, list) else str(value).split(",")) if str(v).strip()]
            gone = [v for v in picked if v not in choices]
            if gone:
                kept = len(picked) - len(gone)
                if not kept:
                    present.pop(key, None)
                issues.append(_issue(
                    "option_invalid", WARNING,
                    f"{label}: {', '.join(gone)} {'is' if len(gone) == 1 else 'are'} no longer offered and will be "
                    f"removed." + (f" Available: {offered}." if offered else ""),
                    field=key,
                ))
        elif str(value) not in choices:
            present.pop(key, None)
            issues.append(_issue(
                "option_invalid", WARNING,
                f"{label} is set to “{value}”, which is no longer offered." + (f" Choose one of: {offered}." if offered else ""),
                field=key,
            ))

    for key, field in fields.items():
        if not field.is_required or field.field_type in (DynamicInputFieldType.TOGGLE, DynamicInputFieldType.ICPMS_STANDARD_COVERAGE):
            continue
        if is_print_3d and key in ("A", "B", "C"):
            continue
        if not _is_blank(present.get(key)) or not _is_blank(field.default_value):
            continue
        issues.append(_issue(
            "required_missing", ERROR, f"{field.field_label or key} is required — fill it in.", field=key,
        ))
    return issues


# --- numeric limits and sample sets ------------------------------------------------------------------


def _numeric_issues(equipment, user, base, sets, fields, *, is_print_3d):
    """Values the booking would reject (min, fixed max, formula max), per sample set, for inputs the form sends."""
    from .api_views import _numeric_limit_problems

    issues = []
    for index, group in enumerate([base, *sets], start=1):
        seen = set()
        prefix = f"Sample set {index}: " if index > 1 else ""
        for problem in _numeric_limit_problems(equipment, group, user):
            marker = (problem["key"], problem["kind"])
            if marker in seen or problem["key"] not in fields or (is_print_3d and problem["key"] in ("A", "B", "C")):
                continue
            seen.add(marker)
            kind = problem["kind"]
            limit = problem["limit"]
            value = _pretty_number(problem["value"])
            if kind == "min":
                text = f"{problem['label']} is {value}; the minimum is {limit}."
            else:
                text = f"{problem['label']} is {value}; max {limit} allowed."
            issues.append(_issue(
                f"numeric_{kind}", ERROR, prefix + text, field=problem["key"], sample_set=index,
                label=problem["label"], limit=limit, value=value, detail=problem["message"], fix="clamp",
            ))
    return issues


_TABLE_INCOMPLETE_KINDS = frozenset({"required", "min_rows", "row_count"})


def _typed_table_issues(equipment, user, base, sets, fields, existing):
    """Advanced-table rows the booking would reject, one issue per table and sample set.

    ``table_invalid`` (a cell out of range, an option no longer offered, too many rows) blocks saving the
    template like a numeric limit; ``table_incomplete`` (required cells, row count) only needs filling in.
    """
    from .calculators import SAMPLE_SETS_KEY
    from .typed_table import iter_typed_table_problems

    table_fields = [
        f for f in fields.values()
        if f.field_type == DynamicInputFieldType.TYPED_TABLE and isinstance(f.table_config, dict)
        and f.table_config.get("columns")
    ]
    if not table_fields:
        return []
    already_required = {(i["field"], 1) for i in existing if i["code"] == "required_missing"}
    values = {**base, SAMPLE_SETS_KEY: list(sets)} if sets else dict(base)
    issues = []
    seen = set()
    for problem in iter_typed_table_problems(
        equipment, values, user_type=str(getattr(user, "user_type", "") or ""), fields=table_fields
    ):
        incomplete = problem["kind"] in _TABLE_INCOMPLETE_KINDS
        marker = (problem["key"], problem["set"], incomplete)
        if marker in seen or (incomplete and (problem["key"], problem["set"]) in already_required):
            continue
        seen.add(marker)
        issues.append(_issue(
            "table_incomplete" if incomplete else "table_invalid", ERROR, problem["message"],
            field=problem["key"], sample_set=problem["set"], label=problem["label"], limit=problem.get("limit"),
            row=problem.get("row"), column=problem.get("column"), kind=problem["kind"],
        ))
    return issues


def _sample_set_issues(equipment, user, values, sets):
    from .calculators import MAX_SAMPLE_SETS
    from .sample_set_limits import combined_max_error, sample_sets_allowed

    issues = []
    if sets and not sample_sets_allowed(equipment):
        issues.append(_issue(
            "sample_sets_disabled", WARNING,
            f"This equipment no longer accepts samples with different parameters, so only sample set 1 will be "
            f"booked ({len(sets)} extra set{'' if len(sets) == 1 else 's'} left out).",
        ))
        return issues
    if len(sets) > MAX_SAMPLE_SETS:
        issues.append(_issue(
            "too_many_sample_sets", ERROR,
            f"At most {MAX_SAMPLE_SETS + 1} sample sets are allowed in one booking; this template has {len(sets) + 1}.",
        ))
    error = combined_max_error(equipment, values, booking_user=user)
    if error:
        issues.append(_issue("combined_max", ERROR, error))
    return issues


# --- time, charge, slots -----------------------------------------------------------------------------


def _clean_group(equipment, group):
    from .api_views import _clean_single_input_value
    from .calculators import normalize_periodic_table_billable_counts

    cleaned = {}
    for key, value in (group or {}).items():
        if str(key).startswith("_"):
            continue
        value = _clean_single_input_value(value)
        if value is None or value == []:
            continue
        cleaned[key] = value
    return normalize_periodic_table_billable_counts(equipment, cleaned)


def estimate(equipment, user, base, sets):
    """(analysis_minutes, charge_profile_found, charge) the booking page would work out, or raise."""
    from .api_views import _get_charge_profile_pricing_profile_for_user, get_external_gst_percent
    from .calculators import (
        SAMPLE_SETS_KEY,
        ChargeCalculationEngine,
        TimeCalculationEngine,
        build_safe_input_values_for_charge_calculation,
        quantize_money,
    )
    from .models import ChargeProfile
    from .pi_pricing import get_active_charge_profile
    from .slot_allocation import slots_needed_for_equipment

    user_type = getattr(user, "user_type", None) or UserType.STUDENT
    try:
        profile = get_active_charge_profile(
            equipment, user_type, _get_charge_profile_pricing_profile_for_user(user, equipment), user
        )
    except ChargeProfile.DoesNotExist:
        return None
    values = _clean_group(equipment, base)
    extra = [c for c in (_clean_group(equipment, s) for s in sets) if c]
    if extra:
        values[SAMPLE_SETS_KEY] = extra
    safe = build_safe_input_values_for_charge_calculation(values, equipment=equipment)
    analysis = int(TimeCalculationEngine.calculate_time(
        profile, safe, slot_duration_minutes=equipment.slot_duration_minutes
    ) or 0)
    slots = slots_needed_for_equipment(equipment, analysis)
    booked = slots * int(equipment.slot_duration_minutes or 0)
    charge, _breakdown = ChargeCalculationEngine.calculate_charge(profile, safe, booked or analysis, selected_parameters=None)
    charge = quantize_money(charge)
    if _is_external(user):
        gst = get_external_gst_percent()
        if gst > 0:
            charge = quantize_money(charge + quantize_money(charge * gst / Decimal("100")))
    return {"analysis_minutes": analysis, "slots": slots, "booked_minutes": booked, "charge": charge}


def weekly_slot_rows(equipment, user):
    """[(start_minute, end_minute)] of one day's slots, as the booking page lists them for ``user``."""
    rows = {}
    duration = int(equipment.slot_duration_minutes or 60)
    for open_t, close_t in SlotMaster.objects.filter(equipment=equipment, is_active=True).values_list("open_time", "close_time"):
        if open_t is None:
            continue
        start = open_t.hour * 60 + open_t.minute
        end = close_t.hour * 60 + close_t.minute if close_t is not None else start + duration
        if end <= start:
            end += 1440
        rows.setdefault(start, end)
    spans = sorted(rows.items())
    if not _is_external(user):
        lo, hi = getattr(equipment, "weekly_view_time_from", None), getattr(equipment, "weekly_view_time_to", None)
        lo = lo.hour * 60 + lo.minute if lo else None
        hi = hi.hour * 60 + hi.minute if hi else None
        spans = [(s, e) for s, e in spans if (lo is None or s >= lo) and (hi is None or e % 1440 <= hi)]
    return spans


def _next_week_monday(now=None) -> date:
    today = timezone.localtime(now or timezone.now()).date()
    return today - timedelta(days=today.weekday()) + timedelta(days=7)


def _preferred_slot_issues(equipment, user, preferred, slots_needed, now=None):
    from .mode_utils import equipment_bookable_on_date

    issues = []
    weekday = preferred["weekday"]
    start = preferred["start_time"]
    label = f"{WEEKDAY_NAMES[weekday]} {start.strftime('%H:%M')}"
    if weekday >= 5:
        issues.append(_issue(
            "preferred_weekend", WARNING,
            f"Your preferred slot is on a {WEEKDAY_NAMES[weekday]}; weekends are not bookable, so it will not be "
            "pre-selected. Choose a weekday slot.",
            field="preferred_slot",
        ))
        return issues
    rows = weekly_slot_rows(equipment, user)
    if rows:
        starts = [s for s, _e in rows]
        minute = start.hour * 60 + start.minute
        if minute not in starts:
            issues.append(_issue(
                "preferred_slot_missing", WARNING,
                f"Your preferred slot ({label}) no longer matches this equipment's slot timings, so it will not be "
                "pre-selected. Choose it again in the template.",
                field="preferred_slot",
            ))
            return issues
        if slots_needed:
            left = len(starts) - starts.index(minute)
            if slots_needed > left:
                issues.append(_issue(
                    "preferred_slot_too_short", WARNING,
                    f"Your sample details need {slots_needed} slots but only {left} "
                    f"{'is' if left == 1 else 'are'} left that day from {start.strftime('%H:%M')}. Pick an earlier "
                    "start, reduce the samples, or pick slots when booking.",
                    field="preferred_slot", needed=slots_needed, available=left,
                ))
    target = _next_week_monday(now) + timedelta(days=weekday)
    holiday = Holiday.objects.filter(date=target, is_active=True).values_list("reason", flat=True).first()
    if holiday is not None:
        reason = f" ({holiday})" if holiday else ""
        issues.append(_issue(
            "preferred_holiday", INFO,
            f"Your preferred day next week, {target.strftime('%a %d %b')}, is a holiday{reason}; choose another slot "
            "for that week.",
            field="preferred_slot",
        ))
    elif not _is_staff(user):
        try:
            ok, _msg = equipment_bookable_on_date(equipment, target, start)
        except Exception:
            ok = True
        if not ok:
            issues.append(_issue(
                "preferred_mode_unavailable", WARNING,
                f"This equipment mode is not scheduled on {target.strftime('%a %d %b')} at {start.strftime('%H:%M')}, "
                "so your preferred slot cannot be booked that week.",
                field="preferred_slot",
            ))
    return issues


# --- quota and wallet ----------------------------------------------------------------------------------


def _quota_issues(equipment, user, booked_minutes, now=None):
    from .booking_quota_summary import build_booking_quota_summary

    if not booked_minutes:
        return []
    week = _next_week_monday(now)
    summary = build_booking_quota_summary(user, equipment, week)
    if not summary.get("applies"):
        return []
    need = format_minutes(booked_minutes)
    issues = []
    for period in summary.get("periods") or []:
        limit = int(period["limit_minutes"])
        if booked_minutes > limit:
            span = "month" if period["period"] == "MONTHLY" else "week"
            shared = " (shared with your supervisor's group)" if period.get("shared") else ""
            issues.append(_issue(
                "quota_over_limit", ERROR,
                f"This template needs {need} of instrument time; your {span}ly limit on this equipment is "
                f"{format_minutes(limit)}{shared}, so you won't be able to book it in one {span}. Reduce the samples "
                "or split them into separate bookings.",
                limit_minutes=limit, needed_minutes=booked_minutes, period=period["period"],
            ))
    if issues:
        return issues
    remaining = summary.get("remaining_minutes")
    binding = summary.get("binding") or {}
    if remaining is not None and booked_minutes > remaining:
        span = "month" if binding.get("period") == "MONTHLY" else "week"
        issues.append(_issue(
            "quota_low", WARNING,
            f"This template needs {need}; you have {format_minutes(remaining)} left of your {span}ly limit for the "
            f"week of {week.strftime('%d %b')}.",
            remaining_minutes=remaining, needed_minutes=booked_minutes, period=binding.get("period"),
        ))
    return issues


def _wallet_issues(equipment, user, charge):
    from iic_booking.users.models import SubWallet

    from .booking_payment_service import compute_booking_payment_split

    if charge is None or charge <= 0 or _is_external(user):
        return []
    wallet = user.get_accessible_wallet()
    if wallet is None:
        return [_issue("no_wallet", WARNING, "You don't have access to a wallet yet, so this booking cannot be paid for.")]
    department = getattr(equipment, "internal_department", None)
    subs = SubWallet.objects.filter(wallet=wallet)
    sub = subs.filter(department=department).first() if department else subs.filter(department__name="General").first()
    if sub is None:
        insufficient, message = True, None
    else:
        _applied, _due, message = compute_booking_payment_split(sub, charge, user_type=user.user_type, create_as_hold=False)
        if not message:
            return []
        insufficient = message.startswith("Insufficient")
    if insufficient:
        message = (
            f"The estimated charge (₹{charge:,.0f}) is more than your wallet balance for this department. "
            "Recharge before booking opens."
        )
    return [_issue("wallet_low", WARNING, message, charge=str(charge))]


# --- options ---------------------------------------------------------------------------------------------


def _option_issues(equipment, user, options):
    issues = []
    if options.get("atmosphere_sensitive_sample") is True and not getattr(equipment, "atmosphere_sensitive_sample_enabled", False):
        issues.append(_issue(
            "option_not_offered", INFO,
            "Atmosphere-sensitive sample handling is no longer offered on this equipment and will be turned off.",
            field="atmosphere_sensitive_sample",
        ))
    workspace_id = options.get("research_workspace")
    if workspace_id:
        issues.extend(_workspace_issues(user, workspace_id))
    return issues


def _workspace_issues(user, workspace_id):
    try:
        from iic_booking.my_research.models import ResearchWorkspace
    except Exception:
        return []
    try:
        workspace = ResearchWorkspace.objects.filter(pk=workspace_id).first()
    except Exception:
        workspace = None
    usable = (
        workspace is not None
        and not workspace.is_archived
        and (
            workspace.owner_id == user.pk
            or workspace.members.filter(user=user, revoked_at__isnull=True).exists()
        )
    )
    if usable:
        return []
    return [_issue(
        "workspace_unavailable", INFO,
        "The research workspace this template links bookings to is closed or no longer shared with you; "
        "bookings will not be added to it.",
        field="research_workspace",
    )]


# --- entry points ----------------------------------------------------------------------------------------


def _summary(issues, estimate_result):
    severities = {i["severity"] for i in issues}
    status = "needs_attention" if ERROR in severities else "advice" if WARNING in severities else "ok"
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    issues = sorted(issues, key=lambda i: order.get(i["severity"], 3))
    out = {
        "status": status,
        "issues": issues,
        "error_count": sum(1 for i in issues if i["severity"] == ERROR),
        "fixable_error_count": sum(1 for i in issues if i["severity"] == ERROR and i["code"] not in UNFIXABLE_CODES),
        "warning_count": sum(1 for i in issues if i["severity"] == WARNING),
        "analysis_minutes": None,
        "required_slots": None,
        "booked_minutes": None,
        "estimated_charge": None,
        "checked_at": timezone.now().isoformat(),
    }
    if estimate_result:
        out.update(
            analysis_minutes=estimate_result["analysis_minutes"],
            required_slots=estimate_result["slots"],
            booked_minutes=estimate_result["booked_minutes"],
            estimated_charge=str(estimate_result["charge"]),
        )
    return out


def check_values(user, equipment, input_values, options=None, preferred=None, *, now=None, light=False) -> dict:
    """Health of a template's values (saved or a draft from the editor) for ``user`` on ``equipment``.

    ``preferred`` is {weekday, start_time (time), slot_count} or None. ``light`` (used while new slots open)
    keeps only the input checks the booking page acts on and skips time, charge, slot, quota and wallet work.
    """
    from .booking_templates import template_booking_block
    from .calculators import split_sample_sets

    values = input_values if isinstance(input_values, dict) else {}
    options = options if isinstance(options, dict) else {}
    issues = []

    status_value = (getattr(equipment, "status", "") or "").strip()
    if status_value != EquipmentStatus.ACTIVE:
        try:
            label = equipment.get_status_display()
        except Exception:
            label = status_value or "not operational"
        issues.append(_issue(
            "equipment_not_operational", ERROR, f"This equipment is {label} right now, so it cannot be booked.",
        ))
    block = template_booking_block(user, equipment)
    if block:
        issues.append(_issue("not_allowed", ERROR, block))
    elif _peak_paused(user):
        issues.append(_issue(
            "peak_window_paused", INFO,
            "External access pauses for a few minutes while new slots open each week; book once it reopens.",
        ))

    user_type = str(getattr(user, "user_type", "") or "")
    is_print_3d = getattr(equipment, "profile_type", None) == EquipmentProfileType.PRINT_3D
    base, sets = split_sample_sets(values)
    fields = shown_fields(equipment, user_type)
    issues.extend(_field_issues(fields, base, is_print_3d=is_print_3d))
    set_issues = _sample_set_issues(equipment, user, values, sets)
    issues.extend(set_issues)
    if any(i["code"] == "sample_sets_disabled" for i in set_issues):
        sets = []
    issues.extend(_numeric_issues(equipment, user, base, sets, fields, is_print_3d=is_print_3d))
    issues.extend(_typed_table_issues(equipment, user, base, sets, fields, issues))
    if light:
        return {**_summary(issues, None), "light": True}

    estimate_result = None
    blocked_inputs = any(i["code"] in ("required_missing", "too_many_sample_sets") for i in issues)
    is_laser = getattr(equipment, "profile_type", None) == EquipmentProfileType.LASER_CUT_2D
    if not _is_staff(user) and not is_print_3d and not is_laser and not blocked_inputs:
        try:
            estimate_result = estimate(equipment, user, base, sets)
        except Exception:
            logger.warning("template health estimate failed equipment=%s", getattr(equipment, "pk", None), exc_info=True)
            issues.append(_issue(
                "estimate_failed", INFO, "The time and charge for these inputs could not be worked out in advance.",
            ))
        else:
            if estimate_result is None:
                issues.append(_issue(
                    "no_charge_profile", ERROR,
                    "This equipment has no rate for your user category, so it cannot be booked. Contact the lab.",
                ))

    if preferred:
        slots_needed = estimate_result["slots"] if estimate_result else None
        issues.extend(_preferred_slot_issues(equipment, user, preferred, slots_needed, now=now))

    if estimate_result and not _is_staff(user):
        try:
            issues.extend(_quota_issues(equipment, user, estimate_result["booked_minutes"], now=now))
        except Exception:
            logger.warning("template health quota check failed equipment=%s", equipment.pk, exc_info=True)
        try:
            issues.extend(_wallet_issues(equipment, user, estimate_result["charge"]))
        except Exception:
            logger.warning("template health wallet check failed equipment=%s", equipment.pk, exc_info=True)

    issues.extend(_option_issues(equipment, user, options))
    return _summary(issues, estimate_result)


def _preferred_of(template):
    if template.preferred_weekday is None or template.preferred_start_time is None:
        return None
    return {
        "weekday": template.preferred_weekday,
        "start_time": template.preferred_start_time,
        "slot_count": template.preferred_slot_count or 1,
    }


def _cache_key(template, user) -> str:
    stamp = template.updated_at.timestamp() if template.updated_at else 0
    return f"tplhealth:v{_CACHE_VERSION}:{template.pk}:{user.pk}:{stamp}"


def check_template(template, user=None, *, use_cache=True, now=None, light=False) -> dict:
    """Health of a saved template for its owner (cached for CACHE_SECONDS per template version).

    ``light`` returns a cached full result when there is one, else only the input checks.
    """
    user = user or template.user
    key = _cache_key(template, user)
    light_key = f"{key}:light"
    if use_cache:
        cached = cache.get(key)
        if cached is None and light:
            cached = cache.get(light_key)
        if cached is not None:
            return cached
    try:
        result = check_values(
            user, template.equipment, template.input_values, template.options, _preferred_of(template),
            now=now, light=light,
        )
    except Exception:
        logger.exception("template health failed template=%s", template.pk)
        return {"status": "unknown", "issues": [], "error_count": 0, "fixable_error_count": 0, "warning_count": 0}
    if use_cache:
        cache.set(light_key if light else key, result, CACHE_SECONDS)
    return result


def peak_light() -> bool:
    """True while new slots are opening: callers ask for light checks only."""
    try:
        from .peak_window import is_peak_window_active

        return bool(is_peak_window_active())
    except Exception:
        return False


def parse_preferred(raw):
    """Preferred slot from an editor draft ({weekday, start_time "HH:MM", slot_count}) or None."""
    if not isinstance(raw, dict):
        return None
    weekday = raw.get("weekday")
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        return None
    text = str(raw.get("start_time") or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            start = datetime.strptime(text, fmt).time()
            break
        except ValueError:
            continue
    else:
        return None
    count = raw.get("slot_count") or 1
    return {"weekday": weekday, "start_time": time(start.hour, start.minute), "slot_count": count if isinstance(count, int) else 1}
