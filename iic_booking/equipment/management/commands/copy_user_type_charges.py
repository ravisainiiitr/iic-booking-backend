import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment import charge_copy
from iic_booking.equipment.models import ChargeCopyBatch


class Command(BaseCommand):
    help = (
        "Copy one user type's charges (charge profiles, charge-scoped input fields, slot options) to another, "
        "for every equipment. Dry run unless --apply. Existing target rows are never changed. "
        "Undo an applied run with --rollback <batch id>. Output has codes and counts only, no amounts."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", default=charge_copy.DEFAULT_SOURCE)
        parser.add_argument("--target", default=charge_copy.DEFAULT_TARGET)
        parser.add_argument("--apply", action="store_true", help="Create the rows (default: dry run).")
        parser.add_argument("--rollback", type=int, metavar="BATCH_ID", help="Remove the rows created by a batch.")
        parser.add_argument("--list-batches", action="store_true", help="Show recorded batches.")

    def handle(self, *args, **options):
        if options["list_batches"]:
            for b in ChargeCopyBatch.objects.order_by("pk"):
                state = f"rolled back {b.rolled_back_at:%Y-%m-%d %H:%M}" if b.rolled_back_at else "active"
                counts = {k: len(v) for k, v in (b.created or {}).items()}
                self.stdout.write(f"batch {b.pk}: {b.source_user_type} -> {b.target_user_type} {counts} ({state})")
            return

        if options["rollback"] is not None:
            try:
                report = charge_copy.rollback_copy(options["rollback"])
            except ChargeCopyBatch.DoesNotExist as exc:
                raise CommandError(f"No batch {options['rollback']}.") from exc
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"ROLLED BACK batch {options['rollback']}: {json.dumps(report, sort_keys=True)}")
            return

        source, target = options["source"], options["target"]
        apply = bool(options["apply"])
        try:
            if apply:
                batch, plan = charge_copy.apply_copy(source, target)
            else:
                batch, plan = None, charge_copy.plan_copy(source, target)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        mode = "APPLY" if apply else "DRY RUN"
        verb = "created" if apply else "would create"
        self.stdout.write(f"{mode}: copy charges {source} -> {target}")
        for entry in plan:
            variants = ", ".join(cp.pricing_profile for cp in entry["charge_profiles"]) or "none"
            self.stdout.write(
                f"  {entry['code']} [{entry['status']}]: {verb} charge profiles: {variants}; "
                f"input fields: {len(entry['input_fields'])}; slot options: {len(entry['param_definitions'])}"
            )
            for reason in entry["skipped"]:
                self.stdout.write(f"      skipped: {reason}")
        self.stdout.write(f"SUMMARY {json.dumps(charge_copy.summarize(plan), sort_keys=True)}")
        self.stdout.write(f"NOT COPIED (context) {json.dumps(charge_copy.context_counts(source, target), sort_keys=True)}")
        if apply:
            if batch is None:
                self.stdout.write("Nothing to create; no batch recorded.")
            else:
                self.stdout.write(f"BATCH {batch.pk} recorded. Undo with: --rollback {batch.pk}")
