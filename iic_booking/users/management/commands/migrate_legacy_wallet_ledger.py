"""Dry-run or apply Employee-ID mapping + ledger-first wallet import."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from iic_booking.users.legacy_ledger.reader import OldMySQLConnectionError, OldMySQLNotConfigured, OldMySQLReader
from iic_booking.users.legacy_ledger.reconcile import run_full_reconciliation
from iic_booking.users.legacy_ledger.sync import dry_run_report, run_ledger_sync, run_mapping


class Command(BaseCommand):
    help = (
        "Legacy wallet ledger migration. Default is dry-run (no financial writes). "
        "Credentials: OLD_MYSQL_* environment only. --apply writes to the CURRENT Django database."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=True)
        parser.add_argument("--apply", action="store_true", help="Write mapping and ledger into the current Django DB.")
        parser.add_argument(
            "--confirm-write-to-current-database",
            action="store_true",
            help="Required with --apply. Prevents accidental production import.",
        )
        parser.add_argument("--mapping-only", action="store_true")
        parser.add_argument("--ledger-only", action="store_true")
        parser.add_argument("--reconcile", action="store_true")
        parser.add_argument("--limit", type=int, default=None, help="Max wallet_transactions rows this run.")
        parser.add_argument("--batch", default="")
        parser.add_argument("--report-json", default="", help="Write a machine-readable report (no secrets).")

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        if options["apply"] and not options["confirm_write_to_current_database"]:
            raise CommandError(
                "Refusing --apply without --confirm-write-to-current-database. "
                "Point DATABASE_URL at an isolated/staging database first."
            )
        batch = options["batch"] or timezone.now().strftime("mig-%Y%m%d%H%M%S")
        report = {"batch": batch, "dry_run": dry_run}
        mapping = {}
        try:
            with OldMySQLReader() as reader:
                if options["reconcile"]:
                    recon = run_full_reconciliation()
                    recon.pop("rows", None)
                    self.stdout.write(json.dumps(recon, default=str))
                    return
                if not options["ledger_only"]:
                    mapping = run_mapping(reader, batch=batch, dry_run=dry_run)
                    report["mapping"] = {
                        k: v for k, v in mapping.items() if k not in {"rows", "exceptions"}
                    }
                    report["mapping_counts"] = {str(k): v for k, v in mapping["counts"].items()}
                    self.stdout.write(f"mapping_counts={report['mapping_counts']} dry_run={dry_run}")
                    self.stdout.write(f"exception_rows={mapping['exception_count']}")
                    if options["mapping_only"]:
                        self._write_report(options["report_json"], report)
                        return
                result = run_ledger_sync(reader, batch=batch, dry_run=dry_run, limit=options["limit"])
                report["ledger"] = result
                self.stdout.write(str(result))
                if dry_run:
                    report["summary"] = dry_run_report(mapping, result)
                    self.stdout.write(json.dumps(report["summary"], default=str))
                if options["apply"] and not dry_run:
                    recon = run_full_reconciliation()
                    report["reconciliation"] = {k: v for k, v in recon.items() if k != "rows"}
                    self.stdout.write(str(report["reconciliation"]))
        except OldMySQLNotConfigured as exc:
            raise CommandError(str(exc)) from exc
        except OldMySQLConnectionError as exc:
            raise CommandError(str(exc)) from exc
        self._write_report(options["report_json"], report)

    def _write_report(self, path, report):
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        self.stdout.write(f"Wrote {path}")
