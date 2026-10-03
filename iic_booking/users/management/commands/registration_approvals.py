import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users import registration_approvals as svc


class Command(BaseCommand):
    help = (
        "Registration approvals and programme expiry. 'report' and 'status' / 'dry-run' are read-only "
        "(counts only, no names or addresses); 'enable' / 'disable' switch the daily expiry automation."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["report", "status", "dry-run", "enable", "disable"])

    def handle(self, *args, **options):
        action = options["action"]
        if action == "report":
            self.stdout.write(json.dumps(svc.production_report(), indent=2, sort_keys=True))
            return
        if not svc.schema_ready():
            raise CommandError("Registration approval tables are not migrated yet (users 0128).")
        if action == "status":
            row = svc.policy()
            self.stdout.write(
                f"expiry_automation_enabled={row.expiry_automation_enabled} enabled_at={svc._iso(row.enabled_at)} "
                f"warning_days={list(svc.warning_days(row))}"
            )
            return
        if action == "dry-run":
            data = svc.dry_run()
            self.stdout.write(
                json.dumps(
                    {
                        "automation_enabled": data["automation_enabled"],
                        "today": data["today"],
                        "warning_days": data["warning_days"],
                        "counts": data["counts"],
                        "would_warn_by_days": _by_days(data["would_warn"]),
                        "would_disable_with_future_bookings": sum(1 for r in data["would_disable"] if r["future_bookings"]),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        svc.set_automation(action == "enable")
        self.stdout.write(f"Programme expiry automation {'enabled' if action == 'enable' else 'disabled'}.")


def _by_days(rows):
    out: dict[str, int] = {}
    for row in rows:
        key = str(row["days"])
        out[key] = out.get(key, 0) + 1
    return out
