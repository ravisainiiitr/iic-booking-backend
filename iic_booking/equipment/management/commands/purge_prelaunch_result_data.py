"""
Archive booking result data created before booking opened (2026-09-30 21:00 IST). See
``iic_booking.equipment.prelaunch_purge`` for scope and the archive / backup layout.

Dry run (default, read-only):
  python manage.py purge_prelaunch_result_data

Apply (moves S3 objects to archive/pre-launch-2026-09-30/, writes manifest + JSON backup):
  python manage.py purge_prelaunch_result_data --apply --confirm PURGE-PRE-LAUNCH --backup-dir /tmp/prelaunch-purge

Restore from a manifest written by --apply:
  python manage.py purge_prelaunch_result_data --restore /path/manifest-<stamp>.json --confirm RESTORE-PRE-LAUNCH
  python manage.py loaddata /path/db-rows-<stamp>.json   # only when the apply removed rows

Prints counts only.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment import prelaunch_purge as purge


class Command(BaseCommand):
    help = "Archive pre-launch (before 2026-09-30 21:00 IST) booking result files and rows. Dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Move / archive (requires --confirm).")
        parser.add_argument("--confirm", default="", help=f'"{purge.CONFIRM_APPLY}" for --apply, "{purge.CONFIRM_RESTORE}" for --restore.')
        parser.add_argument("--backup-dir", default="/tmp/prelaunch-purge", help="Where the manifest and row backup are written.")
        parser.add_argument("--restore", default="", help="Manifest path to restore from.")

    def handle(self, *args, **opts):
        confirm = (opts.get("confirm") or "").strip()
        if opts.get("restore"):
            if confirm != purge.CONFIRM_RESTORE:
                raise CommandError(f'Restore requires --confirm {purge.CONFIRM_RESTORE}')
            out = purge.restore_from_manifest(opts["restore"])
            for k in sorted(out):
                self.stdout.write(f"  {k}={out[k]}")
            return

        apply = bool(opts.get("apply"))
        if apply and confirm != purge.CONFIRM_APPLY:
            raise CommandError(f'--apply requires --confirm {purge.CONFIRM_APPLY}')

        self.stdout.write(f"mode={'apply' if apply else 'dry-run'} cutoff={purge.CUTOFF.isoformat()}")
        report = purge.run(apply=apply, backup_dir=opts["backup_dir"])
        for k in sorted(report.counts):
            self.stdout.write(f"  {k}={report.counts[k]}")
        for k in sorted(report.errors):
            self.stdout.write(f"  error:{k}={report.errors[k]}")
        if apply:
            self.stdout.write(f"manifest={report.manifest_path}")
            self.stdout.write(f"db_backup={report.db_backup_path or 'none (no rows removed)'}")

        from iic_booking.sync.services.results_s3 import _s3_client

        client, bucket = _s3_client()
        if client is not None:
            after = purge.reachable_old_objects(client, bucket)
            for k in sorted(after):
                self.stdout.write(f"  check:{k}={after[k]}")
        if report.errors:
            raise CommandError("Some items failed; re-run to retry (moves are idempotent).")
