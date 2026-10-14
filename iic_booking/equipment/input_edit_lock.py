"""Who may change booking inputs whose field has 'Editing required' unchecked.

After booking, a field with ``editing_required = False`` may only be changed by the equipment's
Officer in Charge (incl. a currently active temporary OIC), a Department Administrator of the
department that owns the equipment, or the Main Administrator. Everyone else (the booking user,
supervisor, lab staff, finance, OICs / Department Administrators of other equipment) may still edit
the 'Editing required' fields within the usual status limits.
"""

from __future__ import annotations

from typing import Any, Iterable

from iic_booking.users.models.user_type import UserType

LOCKED_INPUT_MESSAGE = (
    "This field can only be changed by the Officer in Charge, Department Administrator or Main Administrator"
)
LOCKED_INPUT_CODE = "input_locked"

ELEMENTS_SUFFIX = "_elements"
SAMPLE_SETS_KEY = "_sample_sets"


def can_edit_locked_inputs(user, equipment) -> bool:
    """True for the equipment's OIC (incl. an active temporary OIC), a Department Administrator of the
    equipment's department, and the Main Administrator."""
    if not user or not getattr(user, "is_authenticated", False) or equipment is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = getattr(user, "user_type", None)
    if user_type == UserType.ADMIN:
        return True
    if user_type == UserType.DEPT_ADMIN:
        department_id = getattr(user, "department_id", None)
        return bool(department_id) and getattr(equipment, "internal_department_id", None) == department_id
    if user_type == UserType.MANAGER:
        from .reports import get_equipment_ids_managed_by_oic

        equipment_id = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
        return equipment_id is not None and equipment_id in get_equipment_ids_managed_by_oic(user.id)
    return False


def locked_input_keys(booking) -> set[str]:
    """Field keys of the booking's equipment with 'Editing required' unchecked.

    Uses the booking's field rows (typed for its user type, else the shared rows); a key that only
    exists in another user type's rows is locked unless one of those rows has 'Editing required'.
    """
    from .models import DynamicInputField
    from .sample_set_limits import booking_field_user_type

    rows = list(
        DynamicInputField.objects.filter(equipment_id=booking.equipment_id).values_list(
            "field_key", "user_type", "editing_required"
        )
    )
    user_type = booking_field_user_type(booking)
    effective_type = user_type if user_type and any(t == user_type for _k, t, _e in rows) else ""
    effective: dict[str, bool] = {}
    others: dict[str, bool] = {}
    for key, row_type, editable in rows:
        if not key:
            continue
        if (row_type or "") == effective_type:
            effective[key] = bool(editable)
        else:
            others[key] = others.get(key, False) or bool(editable)
    locked = {k for k, editable in effective.items() if not editable}
    locked |= {k for k, editable in others.items() if k not in effective and not editable}
    return locked


def _normalized(value: Any) -> Any:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _same(a: Any, b: Any) -> bool:
    return _normalized(a) == _normalized(b)


def _base_key(key: str) -> str:
    return key[: -len(ELEMENTS_SUFFIX)] if key.endswith(ELEMENTS_SUFFIX) else key


def _changed_keys(raw: dict, original: dict, final: dict, locked: set[str], *, require_raw: bool) -> set[str]:
    """Locked field keys whose stored value changes. With ``require_raw`` a key counts only when it was
    submitted with a different value, so values the server recomputes on its own never block an edit."""
    changed = set()
    for key in set(original) | set(final):
        if _base_key(str(key)) not in locked or _same(original.get(key), final.get(key)):
            continue
        if require_raw and (key not in raw or _same(raw.get(key), original.get(key))):
            continue
        changed.add(_base_key(str(key)))
    return changed


def locked_input_changes(
    booking, original: dict, final: dict, raw: dict, raw_sample_sets: Iterable | None
) -> list[str]:
    """Locked field keys (sorted) the edit would change, in the primary inputs or any sample set."""
    locked = locked_input_keys(booking)
    if not locked:
        return []
    original = original or {}
    final = final or {}
    primary_original = {k: v for k, v in original.items() if k != SAMPLE_SETS_KEY}
    primary_final = {k: v for k, v in final.items() if k != SAMPLE_SETS_KEY}
    changed = _changed_keys(raw or {}, primary_original, primary_final, locked, require_raw=True)

    original_sets = original.get(SAMPLE_SETS_KEY) if isinstance(original.get(SAMPLE_SETS_KEY), list) else []
    final_sets = final.get(SAMPLE_SETS_KEY) if isinstance(final.get(SAMPLE_SETS_KEY), list) else []
    if isinstance(raw_sample_sets, str):
        import json

        try:
            raw_sample_sets = json.loads(raw_sample_sets)
        except ValueError:
            raw_sample_sets = None
    raw_sets = list(raw_sample_sets) if isinstance(raw_sample_sets, (list, tuple)) else None
    for index in range(max(len(original_sets), len(final_sets))):
        before = original_sets[index] if index < len(original_sets) and isinstance(original_sets[index], dict) else {}
        after = final_sets[index] if index < len(final_sets) and isinstance(final_sets[index], dict) else {}
        if raw_sets is None:
            changed |= _changed_keys({}, before, after, locked, require_raw=False)
            continue
        submitted = raw_sets[index] if index < len(raw_sets) and isinstance(raw_sets[index], dict) else {}
        submitted = {**{k: None for k in before}, **submitted}
        changed |= _changed_keys(submitted, before, after, locked, require_raw=True)
    return sorted(changed)
