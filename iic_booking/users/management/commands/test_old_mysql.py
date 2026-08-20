"""Test SSH-optional topology and read-only old MySQL connectivity. Never prints passwords."""

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.reader import (
    OldMySQLConnectionError,
    OldMySQLNotConfigured,
    OldMySQLReader,
)


class Command(BaseCommand):
    help = "Probe old IIC MySQL (read-only). Uses OLD_MYSQL_* env vars. Does not print secrets."

    def handle(self, *args, **options):
        try:
            with OldMySQLReader() as reader:
                report = reader.connection_probe()
        except OldMySQLNotConfigured as exc:
            raise CommandError(str(exc)) from exc
        except OldMySQLConnectionError as exc:
            raise CommandError(str(exc)) from exc

        for key, value in report.items():
            if key in {"password", "OLD_MYSQL_PASSWORD"}:
                continue
            if key == "schema_discovery" and isinstance(value, dict):
                self.stdout.write(f"tables: {value.get('tables')}")
                self.stdout.write(f"schema_mapping: {value.get('mapping')}")
                continue
            self.stdout.write(f"{key}: {value}")
        audit = reader.live_financial_audit()
        self.stdout.write("live_financial_audit:")
        for key, value in audit.items():
            if key == "type_samples":
                self.stdout.write(f"  {key}: {len(value)} samples (prefixes only)")
                continue
            self.stdout.write(f"  {key}: {value}")
        if report.get("account_appears_writable"):
            self.stdout.write(self.style.WARNING(report.get("writable_account_recommendation")))
        if not report.get("ok"):
            raise CommandError("Schema probe failed (missing tables or columns).")
        self.stdout.write(self.style.SUCCESS("Old MySQL read probe succeeded."))
