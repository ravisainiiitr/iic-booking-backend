"""
Bring existing 2D laser / profile cutting equipment onto the DXF machine-time estimate.

- Lists every LASER_CUT_2D equipment with the machine type used for its estimate (saved, else detected from
  Make / Model / Name / Code) and the cutting speed per supported sheet.
- Measures the cut path of DXF parts uploaded before the estimate existed (reads the stored DXF) so their
  bookings show and use the estimate. Parts already measured are skipped unless --remeasure.
- With --pin-detected-preset, saves the detected machine type on equipment that has none saved, so a later
  rename does not change it (the OIC can still change it on the Fabrication Materials page).

Bookings are not recalculated and stored charges are not changed. Output: ids, codes, machine types, sizes
and times only.

  python manage.py backfill_laser_cut_features                       # dry run
  python manage.py backfill_laser_cut_features --apply
  python manage.py backfill_laser_cut_features --apply --pin-detected-preset --equipment 93,94
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from iic_booking.equipment.fabrication import (
    booking_job_quantity,
    booking_own_material,
    booked_minutes,
    laser_time_estimate,
)
from iic_booking.equipment.fabrication_material_support import supported_materials
from iic_booking.equipment.laser_cut_service import analyze_dxf_cut_features
from iic_booking.equipment.laser_time_model import (
    PRESETS,
    equipment_detected_preset,
    estimate_part,
    has_features,
    material_cut_params,
    resolve_profile,
    stored_profile,
    unit_factor,
)
from iic_booking.equipment.models import (
    Booking,
    Equipment,
    EquipmentProfileType,
    LaserCutAnalysis,
    PrintAnalysisStatus,
)


def _ids(raw: str) -> list[int]:
    out = []
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise CommandError(f"Equipment ids must be numbers: {part!r}")
        out.append(int(part))
    return out


class Command(BaseCommand):
    help = "Measure DXF cut paths of existing laser parts and report the machine-time estimate (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the measured cut paths (default: dry run).")
        parser.add_argument("--equipment", default="", help="Comma-separated equipment ids (default: all laser).")
        parser.add_argument("--remeasure", action="store_true", help="Also re-measure parts already measured.")
        parser.add_argument(
            "--pin-detected-preset",
            action="store_true",
            help="Save the detected machine type on equipment without a saved one.",
        )
        parser.add_argument("--limit", type=int, default=0, help="At most this many parts (0 = all).")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        equipment = Equipment.objects.filter(profile_type=EquipmentProfileType.LASER_CUT_2D).order_by("pk")
        ids = _ids(options["equipment"])
        if ids:
            equipment = equipment.filter(pk__in=ids)
        equipment = list(equipment)
        self.stdout.write(f"MODE={'APPLY' if apply else 'DRY RUN'} equipment={[(e.pk, e.code) for e in equipment]}")

        for eq in equipment:
            self._equipment(eq, apply=apply, pin=bool(options["pin_detected_preset"]))

        measured, failed, skipped = self._parts(
            equipment, apply=apply, remeasure=bool(options["remeasure"]), limit=int(options["limit"] or 0)
        )
        self.stdout.write(f"parts: measured={measured} failed={failed} already_measured={skipped}")
        self._open_bookings(equipment)
        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing was saved."))
        else:
            self.stdout.write(self.style.SUCCESS("APPLIED."))

    def _equipment(self, eq, *, apply: bool, pin: bool):
        stored = stored_profile(eq)
        detected = equipment_detected_preset(eq)
        saved = stored.get("preset") if stored.get("preset") in PRESETS else ""
        self.stdout.write(
            f"-- {eq.code}#{eq.pk} name={eq.name!r} make={eq.make!r} model={eq.model_information!r} "
            f"status={eq.status} slot_min={eq.slot_duration_minutes}\n"
            f"    saved_type={saved or '-'} detected_type={detected} overrides={sorted((stored.get('overrides') or {}))}"
        )
        if pin and not saved:
            self.stdout.write(f"    pin machine type {detected}")
            if apply:
                eq.laser_estimate_profile = {**stored, "preset": detected}
                eq.save(update_fields=["laser_estimate_profile", "updated_at"])
        profile = resolve_profile(eq)
        for m in supported_materials(eq).order_by("display_order", "name"):
            cut = material_cut_params(profile, m)
            note = f" ({cut.warning})" if cut.warning else ""
            self.stdout.write(
                f"    sheet {m.code}#{m.pk} {m.material_family} {m.thickness_mm} mm: "
                f"{cut.speed_mm_s:.1f} mm/s, pierce {cut.pierce_s:.2f} s{' [override]' if cut.overridden else ''}{note}"
            )

    def _parts(self, equipment, *, apply: bool, remeasure: bool, limit: int):
        qs = (
            LaserCutAnalysis.objects.filter(
                equipment__in=equipment, status=PrintAnalysisStatus.COMPLETED, cancelled_at__isnull=True
            )
            .select_related("material", "equipment")
            .order_by("pk")
        )
        if not remeasure:
            qs = qs.filter(Q(cut_features__isnull=True) | Q(cut_features={}))
        skipped = 0 if remeasure else (
            LaserCutAnalysis.objects.filter(
                equipment__in=equipment, status=PrintAnalysisStatus.COMPLETED, cancelled_at__isnull=True
            ).count()
            - qs.count()
        )
        measured = failed = 0
        profiles = {}
        for a in qs[:limit] if limit else qs:
            try:
                with a.dxf_file.open("rb") as fh:
                    data = fh.read()
                features = analyze_dxf_cut_features(data)
            except Exception as exc:  # noqa: BLE001 - unreadable / missing file: report and continue
                failed += 1
                self.stdout.write(f"    part {a.pk} booking={a.booking_id}: not measured ({exc.__class__.__name__})")
                continue
            measured += 1
            line = (
                f"    part {a.pk} booking={a.booking_id} units={a.units} cut={features['cut_length']:.1f} "
                f"contours={features['contours']} duplicates={features['duplicates']}"
            )
            if a.material is not None and has_features(features):
                profile = profiles.get(a.equipment_id) or profiles.setdefault(a.equipment_id, resolve_profile(a.equipment))
                t = estimate_part(features, unit_factor(a.units), profile, material_cut_params(profile, a.material))
                line += f" est_each={t.total_s:.0f}s"
            self.stdout.write(line)
            if apply:
                a.cut_features = features
                a.save(update_fields=["cut_features", "updated_at"])
        return measured, failed, skipped

    def _open_bookings(self, equipment):
        open_statuses = ("PENDING", "PENDING_PAYMENT", "BOOKED", "HOLD", "PROCESSING")
        bookings = (
            Booking.objects.filter(equipment__in=equipment, status__in=open_statuses)
            .select_related("equipment")
            .order_by("pk")
        )
        for b in bookings:
            analyses = list(
                LaserCutAnalysis.objects.filter(booking=b, cancelled_at__isnull=True)
                .select_related("material", "equipment")
                .order_by("sequence", "created_at")
            )
            estimate = laser_time_estimate(analyses, booking_job_quantity(b), booking_own_material(b))
            self.stdout.write(
                f"  open booking {b.pk} {b.equipment.code} status={b.status} booked_min={booked_minutes(b)} "
                f"estimate_min={estimate.total_min if estimate else '-'} (stored charge unchanged)"
            )
