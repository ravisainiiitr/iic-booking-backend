"""
Finish the fabrication set-up of one department (default "Rethink ! The Tinkering Lab").

CNC Machine Tools
- Open bookings on CNC equipment that is not on 2D laser cutting yet are cancelled through the standard admin
  cancellation: the amount paid is refunded to the wallet and the booker gets the standard notification. The
  equipment then switches to 2D laser cutting and supports every enabled laser sheet.
- The old "No. of Parts" (A) and "Number of Slots" (B) inputs are deleted (input fields have no active flag; the
  config stage writes them to config-before.json and prints them). Time and charge formulas that use an input
  key that no longer exists are cleared, so a laser job takes its booked slots (one slot when nothing is booked).

3D Printers
- The printers support only the enabled master-list materials that no lab printer owns. The material rows owned
  by lab printers are deleted with every lab-printer booking that refers to them (by material id or code). Open
  ones are cancelled and refunded first. A row that equipment outside the lab also uses is kept.
- A booking is kept when deleting it would remove or be blocked by anything other than its own booking records
  (payment orders, gateway transactions, receipts, settlements, reward redemptions, TA assignments, ...).
  Wallet and payment rows are never touched; their totals are compared before and after the delete.

Stages (dry run unless --apply):
  --stage all       dry run of every step (default)
  --stage config    cancellations, CNC switch, inputs, formulas and printer links
  --stage backup    JSON backup of everything the delete stage removes (--backup-dir)
  --stage delete    move the bookings' media files to the archive prefix, then delete (--backup-dir of the backup)
  --stage restore   load a backup back and return the media files (--backup-dir)

Safe to run again. Output: ids, codes, statuses and amounts only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.db.models import Count, Q, Sum
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.utils import timezone

from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
from iic_booking.equipment.booking_paid_amount import booking_paid_charge
from iic_booking.equipment.calculators import ChargeCalculationEngine, TimeCalculationEngine, get_charge_profile_type
from iic_booking.equipment.fabrication import PARTS_KEY
from iic_booking.equipment.management.commands.apply_tinkering_lab_fab_materials import (
    DEFAULT_CNC_CATEGORY,
    DEFAULT_DEPARTMENT,
    DEFAULT_PRINTER_CATEGORY,
    LASER,
    OPEN_STATUSES,
    apply_link_plan,
    code_key,
    plan_supported_materials,
    switch_to_laser,
)
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DynamicInputField,
    Equipment,
    EquipmentProfileType,
    LaserSheetMaterial,
    PrintAnalysis,
    PrintAnalysisBatch,
    PrintMaterial,
)
from iic_booking.users.models import UserType
from iic_booking.users.models.department import Department
from iic_booking.users.repositories.wallet_repository import WalletRepository

CNC_REASON = "Equipment converted to 2D laser cutting (fabrication) by IIC"
PRINTER_REASON = "3D printer materials replaced with the IIC Fabrication Materials list by IIC"
ARCHIVE_PREFIX = "archive/tinkering-lab-material-purge"
REMOVED_INPUTS = {
    "A": re.compile(r"^\s*no\.?\s*of\s+parts", re.IGNORECASE),
    "B": re.compile(r"^\s*number\s+of\s+slots", re.IGNORECASE),
}
FORMULA_FIELDS = ("time_formula", "charge_formula")
INPUT_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z])(?![A-Za-z0-9_])")
# The wallet was debited for these when the booking was made; for the others the payment state is not certain.
PAID_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.BOOKED,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.PROCESSING,
)
# A booking may only take these rows with it (plus auto-created many-to-many link rows).
DELETABLE_WITH_BOOKING = {
    "equipment.Booking",
    "equipment.BookingEvent",
    "equipment.BookingSlotRange",
    "equipment.BookingSampleTrace",
    "equipment.BookingSampleTraceReplyAttachment",
    "equipment.BookingCancellationRequest",
    "equipment.FabricationFileChange",
    "equipment.BookingResultFile",
    "equipment.BookingResultView",
    "equipment.BookingDataShare",
    "equipment.RepeatSampleRequest",
    "equipment.PrintAnalysis",
    "equipment.PrintAnalysisBatch",
}
MONEY_MODELS = (
    ("users.SubWalletTransaction", "amount"),
    ("users.SubWallet", "balance"),
    ("users.Wallet", "balance"),
    ("users.PaymentGatewayTransaction", "amount"),
    ("users.DepartmentPaymentReceipt", "amount"),
    ("users.MigrationBookingSettlement", "refund_amount"),
    ("payments.PaymentOrder", "amount"),
    ("payments.Payment", "amount"),
    ("equipment.TARewardLedger", "points"),
)


def money(value) -> Decimal:
    return Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))


def money_snapshot() -> dict:
    """Row counts and amount totals of the wallet, payment and reward tables."""
    snap = {}
    for label, amount_field in MONEY_MODELS:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        names = {f.name for f in model._meta.concrete_fields}
        agg = {"n": Count("pk")}
        if amount_field in names:
            agg["total"] = Sum(amount_field)
        if label == "users.SubWalletTransaction":
            for kind in ("credit", "debit"):
                row = model.objects.filter(transaction_type=kind).aggregate(**agg)
                snap[f"{label}:{kind}"] = [row["n"], str(row.get("total") or 0)]
            continue
        row = model.objects.aggregate(**agg)
        snap[label] = [row["n"], str(row.get("total") or 0)]
    return snap


def refund_credits(booking) -> tuple[Decimal, list[int]]:
    """Wallet refunds made by the standard cancellation for this booking (matched by its reference)."""
    from iic_booking.users.models.wallet import SubWalletTransaction

    ref = booking_display_id_for_email(booking)
    rows = list(
        SubWalletTransaction.objects.filter(
            transaction_type=SubWalletTransaction.TransactionType.CREDIT,
            description__startswith=f"Refund for cancelled Booking {ref}-",
        ).values_list("pk", "amount")
    )
    return sum((money(a) for _, a in rows), Decimal("0.00")), [pk for pk, _ in rows]


def formula_keys(formula: str) -> set[str]:
    return set(INPUT_KEY_RE.findall(formula or ""))


def json_value(value):
    return str(value) if value is not None and not isinstance(value, (int, float, str, bool)) else value


# ---------------------------------------------------------------------------------------------------- scope / plans


@dataclass
class Scope:
    dept: Department
    cnc: list
    printers: list

    @property
    def printer_ids(self) -> list[int]:
        return [e.pk for e in self.printers]


@dataclass
class PurgePlan:
    rows: list = field(default_factory=list)  # PrintMaterial rows to delete
    kept_rows: list = field(default_factory=list)  # (row, reason)
    bookings: list = field(default_factory=list)  # every lab-printer booking that refers to a row
    groups: dict = field(default_factory=dict)  # booking pk -> objects deleted with it
    blocked: dict = field(default_factory=dict)  # booking pk -> reason
    counts: Counter = field(default_factory=Counter)  # rows deleted with the deletable bookings, per model

    @property
    def open_bookings(self) -> list:
        return [b for b in self.bookings if b.status in OPEN_STATUSES]

    @property
    def deletable(self) -> list:
        return [b for b in self.bookings if b.pk in self.groups]


def load_scope(options) -> Scope:
    dept = Department.objects.filter(name=options["department"]).first()
    if dept is None:
        raise CommandError(f"Department {options['department']!r} not found (exact name).")
    base = Equipment.objects.filter(internal_department=dept).select_related("category").order_by("pk")
    return Scope(
        dept=dept,
        cnc=list(base.filter(category__name__iexact=options["cnc_category"])),
        printers=list(base.filter(category__name__iexact=options["printer_category"])),
    )


def outside_use(row, printer_ids) -> str | None:
    reasons = []
    others = sorted(row.supported_equipment.exclude(pk__in=printer_ids).values_list("pk", flat=True))
    if others:
        reasons.append(f"supported by equipment {others}")
    for label, qs in (
        ("analyses", PrintAnalysis.objects.filter(material=row)),
        ("analysis batches", PrintAnalysisBatch.objects.filter(material=row)),
    ):
        eq_ids = sorted(set(qs.exclude(equipment_id__in=printer_ids).values_list("equipment_id", flat=True)))
        if eq_ids:
            reasons.append(f"{label} on equipment {eq_ids}")
    return "; ".join(reasons) or None


def referencing_bookings(scope: Scope, rows) -> list:
    if not rows:
        return []
    ids = [r.pk for r in rows]
    codes = {code_key(r.code) for r in rows}
    qs = Booking.objects.filter(equipment_id__in=scope.printer_ids)
    hits = set(
        qs.filter(
            Q(print_analyses__material_id__in=ids)
            | Q(print_analysis_batches__material_id__in=ids)
            | Q(print_analysis__material_id__in=ids)
            | Q(print_analysis_batch__material_id__in=ids)
        ).values_list("pk", flat=True)
    )
    for booking_pk, snapshot in PrintAnalysis.objects.filter(booking__in=qs).values_list(
        "booking_id", "material_code_snapshot"
    ):
        if code_key(snapshot) in codes:
            hits.add(booking_pk)
    for booking_pk, values in qs.values_list("pk", "input_values"):
        if isinstance(values, dict) and code_key(str(values.get("B") or "")) in codes:
            hits.add(booking_pk)
    return list(qs.filter(pk__in=hits).select_related("equipment", "user").order_by("pk"))


def booking_objects(booking) -> list[list]:
    """The booking and the 3D print analyses / batches made for it, grouped by model."""
    analyses = Q(booking=booking)
    if booking.print_analysis_id:
        analyses |= Q(pk=booking.print_analysis_id, booking__isnull=True)
    batches = Q(booking=booking)
    if booking.print_analysis_batch_id:
        batches |= Q(pk=booking.print_analysis_batch_id, booking__isnull=True)
    return [
        [booking],
        list(PrintAnalysis.objects.filter(analyses)),
        list(PrintAnalysisBatch.objects.filter(batches)),
    ]


def new_collector(groups) -> Collector:
    collector = Collector(using=DEFAULT_DB_ALIAS, origin=None)
    for group in groups:
        if group:
            collector.collect(group)
    collector.sort()
    return collector


def collected(collector: Collector) -> list[tuple]:
    """(model, instances) in restore order: parents first, cascaded rows after."""
    out = [(model, sorted(insts, key=lambda o: str(o.pk))) for model, insts in reversed(list(collector.data.items()))]
    for qs in collector.fast_deletes:
        rows = list(qs)
        if rows:
            out.append((qs.model, rows))
    return out


def cascade_counts(collector: Collector) -> Counter:
    counts = Counter()
    for model, rows in collected(collector):
        counts[model._meta.label] += len(rows)
    return counts


def unexpected_labels(collector: Collector, allowed: set[str]) -> list[str]:
    return sorted(
        model._meta.label
        for model, rows in collected(collector)
        if rows and model._meta.label not in allowed and not model._meta.auto_created
    )


def plan_purge(scope: Scope) -> PurgePlan:
    plan = PurgePlan()
    for row in PrintMaterial.objects.filter(equipment_id__in=scope.printer_ids).order_by("pk"):
        reason = outside_use(row, scope.printer_ids)
        if reason:
            plan.kept_rows.append((row, reason))
        else:
            plan.rows.append(row)
    plan.bookings = referencing_bookings(scope, plan.rows)
    for b in plan.bookings:
        if b.status in OPEN_STATUSES:
            plan.blocked[b.pk] = f"still {b.status}: run the config stage first"
            continue
        groups = booking_objects(b)
        try:
            collector = new_collector(groups)
        except (ProtectedError, RestrictedError) as exc:
            objs = getattr(exc, "protected_objects", None) or getattr(exc, "restricted_objects", None) or []
            plan.blocked[b.pk] = f"referenced by {sorted({o._meta.label for o in objs})}"
            continue
        extra = unexpected_labels(collector, DELETABLE_WITH_BOOKING)
        if extra:
            plan.blocked[b.pk] = f"would also delete {extra}"
            continue
        foreign = [
            o for model, rows in collected(collector) if model in (PrintAnalysis, PrintAnalysisBatch)
            for o in rows if o.booking_id not in (None, b.pk)
        ]
        if foreign:
            plan.blocked[b.pk] = f"its print files also belong to bookings {sorted({o.booking_id for o in foreign})}"
            continue
        plan.groups[b.pk] = groups
        plan.counts.update(cascade_counts(collector))
    return plan


def purge_collector(plan: PurgePlan) -> Collector:
    by_model = defaultdict(list)
    for groups in plan.groups.values():
        for group in groups:
            for obj in group:
                by_model[type(obj)].append(obj)
    groups = [list({o.pk: o for o in objs}.values()) for objs in by_model.values()]
    return new_collector(groups + [plan.rows])


def set_null_links(collector: Collector) -> list[dict]:
    """Rows that keep living but point at a deleted row (the delete sets them to NULL)."""
    deleted = defaultdict(set)
    for model, rows in collected(collector):
        deleted[model].update(o.pk for o in rows)
    links = []
    for model, pks in deleted.items():
        for rel in model._meta.related_objects:
            if rel.many_to_many or getattr(rel, "on_delete", None) is not models.SET_NULL:
                continue
            target = rel.field.target_field.attname
            values = (
                list(pks) if target == model._meta.pk.attname
                else list(model._base_manager.filter(pk__in=pks).values_list(target, flat=True))
            )
            attname = rel.field.attname
            skip = deleted.get(rel.related_model, set())
            for pk, value in rel.related_model._base_manager.filter(**{f"{attname}__in": values}).values_list(
                "pk", attname
            ):
                if pk not in skip:
                    links.append(
                        {"model": rel.related_model._meta.label, "pk": json_value(pk), "field": attname,
                         "value": json_value(value)}
                    )
    return links


def media_files(collector: Collector, run_id: str) -> list[dict]:
    files = []
    for model, rows in collected(collector):
        file_fields = [f for f in model._meta.concrete_fields if isinstance(f, models.FileField)]
        for obj in rows:
            for f in file_fields:
                name = getattr(obj, f.attname) or ""
                name = str(name)
                if name:
                    files.append({
                        "model": model._meta.label, "pk": json_value(obj.pk), "field": f.name, "name": name,
                        "archive": f"{ARCHIVE_PREFIX}/{run_id}/{name}",
                    })
    return files


def file_storage(entry):
    return apps.get_model(entry["model"])._meta.get_field(entry["field"]).storage


def copy_file(storage, source: str, target: str) -> None:
    if storage.exists(target):
        if storage.exists(source) and storage.size(source) != storage.size(target):
            raise CommandError(f"Archive copy {target} differs from {source}.")
        return
    with storage.open(source, "rb") as fh:
        saved = storage.save(target, ContentFile(fh.read()))
    if saved != target:
        raise CommandError(f"Storage saved {target} as {saved}.")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True, default=str), encoding="utf-8")


def write_checksums(directory: Path) -> None:
    lines = [f"{sha256(p)}  {p.name}" for p in sorted(directory.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_actor(actor_id):
    users = get_user_model().objects.filter(is_active=True)
    if actor_id:
        actor = users.filter(pk=actor_id).first()
        if actor is None or not (actor.is_superuser or str(actor.user_type).lower() == UserType.ADMIN):
            raise CommandError(f"User {actor_id} is not an active main administrator.")
        return actor
    actor = users.filter(user_type=UserType.ADMIN).order_by("pk").first() or users.filter(is_superuser=True).order_by(
        "pk"
    ).first()
    if actor is None:
        raise CommandError("No active main administrator found; pass --actor-id.")
    return actor


def payment_problem(booking, paid: Decimal) -> str | None:
    if paid <= 0:
        return None
    if booking.status not in PAID_STATUSES:
        return f"status {booking.status} with charge {paid}: payment state not certain"
    if money(booking.amount_due) > 0 and not booking.payment_settled_at:
        return f"{booking.amount_due} of the charge is still due"
    target, _ = WalletRepository.get_booking_wallet_target(booking.user, booking.equipment.internal_department)
    if target is None:
        return "no wallet to refund to"
    return None


# ---------------------------------------------------------------------------------------------------- command


class Command(BaseCommand):
    help = (
        "Tinkering Lab fabrication clean-up: cancel bookings, finish the CNC laser switch, drop the old CNC inputs, "
        "and replace the 3D printer materials with the IIC master list (deleting the old rows and their bookings)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--stage", choices=["all", "config", "backup", "delete", "restore"], default="all")
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--backup-dir", default="", help="Directory for the backup files (inside the container).")
        parser.add_argument("--run-id", default="", help="Name of this run in the archive prefix.")
        parser.add_argument("--actor-id", type=int, default=None, help="Main administrator cancelling the bookings.")
        parser.add_argument("--department", default=DEFAULT_DEPARTMENT, help="Exact department name.")
        parser.add_argument("--cnc-category", default=DEFAULT_CNC_CATEGORY)
        parser.add_argument("--printer-category", default=DEFAULT_PRINTER_CATEGORY)

    def handle(self, *args, **options):
        stage, apply = options["stage"], bool(options["apply"])
        if stage == "all" and apply:
            raise CommandError("--stage all is a dry run; apply the stages one by one.")
        if stage in ("backup", "delete", "restore") and not options["backup_dir"]:
            raise CommandError(f"--stage {stage} needs --backup-dir.")
        self.apply = apply
        self.backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        self.run_id = options["run_id"] or timezone.now().strftime("%Y%m%d-%H%M%S")
        self.stdout.write(f"MODE={'APPLY' if apply else 'DRY RUN'} STAGE={stage}")
        if stage == "restore":
            return self._restore()

        scope = load_scope(options)
        self.stdout.write(
            f"department id={scope.dept.pk}\n"
            f"CNC: {[(e.pk, e.code) for e in scope.cnc]}\n"
            f"3D printers: {[(e.pk, e.code) for e in scope.printers]}"
        )
        if stage == "backup":
            return self._backup(scope)
        if stage == "delete":
            return self._delete(scope)

        with transaction.atomic():
            self._config(scope, resolve_actor(options["actor_id"]))
            if stage == "all":
                self._report_purge(plan_purge(scope))
            if not apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("DRY RUN: nothing was saved."))
                return
        self.stdout.write(self.style.SUCCESS("APPLIED."))

    # ------------------------------------------------------------------------------------------------ config

    def _config(self, scope: Scope, actor):
        self.stdout.write(f"actor user id={actor.pk}")
        if self.backup_dir and self.apply:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.backup_dir / "config-before.json", self._config_snapshot(scope))
            self.stdout.write(f"config snapshot written to {self.backup_dir / 'config-before.json'}")

        to_cancel = []
        for eq in scope.cnc:
            if eq.profile_type != LASER:
                to_cancel += [
                    (b, CNC_REASON)
                    for b in Booking.objects.filter(equipment=eq, status__in=OPEN_STATUSES)
                    .select_related("equipment", "user").order_by("pk")
                ]
        purge = plan_purge(scope)
        to_cancel += [(b, PRINTER_REASON) for b in purge.open_bookings]
        self._cancel(to_cancel, actor)

        for eq in scope.cnc:
            self._cnc(eq)
        for eq in scope.printers:
            self._printer(scope, eq)

    def _config_snapshot(self, scope: Scope) -> dict:
        cnc_ids = [e.pk for e in scope.cnc]
        return {
            "equipment": list(Equipment.objects.filter(pk__in=cnc_ids).values("pk", "code", "profile_type")),
            "charge_profiles": list(
                ChargeProfile.objects.filter(equipment_id__in=cnc_ids).values(
                    "pk", "equipment_id", "user_type", "pricing_profile", "profile_type", *FORMULA_FIELDS
                )
            ),
            "input_fields": json.loads(
                serializers.serialize("json", DynamicInputField.objects.filter(equipment_id__in=cnc_ids))
            ),
            "print_material_links": {
                str(m.pk): sorted(m.supported_equipment.values_list("pk", flat=True))
                for m in PrintMaterial.objects.order_by("pk")
            },
            "laser_sheet_links": {
                str(e.pk): sorted(e.supported_laser_sheet_materials.values_list("pk", flat=True)) for e in scope.cnc
            },
        }

    def _cancel(self, items, actor):
        from iic_booking.equipment.api_views import _reverse_reward_points_for_booking, _student_booking_description_suffix

        if not items:
            self.stdout.write("cancel: no open bookings to cancel")
            return
        problems = []
        for b, _reason in items:
            paid = booking_paid_charge(b)
            problem = payment_problem(b, paid)
            first = b.daily_slots.order_by("start_datetime").first()
            start = timezone.localtime(first.start_datetime).strftime("%Y-%m-%d %H:%M") if first else "-"
            self.stdout.write(
                f"cancel booking {b.pk} eq={b.equipment.code}#{b.equipment_id} status={b.status} first_slot={start} "
                f"charge={money(b.total_charge)} wallet_applied={money(b.wallet_amount_applied)} "
                f"due={money(b.amount_due)} paid={paid} refund_to_issue={paid if paid > 0 else Decimal('0.00')}"
                + (f" PROBLEM: {problem}" if problem else "")
            )
            if problem:
                problems.append(b.pk)
        if problems:
            raise CommandError(f"Cancel these bookings by hand first (payment needs a decision): {problems}")

        for b, reason in items:
            paid = booking_paid_charge(b)
            previous = b.status
            result = perform_booking_cancellation(
                b,
                slot_ids=list(b.daily_slots.values_list("id", flat=True)),
                should_refund=paid > 0,
                cancel_notes=reason,
                actor=actor,
                allow_started_slots=True,
                reverse_reward_points_fn=_reverse_reward_points_for_booking,
                student_booking_description_suffix_fn=_student_booking_description_suffix,
                cancelled_by_label="admin",
            )
            txn = result.get("refund_transaction")
            self.stdout.write(
                f"  cancelled booking {b.pk}: {previous} -> {result['new_status']} refunded={result['refund_amount']} "
                f"wallet_credit_txn={getattr(txn, 'pk', None)} released_slots={len(result['released_slot_ids'])}"
            )

    def _cnc(self, eq):
        self.stdout.write(f"-- CNC {eq.code}#{eq.pk} status={eq.status} profile={eq.profile_type}")
        if eq.profile_type != LASER:
            if Booking.objects.filter(equipment=eq, status__in=OPEN_STATUSES).exists():
                self.stdout.write("    SKIP switch: open bookings remain")
            else:
                for line in switch_to_laser(eq):
                    self.stdout.write(f"    {line}")
                eq.refresh_from_db()
        if eq.profile_type == LASER:
            plan = plan_supported_materials(eq, LaserSheetMaterial)
            for m in plan.add:
                self.stdout.write(f"    link sheet {m.code}#{m.pk}")
            for m in plan.remove:
                self.stdout.write(f"    unlink disabled sheet {m.code}#{m.pk}")
            apply_link_plan(eq, LaserSheetMaterial, plan)

        for f in DynamicInputField.objects.filter(equipment=eq).order_by("user_type", "field_key"):
            pattern = REMOVED_INPUTS.get(f.field_key)
            if pattern and pattern.match(f.field_label or ""):
                self.stdout.write(
                    f"    delete input {f.user_type or '*'}/{f.field_key} id={f.pk} {f.field_type} "
                    f"{'required' if f.is_required else 'optional'} label={f.field_label!r} "
                    f"default={f.default_value!r} help={f.help_text!r} options={f.options!r}"
                )
                f.delete()
            else:
                self.stdout.write(f"    keep input {f.user_type or '*'}/{f.field_key} id={f.pk} label={f.field_label!r}")

        remaining = set(DynamicInputField.objects.filter(equipment=eq).values_list("field_key", flat=True))
        for cp in ChargeProfile.objects.filter(equipment=eq).select_related("equipment").order_by("pk"):
            changed = []
            for name in FORMULA_FIELDS:
                value = (getattr(cp, name) or "").strip()
                missing = formula_keys(value) - remaining
                if value and missing:
                    self.stdout.write(
                        f"    charge profile {cp.pk} ({cp.user_type}/{cp.pricing_profile}) {name} {value!r} -> '' "
                        f"(uses removed input {sorted(missing)})"
                    )
                    setattr(cp, name, "")
                    changed.append(name)
            if changed:
                cp.save(update_fields=changed + ["updated_at"])
        self._laser_previews(eq)

    def _laser_previews(self, eq):
        eq.refresh_from_db()
        if eq.profile_type != LASER:
            self.stdout.write(f"    preview: skipped, profile {eq.profile_type}")
            return
        sheet = eq.supported_laser_sheet_materials.filter(is_active=True).order_by("pk").first()
        if sheet is None:
            self.stdout.write("    preview: no enabled sheet supported")
            return
        part = {
            "name": "preview", "quantity": 1, "area_mm2": "10000", "material_code": sheet.code,
            "sheet_width_mm": str(sheet.sheet_width_mm), "sheet_height_mm": str(sheet.sheet_height_mm),
            "sheet_rate": str(sheet.sheet_rate),
        }
        values = {PARTS_KEY: [part]}
        for cp in ChargeProfile.objects.filter(equipment=eq).select_related("equipment").order_by("pk"):
            label = f"{cp.user_type}/{cp.pricing_profile}:{get_charge_profile_type(cp)}"
            try:
                minutes = TimeCalculationEngine.calculate_time(cp, values, eq.slot_duration_minutes)
                total, _ = ChargeCalculationEngine.calculate_charge(cp, values, minutes)
                self.stdout.write(
                    f"    preview {eq.code} cp {cp.pk} {label}: 100x100 mm of {sheet.code} -> time={minutes} min "
                    f"charge={money(total)}"
                )
            except Exception as exc:  # noqa: BLE001 - reported, the run goes on
                self.stdout.write(f"    preview {eq.code} cp {cp.pk} {label}: FAILED {type(exc).__name__}: {exc}")

    def _printer(self, scope: Scope, eq):
        self.stdout.write(f"-- 3D printer {eq.code}#{eq.pk} status={eq.status} profile={eq.profile_type}")
        if eq.profile_type != EquipmentProfileType.PRINT_3D:
            self.stdout.write("    SKIP: not on the 3D print profile")
            return
        manager = eq.supported_print_materials
        old = list(manager.filter(equipment_id__in=scope.printer_ids).order_by("pk"))
        if old:
            self.stdout.write(f"    unlink lab-printer materials {[f'{m.code}#{m.pk}' for m in old]}")
            manager.remove(*old)
        current = {code_key(m.code) for m in manager.all()}
        add = []
        for m in PrintMaterial.objects.filter(is_active=True).exclude(equipment_id__in=scope.printer_ids).order_by("pk"):
            if code_key(m.code) not in current:
                add.append(m)
                current.add(code_key(m.code))
        if add:
            self.stdout.write(f"    link master-list materials {[f'{m.code}#{m.pk}' for m in add]}")
            manager.add(*add)
        final = [f"{m.code}#{m.pk}{'' if m.is_active else '(disabled)'}" for m in manager.order_by("pk")]
        self.stdout.write(f"    materials now {final}")

    # ------------------------------------------------------------------------------------------------ purge

    def _report_purge(self, plan: PurgePlan):
        self.stdout.write("== material rows owned by lab printers")
        for m in plan.rows:
            self.stdout.write(f"  delete material {m.code}#{m.pk} owner={m.equipment_id} active={m.is_active}")
        for m, reason in plan.kept_rows:
            self.stdout.write(f"  KEEP material {m.code}#{m.pk} owner={m.equipment_id}: {reason}")
        self.stdout.write(f"== bookings on lab printers that refer to them: {len(plan.bookings)}")
        for b in plan.bookings:
            refunded, txns = refund_credits(b)
            action = f"BLOCKED: {plan.blocked[b.pk]}" if b.pk in plan.blocked else "delete"
            self.stdout.write(
                f"  booking {b.pk} eq={b.equipment.code}#{b.equipment_id} status={b.status} "
                f"created={timezone.localtime(b.created_at).strftime('%Y-%m-%d')} charged={money(b.total_charge)} "
                f"refunded={refunded} refund_txns={txns} -> {action}"
            )
        self.stdout.write(f"== rows deleted with the bookings: {dict(sorted(plan.counts.items()))}")

    def _manifest(self, plan: PurgePlan, collector: Collector) -> dict:
        bookings = []
        for b in plan.deletable:
            refunded, txns = refund_credits(b)
            bookings.append({
                "pk": b.pk, "equipment_id": b.equipment_id, "equipment_code": b.equipment.code, "status": b.status,
                "created_at": b.created_at.isoformat(), "updated_at": b.updated_at.isoformat(),
                "charged": str(money(b.total_charge)), "refunded": str(refunded), "refund_txns": txns,
            })
        return {
            "run_id": self.run_id,
            "created_at": timezone.now().isoformat(),
            "material_rows": [{"pk": m.pk, "code": m.code, "owner": m.equipment_id} for m in plan.rows],
            "kept_rows": [{"pk": m.pk, "code": m.code, "reason": r} for m, r in plan.kept_rows],
            "bookings": bookings,
            "blocked": {str(k): v for k, v in plan.blocked.items()},
            "counts": dict(cascade_counts(collector)),
            "files": media_files(collector, self.run_id),
        }

    def _backup(self, scope: Scope):
        plan = plan_purge(scope)
        self._report_purge(plan)
        if plan.open_bookings:
            raise CommandError(
                f"Open bookings still refer to these materials: {[b.pk for b in plan.open_bookings]}; "
                "run the config stage first."
            )
        if not (plan.rows or plan.deletable):
            self.stdout.write("backup: nothing to delete")
        collector = purge_collector(plan)
        manifest = self._manifest(plan, collector)
        objects = [o for _model, rows in collected(collector) for o in rows]
        old_umask = os.umask(0o077)
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.backup_dir / "manifest.json", manifest)
            (self.backup_dir / "objects.json").write_text(
                serializers.serialize("json", objects, indent=1), encoding="utf-8"
            )
            write_json(self.backup_dir / "set-null-links.json", set_null_links(collector))
            write_checksums(self.backup_dir)
        finally:
            os.umask(old_umask)
        self.stdout.write(
            f"backup written to {self.backup_dir}: {len(objects)} rows "
            f"({dict(sorted(manifest['counts'].items()))}), {len(manifest['files'])} media files, "
            f"run_id={self.run_id}"
        )

    def _delete(self, scope: Scope):
        manifest_path = self.backup_dir / "manifest.json"
        if not manifest_path.exists():
            raise CommandError(f"No backup at {self.backup_dir}; run the backup stage first.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plan = plan_purge(scope)
        self._report_purge(plan)
        if plan.open_bookings:
            raise CommandError(
                f"Open bookings still refer to these materials: {[b.pk for b in plan.open_bookings]}; "
                "run the config stage first."
            )
        want_rows = sorted(r["pk"] for r in manifest["material_rows"])
        want_bookings = {b["pk"]: b["updated_at"] for b in manifest["bookings"]}
        have_rows = sorted(m.pk for m in plan.rows)
        have_bookings = {b.pk: b.updated_at.isoformat() for b in plan.deletable}
        if not have_rows and not have_bookings:
            self.stdout.write(self.style.SUCCESS("Nothing left to delete (already done)."))
            return
        if have_rows != want_rows or have_bookings != want_bookings:
            raise CommandError(
                "The rows to delete changed since the backup; run the backup stage again. "
                f"backup rows={want_rows} now={have_rows}; backup bookings={sorted(want_bookings)} "
                f"now={sorted(have_bookings)}"
            )

        collector = purge_collector(plan)
        extra = unexpected_labels(collector, DELETABLE_WITH_BOOKING | {"equipment.PrintMaterial"})
        if extra:
            raise CommandError(f"The delete would also remove {extra}; nothing was deleted.")
        files = media_files(collector, manifest["run_id"])
        if sorted((f["name"], f["archive"]) for f in files) != sorted(
            (f["name"], f["archive"]) for f in manifest["files"]
        ):
            raise CommandError("The media files changed since the backup; run the backup stage again.")

        if self.apply:
            for f in files:
                copy_file(file_storage(f), f["name"], f["archive"])
            self.stdout.write(f"archived {len(files)} media files under {ARCHIVE_PREFIX}/{manifest['run_id']}/")

        with transaction.atomic():
            before = money_snapshot()
            deleted_total, deleted = collector.delete()
            after = money_snapshot()
            self.stdout.write(f"deleted {deleted_total} rows: {dict(sorted(deleted.items()))}")
            self.stdout.write(f"wallet/payment totals before={before}")
            self.stdout.write(f"wallet/payment totals after ={after}")
            if before != after:
                raise CommandError("Wallet or payment totals changed; rolled back.")
            if not self.apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("DRY RUN: nothing was deleted."))
                return

        removed, kept = 0, []
        for f in files:
            model = apps.get_model(f["model"])
            if model._base_manager.filter(**{f["field"]: f["name"]}).exists():
                kept.append(f["name"])
                continue
            try:
                file_storage(f).delete(f["name"])
                removed += 1
            except Exception as exc:  # noqa: BLE001 - the archive copy exists
                kept.append(f"{f['name']} ({type(exc).__name__})")
        write_json(
            self.backup_dir / "delete-result.json",
            {"deleted": deleted, "money_before": before, "money_after": after, "originals_removed": removed,
             "originals_kept": kept, "finished_at": timezone.now().isoformat()},
        )
        self.stdout.write(
            f"media originals moved: {removed}, left in place (still used elsewhere or failed): {len(kept)}"
        )
        self.stdout.write(self.style.SUCCESS("APPLIED."))

    # ------------------------------------------------------------------------------------------------ restore

    def _restore(self):
        manifest = json.loads((self.backup_dir / "manifest.json").read_text(encoding="utf-8"))
        links = json.loads((self.backup_dir / "set-null-links.json").read_text(encoding="utf-8"))
        with transaction.atomic():
            call_command("loaddata", str(self.backup_dir / "objects.json"), verbosity=0)
            for link in links:
                apps.get_model(link["model"])._base_manager.filter(pk=link["pk"]).update(
                    **{link["field"]: link["value"]}
                )
            self.stdout.write(
                f"restored {len(manifest['material_rows'])} material rows, {len(manifest['bookings'])} bookings "
                f"and {len(links)} links"
            )
            if not self.apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("DRY RUN: nothing was restored."))
                return
        returned = 0
        for f in manifest["files"]:
            storage = file_storage(f)
            if not storage.exists(f["name"]) and storage.exists(f["archive"]):
                copy_file(storage, f["archive"], f["name"])
                returned += 1
        self.stdout.write(f"media files returned from the archive: {returned}")
        self.stdout.write(self.style.SUCCESS("APPLIED."))
