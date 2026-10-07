"""
Set up the fabrication equipment of one department (default "Rethink ! The Tinkering Lab"):

- every equipment of the "CNC Machine Tools" category switches to the 2D laser cutting profile and supports all
  enabled laser sheets of the Fabrication Materials master list;
- every equipment of the "3D Printers" category supports all enabled 3D print materials of the master list.

An equipment can support only one material per code. For each code the equipment keeps the material it already
supports; otherwise it takes its own material (added for it), else the enabled material with the lowest id. A
linked material is priced by its own row, so the price table names the equipment each row was added for. Nothing
is unlinked except a disabled material of another equipment whose code is needed for an enabled one.

Switching the profile updates the equipment and all its charge profiles (rates are kept). Old hour-based profiles
counted B slots; their time formula becomes "time = max(1, B) * SLOT_DURATION_MINUTES" so a booking still spans B
slots. Existing bookings are not touched and keep their stored charges.

Safe to run again. Output: ids, codes and rates only.

  python manage.py apply_tinkering_lab_fab_materials            # dry run
  python manage.py apply_tinkering_lab_fab_materials --apply
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.calculators import get_charge_profile_type, hour_uses_legacy_b_slots
from iic_booking.equipment.fabrication_material_support import related_name_for
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DynamicInputField,
    Equipment,
    EquipmentProfileType,
    LaserSheetMaterial,
    PrintMaterial,
)
from iic_booking.users.models.department import Department

DEFAULT_DEPARTMENT = "Rethink ! The Tinkering Lab"
DEFAULT_CNC_CATEGORY = "CNC Machine Tools"
DEFAULT_PRINTER_CATEGORY = "3D Printers"
SLOTS_TIME_FORMULA = "time = max(1, B) * SLOT_DURATION_MINUTES"
LASER = EquipmentProfileType.LASER_CUT_2D
OPEN_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.WAITLISTED,
    BookingStatus.BOOKED,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.HOLD,
    BookingStatus.PROCESSING,
)


def code_key(code) -> str:
    return (code or "").strip().lower()


@dataclass
class LinkPlan:
    add: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    # (code, chosen material, [other enabled materials with that code])
    skipped: list = field(default_factory=list)
    # codes where several enabled rows existed and none was supported by or added for the equipment
    picked_lowest_id: list = field(default_factory=list)


def plan_supported_materials(equipment, model) -> LinkPlan:
    """Which enabled master-list materials ``equipment`` should start supporting (one per code)."""
    plan = LinkPlan()
    current = {}
    for m in model.objects.filter(supported_equipment=equipment).order_by("pk"):
        current.setdefault(code_key(m.code), m)
    candidates = defaultdict(list)
    for m in model.objects.filter(is_active=True).order_by("pk"):
        candidates[code_key(m.code)].append(m)

    for key, rows in candidates.items():
        chosen = current.get(key)
        if chosen is not None and not chosen.is_active and chosen.equipment_id != equipment.pk:
            plan.remove.append(chosen)
            chosen = None
        if chosen is None:
            own = [m for m in rows if m.equipment_id == equipment.pk]
            chosen = own[0] if own else rows[0]
            plan.add.append(chosen)
            if not own and len(rows) > 1:
                plan.picked_lowest_id.append(chosen)
        others = [m for m in rows if m.pk != chosen.pk]
        if others:
            plan.skipped.append((chosen.code, chosen, others))
    return plan


def apply_link_plan(equipment, model, plan: LinkPlan) -> None:
    manager = getattr(equipment, related_name_for(model))
    if plan.remove:
        manager.remove(*plan.remove)
    if plan.add:
        manager.add(*plan.add)


def switch_to_laser(equipment) -> list[str]:
    """Move the equipment and all its charge profiles to 2D laser cutting. Returns change lines."""
    changes = []
    b_user_types = set(
        DynamicInputField.objects.filter(equipment=equipment, field_key="B").values_list("user_type", flat=True)
    )
    for cp in ChargeProfile.objects.filter(equipment=equipment).select_related("equipment").order_by("pk"):
        old = get_charge_profile_type(cp)
        fields = []
        if (
            old == EquipmentProfileType.HOUR
            and hour_uses_legacy_b_slots(cp)
            and (cp.user_type in b_user_types or "" in b_user_types)
        ):
            cp.time_formula = SLOTS_TIME_FORMULA
            fields.append("time_formula")
        if cp.profile_type != LASER:
            cp.profile_type = LASER
            fields.append("profile_type")
        if fields:
            cp.save(update_fields=fields + ["updated_at"])
            changes.append(f"charge profile {cp.pk} ({cp.user_type}/{cp.pricing_profile}): {old} -> {LASER}"
                           + (f", time formula {SLOTS_TIME_FORMULA!r}" if "time_formula" in fields else ""))
    if equipment.profile_type != LASER:
        old = equipment.profile_type
        equipment.profile_type = LASER
        equipment.save(update_fields=["profile_type", "updated_at"])
        changes.append(f"equipment profile {old} -> {LASER}")
    return changes


def upcoming_bookings(equipment):
    return list(
        Booking.objects.filter(
            equipment=equipment, status__in=OPEN_STATUSES, daily_slots__end_datetime__gte=timezone.now()
        )
        .distinct()
        .order_by("pk")
    )


class Command(BaseCommand):
    help = (
        "Switch a department's CNC Machine Tools to 2D laser cutting and link the enabled Fabrication Materials "
        "to its CNC and 3D printer equipment (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--department", default=DEFAULT_DEPARTMENT, help="Exact department name.")
        parser.add_argument("--cnc-category", default=DEFAULT_CNC_CATEGORY)
        parser.add_argument("--printer-category", default=DEFAULT_PRINTER_CATEGORY)
        parser.add_argument(
            "--skip-equipment-with-upcoming",
            action="store_true",
            help="Leave CNC equipment that has upcoming bookings on its current profile.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        dept = Department.objects.filter(name=options["department"]).first()
        if dept is None:
            raise CommandError(f"Department {options['department']!r} not found (exact name).")
        base = Equipment.objects.filter(internal_department=dept).select_related("category").order_by("pk")
        cnc = list(base.filter(category__name__iexact=options["cnc_category"]))
        printers = list(base.filter(category__name__iexact=options["printer_category"]))
        self.owner_codes = dict(Equipment.objects.values_list("pk", "code"))
        self.stdout.write(f"MODE={'APPLY' if apply else 'DRY RUN'}")
        self.stdout.write(f"department id={dept.pk} name={dept.name!r}")
        self.stdout.write(
            f"{options['cnc_category']}: {[(e.pk, e.code) for e in cnc]}\n"
            f"{options['printer_category']}: {[(e.pk, e.code) for e in printers]}"
        )

        with transaction.atomic():
            self._section("BEFORE", cnc + printers)
            for eq in cnc:
                self._cnc(eq, skip_upcoming=bool(options["skip_equipment_with_upcoming"]))
            for eq in printers:
                self._printer(eq)
            self._section("AFTER", [Equipment.objects.get(pk=e.pk) for e in cnc + printers])
            if not apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("DRY RUN: nothing was saved."))
                return
        self.stdout.write(self.style.SUCCESS("APPLIED."))

    # ------------------------------------------------------------------ output helpers

    def _owner(self, material) -> str:
        return f"{self.owner_codes.get(material.equipment_id, '?')}#{material.equipment_id}"

    def _section(self, title, equipment):
        self.stdout.write(f"== {title}")
        for eq in equipment:
            cps = sorted(
                {f"{cp.user_type}/{cp.pricing_profile}:{get_charge_profile_type(cp)}"
                 for cp in ChargeProfile.objects.filter(equipment=eq).select_related("equipment")}
            )
            model = LaserSheetMaterial if eq.profile_type == LASER else PrintMaterial
            supported = [
                f"{m.pk}:{m.code}{'' if m.is_active else '(disabled)'}"
                for m in model.objects.filter(supported_equipment=eq).order_by("pk")
            ]
            self.stdout.write(
                f"  {eq.code}#{eq.pk} status={eq.status} profile={eq.profile_type} charge_profiles={cps} "
                f"supported={supported}"
            )

    def _price_rows(self, eq, model):
        for m in model.objects.filter(supported_equipment=eq, is_active=True).order_by("pk"):
            source = "own" if m.equipment_id == eq.pk else f"from {self._owner(m)}"
            if model is LaserSheetMaterial:
                price = f"{m.sheet_rate}/sheet {m.sheet_width_mm}x{m.sheet_height_mm} mm"
            else:
                price = f"{m.price_per_gram}/g"
            self.stdout.write(f"    price {eq.code}#{eq.pk} {m.code}#{m.pk}: {price} ({source})")

    def _report_plan(self, eq, plan: LinkPlan):
        for m in plan.remove:
            self.stdout.write(f"    unlink {m.code}#{m.pk} (disabled, owner {self._owner(m)})")
        for m in plan.add:
            self.stdout.write(f"    link {m.code}#{m.pk} (owner {self._owner(m)})")
        for m in plan.picked_lowest_id:
            self.stdout.write(f"    choice {m.code}: several enabled rows, took the lowest id {m.pk} ({self._owner(m)})")
        for code, chosen, others in plan.skipped:
            self.stdout.write(
                f"    skip duplicates of {code}: kept #{chosen.pk} ({self._owner(chosen)}), "
                f"skipped {[f'#{m.pk}({self._owner(m)})' for m in others]}"
            )
        if not (plan.add or plan.remove):
            self.stdout.write("    materials already complete")

    # ------------------------------------------------------------------ steps

    def _cnc(self, eq, *, skip_upcoming: bool):
        self.stdout.write(f"-- CNC {eq.code}#{eq.pk} status={eq.status} profile={eq.profile_type}")
        upcoming = upcoming_bookings(eq)
        for b in upcoming:
            first = b.daily_slots.order_by("start_datetime").first()
            start = timezone.localtime(first.start_datetime).strftime("%Y-%m-%d %H:%M") if first else "-"
            self.stdout.write(
                f"    upcoming booking {b.pk} status={b.status} first_slot={start} stored_charge={b.total_charge} "
                f"charge_profile={b.charge_profile_id} (left as is)"
            )
        if upcoming and skip_upcoming and eq.profile_type != LASER:
            self.stdout.write("    SKIP: has upcoming bookings (--skip-equipment-with-upcoming)")
            return
        for f in DynamicInputField.objects.filter(equipment=eq).order_by("user_type", "field_key"):
            self.stdout.write(
                f"    input field {f.user_type or '*'}/{f.field_key} {f.field_type} "
                f"{'required' if f.is_required else 'optional'} label={f.field_label!r}"
            )
        for line in switch_to_laser(eq):
            self.stdout.write(f"    {line}")
        eq.refresh_from_db()
        plan = plan_supported_materials(eq, LaserSheetMaterial)
        self._report_plan(eq, plan)
        apply_link_plan(eq, LaserSheetMaterial, plan)
        self._price_rows(eq, LaserSheetMaterial)
        rates = sorted(
            {str(v) for v in ChargeProfile.objects.filter(equipment=eq).values_list("primary_unit_charge", flat=True)}
        )
        self.stdout.write(
            f"    review: machine-time rate per hour={rates} own_material_fixed_charge={eq.own_material_fixed_charge} "
            f"fabrication_emails={len(eq.fabrication_notification_emails or [])}"
        )

    def _printer(self, eq):
        self.stdout.write(f"-- 3D printer {eq.code}#{eq.pk} status={eq.status} profile={eq.profile_type}")
        if eq.profile_type != EquipmentProfileType.PRINT_3D:
            self.stdout.write("    SKIP: not on the 3D print profile")
            return
        plan = plan_supported_materials(eq, PrintMaterial)
        self._report_plan(eq, plan)
        apply_link_plan(eq, PrintMaterial, plan)
        self._price_rows(eq, PrintMaterial)
