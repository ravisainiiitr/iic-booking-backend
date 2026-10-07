"""Replace the design files (STL / DXF), part names, quantities or own-material choice of a booked
3D print / laser cutting booking.

The caller runs everything inside one transaction, then re-prices the booking through the regular
charge recalculation flow. ``fabrication_state_snapshot`` is stored in the payment-window snapshot so
an unpaid extra charge restores the previous files (``restore_fabrication_state``).
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from .fabrication import (
    active_laser_analyses_for_booking,
    active_print_analyses_for_booking,
    inject_print_parts,
    merge_laser_booking_into_input_values,
    own_material_available,
    parse_bool,
    parse_quantity,
    strip_fabrication_keys,
    validate_laser_analyses,
)
from .models import (
    EquipmentProfileType,
    FabricationFileChange,
    LaserCutAnalysis,
    LaserCutBatch,
    LaserSheetMaterial,
    PrintAnalysis,
    PrintAnalysisBatch,
)

STATE_KEY = "fabrication_state"
CHANGE_ID_KEY = "fabrication_change_id"


class ReuploadError(Exception):
    pass


def _decimal_str(value):
    return str(value) if value is not None else None


def _print_item_state(a) -> dict:
    return {"id": str(a.id), "part_name": a.part_name, "quantity": a.quantity}


def _laser_item_state(a) -> dict:
    return {
        "id": str(a.id),
        "part_name": a.part_name,
        "quantity": a.quantity,
        "material_id": a.material_id,
        "material_code_snapshot": a.material_code_snapshot,
        "sheet_rate_snapshot": _decimal_str(a.sheet_rate_snapshot),
        "units": a.units,
        "width_mm": _decimal_str(a.width_mm),
        "height_mm": _decimal_str(a.height_mm),
        "area_mm2": _decimal_str(a.area_mm2),
    }


def fabrication_state_snapshot(booking) -> dict:
    from .fabrication_workflow import rejection_snapshot

    profile = booking.equipment.profile_type
    state = {
        "profile_type": profile,
        "own_material": bool(booking.own_material),
        "rejection": rejection_snapshot(booking),
    }
    if profile == EquipmentProfileType.PRINT_3D:
        state["print_analysis_id"] = str(booking.print_analysis_id) if booking.print_analysis_id else None
        state["print_analysis_batch_id"] = (
            str(booking.print_analysis_batch_id) if booking.print_analysis_batch_id else None
        )
        state["items"] = [_print_item_state(a) for a in active_print_analyses_for_booking(booking)]
    elif profile == EquipmentProfileType.LASER_CUT_2D:
        state["laser_batch_ids"] = [str(pk) for pk in LaserCutBatch.objects.filter(booking=booking).values_list("id", flat=True)]
        state["items"] = [_laser_item_state(a) for a in active_laser_analyses_for_booking(booking)]
    return state


def file_rows(booking) -> list[dict]:
    """Audit rows for the booking's active design files."""
    profile = booking.equipment.profile_type
    rows = []
    if profile == EquipmentProfileType.PRINT_3D:
        for a in active_print_analyses_for_booking(booking):
            rows.append(
                {
                    "analysis_id": str(a.id),
                    "filename": a.original_filename,
                    "part_name": a.display_part_name,
                    "quantity": a.quantity,
                    "material": a.material_code_snapshot or (a.material.code if a.material else ""),
                }
            )
    elif profile == EquipmentProfileType.LASER_CUT_2D:
        for a in active_laser_analyses_for_booking(booking):
            rows.append(
                {
                    "analysis_id": str(a.id),
                    "filename": a.original_filename,
                    "part_name": a.display_part_name,
                    "quantity": a.quantity,
                    "material": a.material.name if a.material else a.material_code_snapshot,
                    "width_mm": _decimal_str(a.width_mm),
                    "height_mm": _decimal_str(a.height_mm),
                }
            )
    return rows


def _supersede(qs, booking, now) -> None:
    qs.update(booking=None, superseded_booking=booking, superseded_at=now)


def _replace_print_files(booking, actor, *, print_analysis_id, print_analysis_batch_id, now) -> None:
    from .print_3d_views import link_print_analyses_to_booking, merge_print_booking_into_input_values

    _merged, err = merge_print_booking_into_input_values(
        booking.equipment,
        {},
        actor,
        print_analysis_id=print_analysis_id,
        print_analysis_batch_id=print_analysis_batch_id,
    )
    if err:
        raise ReuploadError(err)

    _supersede(PrintAnalysis.objects.filter(booking=booking), booking, now)
    PrintAnalysisBatch.objects.filter(booking=booking).update(booking=None)
    booking.print_analysis = None
    booking.print_analysis_batch = None
    booking.save(update_fields=["print_analysis", "print_analysis_batch", "updated_at"])

    if print_analysis_batch_id:
        batch = PrintAnalysisBatch.objects.get(pk=print_analysis_batch_id)
        link_print_analyses_to_booking(booking, print_analysis_batch_obj=batch)
    else:
        analysis = PrintAnalysis.objects.get(pk=print_analysis_id)
        link_print_analyses_to_booking(booking, print_analysis_obj=analysis)


def _replace_laser_files(booking, actor, *, laser_cut_batch_id, now, own_material) -> None:
    from .fabrication import link_laser_batch_to_booking

    _merged, err, batch = merge_laser_booking_into_input_values(
        booking.equipment, {}, actor, laser_cut_batch_id=laser_cut_batch_id, own_material=own_material
    )
    if err:
        raise ReuploadError(err)
    _supersede(LaserCutAnalysis.objects.filter(booking=booking), booking, now)
    LaserCutBatch.objects.filter(booking=booking).update(booking=None)
    link_laser_batch_to_booking(booking, batch)


def _apply_part_updates(booking, part_updates, *, own_material=False) -> bool:
    """Apply [{analysis_id, part_name?, quantity?, material_id?, units?}] to active parts. Returns True if any."""
    from .laser_cut_views import apply_laser_part_changes

    if not part_updates:
        return False
    if not isinstance(part_updates, list):
        raise ReuploadError("part_updates must be a list.")
    is_laser = booking.equipment.profile_type == EquipmentProfileType.LASER_CUT_2D
    active = (
        active_laser_analyses_for_booking(booking) if is_laser else active_print_analyses_for_booking(booking)
    )
    by_id = {str(a.id): a for a in active}
    changed = False
    for update in part_updates:
        if not isinstance(update, dict):
            raise ReuploadError("Each part update must be an object.")
        analysis = by_id.get(str(update.get("analysis_id") or ""))
        if analysis is None:
            raise ReuploadError("A part update refers to a file that is not on this booking.")
        if is_laser:
            fields = {k: update[k] for k in ("part_name", "quantity", "material_id", "units") if k in update}
            if not fields:
                continue
            before = _laser_item_state(analysis)
            err = apply_laser_part_changes(
                analysis, fields, equipment=booking.equipment, own_material=own_material
            )
            if err:
                raise ReuploadError(f"{analysis.display_part_name}: {err}")
            changed = changed or before != _laser_item_state(analysis)
            continue
        update_fields = []
        if "part_name" in update:
            name = str(update.get("part_name") or "").strip()[:255]
            if name != analysis.part_name:
                analysis.part_name = name
                update_fields.append("part_name")
        if "quantity" in update:
            qty = parse_quantity(update.get("quantity"))
            if qty is None:
                raise ReuploadError(f"{analysis.display_part_name}: number of copies must be a whole number of at least 1.")
            if qty != analysis.quantity:
                analysis.quantity = qty
                update_fields.append("quantity")
        if update_fields:
            analysis.save(update_fields=update_fields + ["updated_at"])
            changed = True
    return changed


def _check_print_time_fits_slots(booking, analyses) -> None:
    from .booking_cancellation import _booking_slot_duration_minutes
    from .slot_allocation import slot_tolerance_minutes_for, slots_needed_for_analysis_time

    total_time = int(inject_print_parts({}, analyses).get("C") or 0)
    slot_count = booking.daily_slots.count()
    if total_time <= 0 or slot_count <= 0:
        return
    needed = slots_needed_for_analysis_time(
        total_time, _booking_slot_duration_minutes(booking), slot_tolerance_minutes_for(booking.equipment)
    )
    if needed > slot_count:
        raise ReuploadError(
            f"The new files need about {total_time} minutes of printing, which is more than the booked slot(s). "
            "Reduce the quantity or cancel and book more slots."
        )


def replace_booking_files(
    booking,
    actor,
    *,
    print_analysis_id=None,
    print_analysis_batch_id=None,
    laser_cut_batch_id=None,
    part_updates=None,
    own_material=None,
) -> FabricationFileChange:
    """Apply the change and write the audit row (charge_after is filled in by the caller after re-pricing).

    Must run inside ``transaction.atomic()``; raises ``ReuploadError`` for invalid requests.
    """
    equipment = booking.equipment
    profile = equipment.profile_type
    is_laser = profile == EquipmentProfileType.LASER_CUT_2D
    if profile not in (EquipmentProfileType.PRINT_3D, EquipmentProfileType.LASER_CUT_2D):
        raise ReuploadError("Files can only be replaced on 3D print or laser cutting bookings.")
    if is_laser and (print_analysis_id or print_analysis_batch_id):
        raise ReuploadError("Upload DXF files for a laser cutting booking.")
    if not is_laser and laser_cut_batch_id:
        raise ReuploadError("Upload STL files for a 3D print booking.")

    now = timezone.now()
    previous_rows = file_rows(booking)
    previous_state = fabrication_state_snapshot(booking)
    previous_own = bool(booking.own_material)
    # The own-material choice in the same request decides whether parts must fit the IIC sheet.
    requested_own = previous_own if own_material is None else parse_bool(own_material)
    skip_sheet_fit = requested_own and own_material_available(equipment)

    files_replaced = bool(print_analysis_id or print_analysis_batch_id or laser_cut_batch_id)
    if files_replaced:
        if is_laser:
            _replace_laser_files(
                booking, actor, laser_cut_batch_id=laser_cut_batch_id, now=now, own_material=skip_sheet_fit
            )
        else:
            _replace_print_files(
                booking,
                actor,
                print_analysis_id=print_analysis_id,
                print_analysis_batch_id=print_analysis_batch_id,
                now=now,
            )

    parts_changed = _apply_part_updates(booking, part_updates, own_material=skip_sheet_fit)

    own_changed = False
    if own_material is not None:
        requested = parse_bool(own_material)
        if requested and not own_material_available(equipment):
            raise ReuploadError("This equipment does not offer the bring-your-own-material option.")
        if requested != previous_own:
            booking.own_material = requested
            booking.save(update_fields=["own_material", "updated_at"])
            own_changed = True

    if not (files_replaced or parts_changed or own_changed):
        raise ReuploadError("Nothing to change.")

    if is_laser:
        err = validate_laser_analyses(
            active_laser_analyses_for_booking(booking),
            require_active_material=False,
            own_material=bool(booking.own_material) and own_material_available(equipment),
        )
        if err:
            raise ReuploadError(err)
    else:
        analyses = active_print_analyses_for_booking(booking)
        if not analyses:
            raise ReuploadError("Upload at least one STL file.")
        _check_print_time_fits_slots(booking, analyses)
        booking.input_values = strip_fabrication_keys(inject_print_parts(booking.input_values, analyses))
        booking.save(update_fields=["input_values", "updated_at"])

    change = FabricationFileChange.objects.create(
        booking=booking,
        changed_by=actor if getattr(actor, "is_authenticated", False) else None,
        profile_type=profile,
        previous_files=previous_rows,
        new_files=file_rows(booking),
        previous_own_material=previous_own,
        new_own_material=bool(booking.own_material),
        charge_before=booking.total_charge,
    )
    change.previous_state = previous_state
    change.files_replaced = files_replaced
    return change


def _restore_laser_item(item: dict) -> None:
    material_id = item.get("material_id")
    if material_id and not LaserSheetMaterial.objects.filter(pk=material_id).exists():
        material_id = None
    LaserCutAnalysis.objects.filter(pk=item["id"]).update(
        part_name=item.get("part_name") or "",
        quantity=item.get("quantity") or 1,
        material_id=material_id,
        material_code_snapshot=item.get("material_code_snapshot") or "",
        sheet_rate_snapshot=Decimal(item["sheet_rate_snapshot"]) if item.get("sheet_rate_snapshot") else None,
        units=item.get("units") or "",
        width_mm=Decimal(item["width_mm"]) if item.get("width_mm") else None,
        height_mm=Decimal(item["height_mm"]) if item.get("height_mm") else None,
        area_mm2=Decimal(item["area_mm2"]) if item.get("area_mm2") else None,
    )


def restore_fabrication_state(booking, state: dict) -> bool:
    """Put back the files / parts / own-material flag captured by ``fabrication_state_snapshot``."""
    if not state or not isinstance(state, dict):
        return False
    from .fabrication_workflow import restore_rejection

    if state.get("rejection") and booking.fabrication_rejected_at is None:
        restore_rejection(booking, state["rejection"])
    profile = state.get("profile_type")
    now = timezone.now()
    item_ids = [i["id"] for i in state.get("items") or [] if i.get("id")]

    if profile == EquipmentProfileType.PRINT_3D:
        _supersede(PrintAnalysis.objects.filter(booking=booking).exclude(pk__in=item_ids), booking, now)
        PrintAnalysisBatch.objects.filter(booking=booking).update(booking=None)
        PrintAnalysis.objects.filter(pk__in=item_ids).update(
            booking=booking, superseded_booking=None, superseded_at=None
        )
        for item in state.get("items") or []:
            PrintAnalysis.objects.filter(pk=item["id"]).update(
                part_name=item.get("part_name") or "", quantity=item.get("quantity") or 1
            )
        batch_id = state.get("print_analysis_batch_id")
        if batch_id:
            PrintAnalysisBatch.objects.filter(pk=batch_id).update(booking=booking)
        booking.print_analysis_id = state.get("print_analysis_id")
        booking.print_analysis_batch_id = batch_id
        booking.own_material = bool(state.get("own_material"))
        booking.save(update_fields=["print_analysis", "print_analysis_batch", "own_material", "updated_at"])
        return True

    if profile == EquipmentProfileType.LASER_CUT_2D:
        _supersede(LaserCutAnalysis.objects.filter(booking=booking).exclude(pk__in=item_ids), booking, now)
        LaserCutBatch.objects.filter(booking=booking).update(booking=None)
        LaserCutAnalysis.objects.filter(pk__in=item_ids).update(
            booking=booking, superseded_booking=None, superseded_at=None
        )
        for item in state.get("items") or []:
            _restore_laser_item(item)
        LaserCutBatch.objects.filter(pk__in=state.get("laser_batch_ids") or []).update(booking=booking)
        booking.own_material = bool(state.get("own_material"))
        booking.save(update_fields=["own_material", "updated_at"])
        return True
    return False
