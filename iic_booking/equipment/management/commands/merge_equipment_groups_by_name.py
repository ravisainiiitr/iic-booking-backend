from django.core.management.base import BaseCommand

from iic_booking.equipment.equipment_group_service import merge_equipment_groups_by_name


class Command(BaseCommand):
    help = "Fold equipment groups that share a name into one group (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        report = merge_equipment_groups_by_name(apply=apply)
        mode = "APPLIED" if apply else "DRY RUN"
        if not report:
            self.stdout.write(f"{mode}: no duplicate group names.")
            return
        for entry in report:
            if entry["skipped"]:
                self.stdout.write(f"{mode}: SKIP {entry['name']!r}: {entry['skipped']} (groups {[entry['keep_id'], *entry['merge_ids']]})")
            else:
                self.stdout.write(
                    f"{mode}: {entry['name']!r} keep group {entry['keep_id']}, merge groups {entry['merge_ids']}, "
                    f"move equipment {entry['moved_equipment']}"
                )
