"""Build automatic facility user groups from every booking so far.

Dry run by default (prints counts only); ``--apply`` writes. Safe to re-run: the groups end up matching the
bookings exactly (counts and dates recomputed; people whose only bookings are gone leave automatic groups unless
they were added by hand). Custom groups are never touched.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from iic_booking.facility_groups.membership import rebuild_all


class Command(BaseCommand):
    help = "Build automatic facility user groups (equipment / category / lab / all) from all bookings."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        self.stdout.write(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
        with transaction.atomic():
            summary = rebuild_all(apply=apply)
        for key in (
            "bookings_counted",
            "booking_users",
            "automatic_groups",
            "supervisor_links",
            "groups_to_create",
            "members_to_create",
            "members_to_update",
            "members_to_remove",
        ):
            self.stdout.write(f"{key}: {summary[key]}")
        if not apply:
            self.stdout.write("Dry run only; re-run with --apply to write.")
        else:
            self.stdout.write(self.style.SUCCESS("Facility user groups rebuilt."))
