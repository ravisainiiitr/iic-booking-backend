"""
TGA/DTA [A] / [B] field C: add the required "Sample Name/Code" first column to the advanced table, give every
user type that same table (plain TABLE rows replaced) and convert stored booking / template values. See
iic_booking.equipment.tga_field_c. No charges, statuses, wallets or emails are touched.

Output: ids, codes, user types, kinds and counts only. The backup (old and new values) is written to
--backup-dir as backup.json and is never printed.

  python manage.py tga_field_c                                   # dry run
  python manage.py tga_field_c --apply --backup-dir /tmp/x
  python manage.py tga_field_c --apply --simple-mapping --backup-dir /tmp/x   # also map plain tables with data
  python manage.py tga_field_c --restore /tmp/x/backup.json [--apply]
"""

from __future__ import annotations

import hashlib
import json
import os

from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment.tga_field_c import PlanError, restore, run


class Command(BaseCommand):
    help = "TGA/DTA field C: Sample Name/Code column, one advanced table for all user types, values converted."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--simple-mapping", action="store_true",
                            help="Also convert plain-table values that hold data (proposed column mapping).")
        parser.add_argument("--backup-dir", default="", help="Directory for backup.json (required with --apply).")
        parser.add_argument("--restore", default="", help="backup.json to restore from.")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        if options["restore"]:
            with open(options["restore"], encoding="utf-8") as fh:
                restore(json.load(fh), apply=apply, write=self.stdout.write)
            return
        backup_dir = options["backup_dir"]
        if apply and not backup_dir:
            raise CommandError("--backup-dir is required with --apply")

        def save_backup(backup: dict) -> None:
            os.makedirs(backup_dir, exist_ok=True)
            data = json.dumps(backup, ensure_ascii=False, default=str, indent=1).encode("utf-8")
            with open(os.path.join(backup_dir, "backup.json"), "wb") as fh:
                fh.write(data)
            digest = hashlib.sha256(data).hexdigest()
            with open(os.path.join(backup_dir, "SHA256SUMS"), "w", encoding="utf-8") as fh:
                fh.write(f"{digest}  backup.json\n")
            self.stdout.write(f"backup: {len(data)} bytes sha256={digest}")

        try:
            run(apply=apply, simple_mapping=bool(options["simple_mapping"]), write=self.stdout.write,
                save_backup=save_backup)
        except PlanError as exc:
            raise CommandError(f"stopped, nothing changed: {exc}") from exc
