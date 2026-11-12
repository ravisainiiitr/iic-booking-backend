"""Create a community for every Equipment Group and sync its membership from bookings and staff assignments.

DRY RUN BY DEFAULT: nothing is written unless ``--apply`` is given. A dry run performs the exact same work inside a
transaction that is rolled back, so its report matches what ``--apply`` would do.

Examples
    python manage.py connect_sync_communities                     # dry run, summary
    python manage.py connect_sync_communities --report sync.json  # dry run, full JSON report
    python manage.py connect_sync_communities --apply             # write
    python manage.py connect_sync_communities --apply --group 12 --group 15

Works whether or not IITR_CONNECT_ENABLED is on (run it before switching the module on). Idempotent: running it
again changes nothing unless bookings or assignments changed.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.connect.services.membership import sync_all


class Command(BaseCommand):
    help = "Sync IITR Connect communities and membership (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--group", type=int, action="append", dest="groups", help="Limit to these group ids.")
        parser.add_argument("--report", help="Write the full JSON report to this path.")
        parser.add_argument("--json", action="store_true", help="Print the full JSON report.")

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        report = sync_all(dry_run=dry_run, group_ids=options.get("groups"), source="command")
        data = report.as_dict()
        if options.get("report"):
            with open(options["report"], "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
        if options.get("json"):
            self.stdout.write(json.dumps(data, indent=2, default=str))
            return
        mode = "DRY RUN (nothing written)" if dry_run else "APPLIED"
        self.stdout.write(self.style.WARNING(mode) if dry_run else self.style.SUCCESS(mode))
        lines = [
            f"Scope: {data['scope']}",
            f"Communities created: {len(data['communities_created'])}",
            f"Communities renamed: {len(data['communities_renamed'])}",
            f"Communities archived (group removed): {len(data['communities_archived'])}",
            f"Communities reactivated: {len(data['communities_reactivated'])}",
            f"Test-only flag changed: {len(data['test_only_changed'])}",
            f"Members created: {data['members_created']}",
            f"Members reactivated: {data['members_reactivated']}",
            f"Members lapsed (staff source ended): {data['members_lapsed']}",
            f"Sources added / removed: {data['sources_added']} / {data['sources_removed']}",
            f"Unchanged memberships: {data['unchanged']}",
            f"Errors: {len(data['errors'])}",
        ]
        for line in lines:
            self.stdout.write(line)
        if dry_run:
            self.stdout.write("Run again with --apply to write these changes.")
