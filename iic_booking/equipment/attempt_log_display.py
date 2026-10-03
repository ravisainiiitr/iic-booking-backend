"""
Booking Attempt Log details as the Officer in Charge reads them: the booker's details, the requested slots,
the user inputs resolved against the equipment's fields, and a plain-language outcome.

Older log entries stored inputs keyed by the field label of the time (``additional_info.input_values``);
newer ones also keep the field keys (``input_values_by_key``). Labels are resolved at read time, so a field
renamed or deleted since the attempt falls back to a readable key and its value is never shown as raw JSON.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from iic_booking.users.display import get_user_display_name

from .failure_reasons import explain
from .input_display import (
    COMMENTS_KEY,
    ELEMENTS_SUFFIX,
    SAMPLE_SETS_KEY,
    all_equipment_field_items,
    clean_label,
    equipment_field_items,
    humanize_key,
)


def _parse(value: Any) -> Any:
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _legacy_field(key: str, value: Any) -> dict:
    """Display definition for a logged value whose field no longer exists (or was renamed)."""
    label = humanize_key(key)
    if str(key).endswith(ELEMENTS_SUFFIX):
        label = f"{humanize_key(str(key)[: -len(ELEMENTS_SUFFIX)])} elements"
    if isinstance(value, bool):
        return {"field_key": key, "field_label": label, "field_type": "TOGGLE"}
    if isinstance(value, list) and value and all(isinstance(r, (list, tuple)) for r in value):
        return {"field_key": key, "field_label": label, "field_type": "TABLE", "options": []}
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        columns: list[str] = []
        for row in value:
            for k in row:
                if k not in columns:
                    columns.append(k)
        return {
            "field_key": key,
            "field_label": label,
            "field_type": "TYPED_TABLE",
            "table_config": {
                "columns": [{"key": c, "label": humanize_key(c), "type": "TEXT"} for c in columns],
                "rows": {"mode": "USER", "serial_column": True},
            },
        }
    return {"field_key": key, "field_label": label, "field_type": "TEXT"}


def resolve_logged_inputs(equipment_id: Any, user_type: str, additional_info: Any, *, parse_json: bool = True) -> dict:
    """
    {"input_fields": [...], "input_values": {key: value, "_sample_sets": [...]}, "comments": str} for a log entry.
    ``input_fields`` follow the equipment's field order (the user's type, else the shared fields), then any
    logged value whose field is gone, with a readable label and a table shape for table values.
    ``parse_json=False`` keeps values exactly as submitted (table JSON strings), e.g. for prefilling a new booking.
    """
    info = additional_info if isinstance(additional_info, dict) else {}
    all_items = all_equipment_field_items(equipment_id)
    by_key: dict[str, dict] = {}
    for item in all_items:
        by_key.setdefault(item["field_key"], item)
    label_to_key: dict[str, str] = {}
    for item in all_items:
        label_to_key.setdefault(clean_label(item.get("field_label")).lower(), item["field_key"])

    keyed = info.get("input_values_by_key")
    stored = keyed if isinstance(keyed, dict) else info.get("input_values")
    stored = stored if isinstance(stored, dict) else {}

    values: dict[str, Any] = {}
    legacy: list[str] = []
    for raw_key, raw_value in stored.items():
        key = str(raw_key)
        value = _parse(raw_value) if parse_json or key == SAMPLE_SETS_KEY else raw_value
        if key in by_key or key in (COMMENTS_KEY, SAMPLE_SETS_KEY):
            target = key
        elif key.endswith(ELEMENTS_SUFFIX) and key[: -len(ELEMENTS_SUFFIX)] in by_key:
            target = key
        elif clean_label(key).lower() in label_to_key:
            target = label_to_key[clean_label(key).lower()]
        else:
            target = key
            legacy.append(key)
        if target in values and target not in legacy:
            continue
        values[target] = value

    if isinstance(values.get(SAMPLE_SETS_KEY), list):
        values[SAMPLE_SETS_KEY] = [
            {k: (_parse(v) if parse_json else v) for k, v in s.items()}
            for s in values[SAMPLE_SETS_KEY] if isinstance(s, dict)
        ]
    else:
        values.pop(SAMPLE_SETS_KEY, None)

    fields = list(equipment_field_items(equipment_id, user_type or ""))
    shown = {f["field_key"] for f in fields}
    used_keys = set(values)
    for s in values.get(SAMPLE_SETS_KEY) or []:
        used_keys.update(s)
    for key in sorted(used_keys):
        if key in shown or key in legacy or key in (COMMENTS_KEY, SAMPLE_SETS_KEY):
            continue
        if key in by_key:
            fields.append(by_key[key])
            shown.add(key)
    fields.sort(key=lambda f: f["field_key"])
    for key in legacy:
        if key.endswith(ELEMENTS_SUFFIX) and any(
            f["field_key"] == key[: -len(ELEMENTS_SUFFIX)] and str(f.get("field_type")).upper() == "PERIODIC_TABLE"
            for f in fields
        ):
            continue
        fields.append(_legacy_field(key, _parse(values[key])))

    comments = values.pop(COMMENTS_KEY, "")
    return {
        "input_fields": fields,
        "input_values": values,
        "comments": str(comments).strip() if comments not in (None, "") else "",
    }


def user_details(user: Any, *, wallet_cache: Optional[dict] = None) -> Optional[dict]:
    """Booker details as shown on the booking details page (no secrets)."""
    if user is None:
        return None
    from .serializers import _get_wallet_owner_display_name

    department = getattr(user, "department", None)
    supervisor = getattr(user, "supervisor", None) if getattr(user, "supervisor_id", None) else None
    try:
        wallet_owner = _get_wallet_owner_display_name(user, wallet_cache if wallet_cache is not None else {})
    except Exception:
        wallet_owner = None
    try:
        type_label = user.get_user_type_display_label() or user.user_type
    except Exception:
        type_label = getattr(user, "user_type", None)
    return {
        "id": user.pk,
        "name": get_user_display_name(user),
        "email": user.email or "",
        "phone": (getattr(user, "phone_number", "") or "").strip() or None,
        "user_type": getattr(user, "user_type", None) or None,
        "user_type_label": type_label or None,
        "department_name": department.name if department else None,
        "department_code": getattr(department, "code", None) if department else None,
        "id_number": (getattr(user, "emp_id", "") or "").strip() or None,
        "designation": (getattr(user, "designation", "") or "").strip() or None,
        "supervisor_name": get_user_display_name(supervisor) if supervisor else None,
        "wallet_owner_name": wallet_owner,
    }


def _slot_item(slot: Any) -> dict:
    master = getattr(slot, "slot_master", None)
    return {
        "id": slot.pk,
        "slot_name": (getattr(master, "slot_name", None) or None) if master else None,
        "date": slot.date.isoformat() if getattr(slot, "date", None) else None,
        "start_datetime": slot.start_datetime.isoformat() if slot.start_datetime else None,
        "end_datetime": slot.end_datetime.isoformat() if slot.end_datetime else None,
    }


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def requested_slots(info: dict) -> list[dict]:
    """Slots the user picked (from the logged slot IDs, else the requested time range)."""
    from .models import DailySlot

    slot_ids = _int_list(info.get("slot_ids"))
    if slot_ids:
        slots = DailySlot.objects.filter(id__in=slot_ids).select_related("slot_master").order_by("start_datetime")
        return [_slot_item(s) for s in slots]
    start, end = info.get("start_time"), info.get("end_time")
    if start and end:
        return [{"id": None, "slot_name": None, "date": None, "start_datetime": str(start), "end_datetime": str(end)}]
    return []


def selected_parameter_names(equipment_id: Any, codes: Any) -> list[str]:
    """Names of the chosen MULTI_PARAM parameters (codes kept for parameters that no longer exist)."""
    from .models import MultiParamDefinition

    if isinstance(codes, dict):
        codes = [k for k, v in codes.items() if v]
    if not isinstance(codes, list):
        return []
    wanted = [str(c).strip() for c in codes if isinstance(c, (str, int)) and str(c).strip()]
    if not wanted:
        return []
    names = dict(
        MultiParamDefinition.objects.filter(equipment_id=equipment_id, param_code__in=wanted)
        .values_list("param_code", "param_name")
    )
    return [names.get(code) or code for code in wanted]


def _display_booking_id(booking: Any, equipment: Any, booking_id: Any) -> Optional[str]:
    vbid = (getattr(booking, "virtual_booking_id", "") or "").strip() if booking is not None else ""
    if vbid:
        return vbid
    if booking_id is None:
        return None
    code = getattr(equipment, "code", "") if equipment is not None else ""
    return f"{code}-{booking_id}" if code else str(booking_id)


def attempt_detail(log: Any) -> dict:
    """Everything the attempt details dialog shows, for one BookingAttemptLog row."""
    from django.contrib.auth import get_user_model

    from .models import Booking, BookingEvent, BookingEventType, Equipment

    info = log.additional_info if isinstance(log.additional_info, dict) else {}
    equipment = log.equipment
    attempt_user = log.user
    booked_for = None
    booked_for_id = info.get("booked_for_user_id")
    if booked_for_id not in (None, "") and str(booked_for_id) != str(log.user_id):
        booked_for = get_user_model().objects.select_related("department", "supervisor").filter(pk=booked_for_id).first()
    subject = booked_for or attempt_user

    booking = None
    if log.booking_id is not None:
        booking = (
            Booking.objects.select_related("user", "user__department", "user__supervisor")
            .filter(booking_id=log.booking_id)
            .first()
        )
        if booking is not None and booking.user_id and booking.user_id != subject.pk:
            booked_for = booking.user
            subject = booking.user

    user_type = (
        (getattr(booking, "user_type_snapshot", None) or "").strip() if booking is not None else ""
    ) or (getattr(subject, "user_type", "") or "")
    inputs = resolve_logged_inputs(log.equipment_id, user_type, info)

    picked = requested_slots(info)
    booked_slots: list[dict] = []
    notes: list[str] = []
    if booking is not None:
        booked_slots = [_slot_item(s) for s in booking.daily_slots.select_related("slot_master").order_by("start_datetime")]
        picked_ids = {s["id"] for s in picked if s.get("id")}
        booked_ids = {s["id"] for s in booked_slots if s.get("id")}
        if picked_ids and booked_ids and picked_ids != booked_ids:
            if len(booked_ids) < len(picked_ids):
                notes.append("Only one slot was free, so the request was reduced to a single slot.")
            else:
                notes.append("The selected slots were taken, so other free slots of the same length were booked.")
        if (booking.status or "").upper() == "WAITLISTED":
            notes.append("The booking was placed on the waitlist.")
        created = (
            BookingEvent.objects.filter(booking_id=booking.booking_id, event_type=BookingEventType.CREATED)
            .order_by("created_at")
            .values_list("metadata", flat=True)
            .first()
        )
        if isinstance(created, dict) and created.get("equipment_group_alternative"):
            source = created.get("alternative_of_equipment_name") or created.get("alternative_of_equipment_code")
            notes.append(
                f"Booked on this equipment as an alternative to {source}." if source
                else "Booked on this equipment as an alternative in its equipment group."
            )
    if not any("alternative to" in n for n in notes) and info.get("alternative_of_equipment_id"):
        source = Equipment.objects.filter(pk=info.get("alternative_of_equipment_id")).values_list("name", flat=True).first()
        if source:
            notes.append(f"Requested as an alternative to {source}.")

    display_id = _display_booking_id(booking, equipment, log.booking_id)
    success = str(log.outcome).upper() == "SUCCESS"
    if success:
        outcome = {
            "status": "SUCCESS",
            "title": f"Booking created: {display_id}" if display_id else "Booking created",
            "message": (
                f"The booking {display_id} was created"
                + (f" (status: {booking.get_status_display()})." if booking is not None else ".")
            ) if display_id else "The booking was created.",
            "notes": notes,
            "technical": "",
            "code": "success",
        }
    else:
        friendly = explain(log.failure_reason, outcome=log.outcome)
        outcome = {
            "status": "FAILED",
            "title": friendly["title"],
            "message": friendly["message"],
            "notes": notes,
            "technical": (log.failure_reason or "").strip(),
            "code": friendly["code"],
        }

    wallet_cache: dict = {}
    return {
        "id": log.id,
        "requested_at": log.requested_at.isoformat() if log.requested_at else None,
        "outcome": log.outcome,
        "equipment_id": log.equipment_id,
        "equipment_code": getattr(equipment, "code", "") or "",
        "equipment_name": getattr(equipment, "name", "") or "",
        "real_booking_id": log.booking_id,
        "display_booking_id": display_id,
        "number_of_samples": log.number_of_samples,
        "slots_requested": log.slots_requested,
        "duration_minutes": log.duration_minutes,
        "user": user_details(subject, wallet_cache=wallet_cache),
        "requested_by": user_details(attempt_user, wallet_cache=wallet_cache) if booked_for is not None else None,
        "requested_slots": picked,
        "booked_slots": booked_slots,
        "input_fields": inputs["input_fields"],
        "input_values": inputs["input_values"],
        "comments": inputs["comments"],
        "selected_parameters": selected_parameter_names(log.equipment_id, info.get("selected_parameters")),
        "outcome_details": outcome,
    }
