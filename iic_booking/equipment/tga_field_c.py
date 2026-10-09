"""TGA/DTA [A] / [B] "Samples Details" (field C): one advanced table with a required "Sample Name/Code" first
column for every user type, and stored values converted to it.

Config: the IITR (student / faculty) advanced tables get the new first column; every other user type's C (a
plain TABLE) becomes the same advanced table (label, required, edit and help copied from the student row).

Values (bookings incl. the pre-edit revert snapshot, urgent requests, booking templates; base set and extra
sample sets): advanced-table rows get ``sample_name_code: ""`` in front of their cells. Plain-table values
(lists of lists, saved before the field became an advanced table) are converted only when every cell after
the S.No. is blank; values with data are held back unless ``simple_mapping`` is on, because two old columns
(type of analysis, other requirements) have no place in the new table.
"""

from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from .typed_table import TYPED_TABLE, _clean_cell, normalize_table_config, parse_table_value, validate_table_links

TARGET_CODES = ("TGA/DTA [A]", "TGA/DTA [B]")
IITR_USER_TYPES = ("student", "faculty")
REFERENCE_USER_TYPE = "student"
FIELD_KEY = "C"
NEW_KEY = "sample_name_code"
NEW_COLUMN = {"key": NEW_KEY, "label": "Sample Name/Code", "type": "TEXT", "required": True, "help_text": ""}
EXISTING_KEYS = ["initial_temp_c", "final_temp_c", "rate_c_min", "hold_min", "atmosphere", "flow_rate_ml_min"]

# Old plain-table columns (position -> label) and the proposed new column for each.
LEGACY_COLUMNS = [
    "S.No.",
    "Sample Code",
    "Maximum Temperature (Degree Celsius)",
    "Heating Rate (Degree Celsius/min)",
    "Specify Atmosphere (Air/Nitrogen)",
    "Type of Analysis (TGA/DTA/DSC)",
    "Other requirements",
]
SIMPLE_MAPPING = {1: NEW_KEY, 2: "final_temp_c", 3: "rate_c_min", 4: "atmosphere"}
SIMPLE_UNMAPPED = {5: LEGACY_COLUMNS[5], 6: LEGACY_COLUMNS[6]}
ATMOSPHERE_ALIASES = {"n2": "Nitrogen", "nitrogen": "Nitrogen", "air": "Air"}

FIELD_ATTRS = (
    "field_label", "field_type", "is_required", "editing_required", "help_text", "default_value", "options",
    "source_element_field_key", "table_config",
)


class PlanError(RuntimeError):
    """The stored setup is not what this change expects; nothing is changed."""


def _field_state(field) -> dict:
    return {attr: copy.deepcopy(getattr(field, attr)) for attr in FIELD_ATTRS}


def new_table_config(reference_config: Any) -> dict:
    """The 7-column schema built from the reference (student) advanced table."""
    columns = list((reference_config or {}).get("columns") or [])
    keys = [c.get("key") for c in columns]
    if keys == [NEW_KEY, *EXISTING_KEYS]:
        new_columns = columns
    elif keys == EXISTING_KEYS:
        new_columns = [dict(NEW_COLUMN), *columns]
    else:
        raise PlanError(f"reference columns are {keys}, expected {EXISTING_KEYS}")
    return normalize_table_config({"columns": new_columns, "rows": (reference_config or {}).get("rows") or {}})


def plan_fields(equipment) -> tuple[list, dict]:
    """[(field, before state, after state)] for every field C row of ``equipment``, and the new config."""
    from .models import DynamicInputField

    rows = list(DynamicInputField.objects.filter(equipment=equipment, field_key=FIELD_KEY).order_by("user_type", "pk"))
    by_type = {f.user_type: f for f in rows}
    reference = by_type.get(REFERENCE_USER_TYPE)
    if reference is None or reference.field_type != TYPED_TABLE:
        raise PlanError(f"{equipment.code}: no {REFERENCE_USER_TYPE} advanced table C")
    reference_keys = [c.get("key") for c in (reference.table_config or {}).get("columns") or []]
    for user_type in IITR_USER_TYPES:
        f = by_type.get(user_type)
        if f is None or f.field_type != TYPED_TABLE:
            raise PlanError(f"{equipment.code}: {user_type} C is not an advanced table")
        keys = [c.get("key") for c in (f.table_config or {}).get("columns") or []]
        if keys != reference_keys:
            raise PlanError(f"{equipment.code}: {user_type} C columns {keys} differ from {REFERENCE_USER_TYPE}")
    config = new_table_config(reference.table_config)
    plan = []
    for f in rows:
        if f.field_type not in (TYPED_TABLE, "TABLE"):
            raise PlanError(f"{equipment.code}: {f.user_type or '<shared>'} C is {f.field_type}")
        before = _field_state(f)
        after = {
            **before,
            "field_label": reference.field_label,
            "field_type": TYPED_TABLE,
            "is_required": reference.is_required,
            "editing_required": reference.editing_required,
            "help_text": reference.help_text,
            "default_value": None,
            "options": [],
            "source_element_field_key": None,
            "table_config": config,
        }
        plan.append((f, before, after))
    return plan, config


def _blank(cell: Any) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _mapped_cell(column: dict, cell: Any) -> Any:
    if column["key"] == "atmosphere" and isinstance(cell, str):
        cell = ATMOSPHERE_ALIASES.get(cell.strip().lower(), cell.strip())
    cleaned, problem = _clean_cell(column, cell)
    if problem or cleaned is None:
        return cell.strip() if isinstance(cell, str) else cell
    return cleaned


def convert_table_value(value: Any, config: dict, *, simple_mapping: bool = False) -> tuple[Any, str, Counter]:
    """(new value, kind, non-blank cells per unmapped old column) for one stored C value.

    kinds: empty, advanced (converted), advanced-done, simple-blank (converted), simple-mapped (converted),
    simple-data (held back), other (left alone).
    """
    unmapped: Counter = Counter()
    parsed = parse_table_value(value)
    if parsed in (None, "", []):
        return value, "empty", unmapped
    if not isinstance(parsed, list):
        return value, "other", unmapped
    if all(isinstance(r, dict) for r in parsed):
        if all(NEW_KEY in r for r in parsed):
            return value, "advanced-done", unmapped
        return [r if NEW_KEY in r else {NEW_KEY: "", **r} for r in parsed], "advanced", unmapped
    if not all(isinstance(r, (list, tuple)) for r in parsed):
        return value, "other", unmapped
    if all(all(_blank(c) for c in list(r)[1:]) for r in parsed):
        return [{NEW_KEY: ""} for _r in parsed], "simple-blank", unmapped
    columns = {c["key"]: c for c in config["columns"]}
    rows = []
    for r in parsed:
        r = list(r)
        row = {NEW_KEY: ""}
        for pos, key in SIMPLE_MAPPING.items():
            if pos < len(r) and not _blank(r[pos]):
                row[key] = _mapped_cell(columns[key], r[pos])
        for pos, label in SIMPLE_UNMAPPED.items():
            if pos < len(r) and not _blank(r[pos]):
                unmapped[label] += 1
        if len(r) > len(LEGACY_COLUMNS):
            unmapped["extra columns"] += sum(1 for c in r[len(LEGACY_COLUMNS):] if not _blank(c))
        if any(not _blank(v) for v in row.values()) or any(not _blank(c) for c in r[1:]):
            rows.append(row)
    if not simple_mapping:
        return value, "simple-data", unmapped
    return rows, "simple-mapped", unmapped


def convert_inputs(values: Any, config: dict, *, simple_mapping: bool = False) -> tuple[Any, Counter, Counter]:
    """(new input values, kinds, unmapped) for the base set and every extra sample set."""
    from .calculators import SAMPLE_SETS_KEY

    kinds: Counter = Counter()
    unmapped: Counter = Counter()
    if not isinstance(values, dict):
        return values, kinds, unmapped

    def group(g: dict) -> dict:
        if not isinstance(g, dict) or FIELD_KEY not in g:
            return g
        new, kind, lost = convert_table_value(g[FIELD_KEY], config, simple_mapping=simple_mapping)
        kinds[kind] += 1
        unmapped.update(lost)
        if new is g[FIELD_KEY]:
            return g
        return {**g, FIELD_KEY: new}

    out = group(values)
    sets = values.get(SAMPLE_SETS_KEY)
    if isinstance(sets, list):
        new_sets = [group(s) for s in sets]
        if any(a is not b for a, b in zip(new_sets, sets)):
            out = {**out, SAMPLE_SETS_KEY: new_sets}
    return out, kinds, unmapped


def _records(equipment):
    """(label, model, pk, stored JSON, attribute) for every stored input value of ``equipment``."""
    from .models import Booking, BookingInputTemplate, UrgentBookingRequest

    for pk, iv in Booking.objects.filter(equipment=equipment).values_list("pk", "input_values").order_by("pk"):
        yield "bookings", Booking, pk, iv, "input_values"
    for pk, snap in (
        Booking.objects.filter(equipment=equipment, charge_recalculation_revert_snapshot__isnull=False)
        .values_list("pk", "charge_recalculation_revert_snapshot").order_by("pk")
    ):
        if isinstance(snap, dict) and isinstance(snap.get("input_values"), dict):
            yield "booking revert snapshots", Booking, pk, snap, "charge_recalculation_revert_snapshot"
    for pk, iv in UrgentBookingRequest.objects.filter(equipment=equipment).values_list("pk", "input_values").order_by("pk"):
        yield "urgent requests", UrgentBookingRequest, pk, iv, "input_values"
    for pk, iv in BookingInputTemplate.objects.filter(equipment=equipment).values_list("pk", "input_values").order_by("pk"):
        yield "booking templates", BookingInputTemplate, pk, iv, "input_values"


def _charge(profile, equipment, values) -> tuple:
    from .calculators import (
        ChargeCalculationEngine,
        TimeCalculationEngine,
        build_safe_input_values_for_charge_calculation,
    )

    safe = build_safe_input_values_for_charge_calculation(values, equipment=equipment)
    minutes = TimeCalculationEngine.calculate_time(profile, safe, equipment.slot_duration_minutes)
    total, _breakdown = ChargeCalculationEngine.calculate_charge(profile, safe, minutes)
    return int(minutes or 0), str(total)


def _sample_values(config: dict) -> dict:
    row = {c["key"]: c.get("default") for c in config["columns"] if c.get("default") is not None}
    return {"A": 2, FIELD_KEY: [{NEW_KEY: "S1", **row}, {NEW_KEY: "S2", **row}]}


def run(*, apply: bool, simple_mapping: bool = False, codes=TARGET_CODES, write=print, save_backup=None) -> dict:
    """Plan (and with ``apply`` save) the config and value changes. Returns the backup document.

    ``save_backup(backup)`` runs before the commit; if it raises, nothing is saved.
    """
    from .models import Booking, ChargeProfile, DynamicInputField, Equipment

    backup = {"version": 1, "created": timezone.now().isoformat(), "simple_mapping": simple_mapping,
              "fields": [], "records": []}
    with transaction.atomic():
        for code in codes:
            equipment = Equipment.objects.filter(code=code).first()
            if equipment is None:
                raise PlanError(f"equipment {code!r} not found")
            write(f"\n== {equipment.equipment_id} {equipment.code}")
            plan, config = plan_fields(equipment)
            profiles = list(ChargeProfile.objects.filter(equipment=equipment).order_by("user_type", "pricing_profile"))
            sample = _sample_values(config)
            charges_before = {p.pk: _charge(p, Equipment.objects.get(pk=equipment.pk), sample) for p in profiles}

            for field, before, after in plan:
                changed = before != after
                old_cols = len((before["table_config"] or {}).get("columns") or []) if before["field_type"] == TYPED_TABLE \
                    else len(before["options"] or [])
                write(f"  field C id={field.pk} user_type={field.user_type or '<shared>'}: {before['field_type']}({old_cols} cols) -> "
                      f"{after['field_type']}({len(config['columns'])} cols) label={after['field_label']!r} "
                      f"required={after['is_required']} edit={after['editing_required']} "
                      f"{'CHANGE' if changed else 'unchanged'}")
                if not changed:
                    continue
                backup["fields"].append({"id": field.pk, "equipment": equipment.equipment_id,
                                         "user_type": field.user_type, "before": before, "after": after})
                for attr, value in after.items():
                    setattr(field, attr, value)
                field.save(update_fields=[*FIELD_ATTRS, "updated_at"])

            groups = defaultdict(list)
            for f in DynamicInputField.objects.filter(equipment=equipment):
                groups[f.user_type].append(f)
            for user_type, fields in groups.items():
                problem = validate_table_links(fields)
                if problem:
                    raise PlanError(f"{equipment.code} {user_type}: {problem}")
            write(f"  columns: {[c['key'] for c in config['columns']]} rows={config['rows']}")

            fresh = Equipment.objects.get(pk=equipment.pk)
            for p in profiles:
                after_charge = _charge(p, fresh, sample)
                same = "same" if after_charge == charges_before[p.pk] else "DIFFERENT"
                write(f"  sample charge profile={p.pk} {p.user_type}/{p.pricing_profile}: before={charges_before[p.pk]} "
                      f"after={after_charge} {same}")

            counts = defaultdict(Counter)
            unmapped = defaultdict(Counter)
            held = defaultdict(list)
            sanity = Counter()
            for label, model, pk, stored, attr in _records(equipment):
                values = stored["input_values"] if attr == "charge_recalculation_revert_snapshot" else stored
                new_values, kinds, lost = convert_inputs(values, config, simple_mapping=simple_mapping)
                counts[label].update(kinds)
                unmapped[label].update(lost)
                if kinds.get("simple-data"):
                    held[label].append(pk)
                if new_values is values:
                    continue
                counts[label]["records changed"] += 1
                new_stored = {**stored, "input_values": new_values} if attr == "charge_recalculation_revert_snapshot" \
                    else new_values
                backup["records"].append({"model": model._meta.label, "pk": pk, "attr": attr,
                                          "before": stored, "after": new_stored})
                model.objects.filter(pk=pk).update(**{attr: new_stored})
                if label == "bookings":
                    booking = Booking.objects.select_related("charge_profile").get(pk=pk)
                    try:
                        old = _charge(booking.charge_profile, Equipment.objects.get(pk=equipment.pk), values)
                        new = _charge(booking.charge_profile, fresh, new_values)
                    except Exception as exc:  # noqa: BLE001
                        sanity[f"error {type(exc).__name__}"] += 1
                        continue
                    sanity["recomputed same as before conversion" if old == new else "recomputed DIFFERENT"] += 1
                    equal = Decimal(new[1]) == booking.total_charge
                    sanity["recomputed equals stored amount" if equal else "recomputed differs from stored amount"] += 1
            for label in ("bookings", "booking revert snapshots", "urgent requests", "booking templates"):
                c = counts.get(label) or Counter()
                write(f"  {label}: {dict(sorted(c.items())) or 'none'}")
                if unmapped.get(label):
                    write(f"    unmapped old cells (non-blank): {dict(unmapped[label])}")
                if held.get(label):
                    write(f"    held back (plain table with data) ids: {held[label]}")
            write(f"  charge sanity (converted bookings): {dict(sanity) or 'none'}")

            remaining = Counter()
            for label, _model, _pk, stored, attr in _records(equipment):
                values = stored["input_values"] if attr == "charge_recalculation_revert_snapshot" else stored
                _new, kinds, _lost = convert_inputs(values, config, simple_mapping=False)
                for kind in ("advanced", "simple-blank", "simple-data", "other"):
                    if kinds.get(kind):
                        remaining[f"{label}/{kind}"] += kinds[kind]
            write(f"  remaining old-format values after this run: {dict(remaining) or 0}")

        write("\nproposed plain-table mapping (old position: label -> new key):")
        for pos, label in enumerate(LEGACY_COLUMNS):
            target = SIMPLE_MAPPING.get(pos) or ("(S.No., dropped)" if pos == 0 else "NO TARGET COLUMN")
            write(f"  {pos}: {label} -> {target}")
        if not apply:
            transaction.set_rollback(True)
        elif save_backup is not None:
            save_backup(backup)
    write(f"\n{'APPLIED' if apply else 'DRY RUN (rolled back)'}: fields changed={len(backup['fields'])} "
          f"records changed={len(backup['records'])}")
    return backup


def restore(backup: dict, *, apply: bool, write=print) -> Counter:
    """Put back the saved field definitions and values that still hold what this change wrote."""
    from django.apps import apps

    from .models import DynamicInputField

    out = Counter()
    with transaction.atomic():
        for item in backup.get("fields") or []:
            field = DynamicInputField.objects.filter(pk=item["id"]).first()
            if field is None:
                out["fields missing"] += 1
                continue
            if _field_state(field) != item["after"]:
                out["fields changed since"] += 1
                continue
            for attr, value in item["before"].items():
                setattr(field, attr, value)
            field.save(update_fields=[*FIELD_ATTRS, "updated_at"])
            out["fields restored"] += 1
        for item in backup.get("records") or []:
            model = apps.get_model(item["model"])
            current = model.objects.filter(pk=item["pk"]).values_list(item["attr"], flat=True).first()
            if current is None:
                out["records missing"] += 1
                continue
            if json.dumps(current, sort_keys=True) != json.dumps(item["after"], sort_keys=True):
                out["records changed since"] += 1
                continue
            model.objects.filter(pk=item["pk"]).update(**{item["attr"]: item["before"]})
            out["records restored"] += 1
        if not apply:
            transaction.set_rollback(True)
    write(f"{'RESTORED' if apply else 'RESTORE DRY RUN (rolled back)'}: {dict(out)}")
    return out
