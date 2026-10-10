"""
Mark (or unmark) test accounts so their wallet activity and bookings are not counted in revenue.

Output is ids, user types, counts and flags only (public CI logs).

  python manage.py flag_test_accounts                               # read-only report
  python manage.py flag_test_accounts --user-ids 78,91              # dry run: what would change
  python manage.py flag_test_accounts --user-ids 78 --confirm FLAG_TEST_ACCOUNTS
  python manage.py flag_test_accounts --user-ids 78 --unflag --confirm FLAG_TEST_ACCOUNTS
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


def _split(raw: str) -> list[str]:
    return [p for p in (raw or "").replace(";", ",").replace(" ", ",").split(",") if p.strip()]


class Command(BaseCommand):
    help = "Report test accounts, or flag / unflag given users as test accounts (dry run unless --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("--user-ids", default="", help="Comma-separated user ids.")
        parser.add_argument("--emails", default="", help="Comma-separated emails (not echoed back).")
        parser.add_argument("--unflag", action="store_true", help="Remove the test-account mark instead.")
        parser.add_argument("--confirm", default="", help='Must be "FLAG_TEST_ACCOUNTS" to write.')

    def handle(self, *args, **options):
        from iic_booking.users.test_account_flags import (
            CONFIRM_TOKEN,
            build_report,
            is_protected_account,
            resolve_targets,
            set_test_account_flag,
        )

        ids, emails = _split(options["user_ids"]), _split(options["emails"])
        if not ids and not emails:
            if options["confirm"]:
                raise CommandError("Give --user-ids or --emails to change anything.")
            self._report(build_report())
            return

        apply = options["confirm"] == CONFIRM_TOKEN
        if options["confirm"] and not apply:
            raise CommandError(f'--confirm must be exactly "{CONFIRM_TOKEN}".')
        value = not options["unflag"]
        users, missing = resolve_targets(ids, emails)
        for m in missing:
            self.stdout.write(f"not_found {m}")
        self.stdout.write(f"mode={'apply' if apply else 'dry-run'} set_is_test_account={value} targets={len(users)}")
        changed = 0
        with transaction.atomic():
            for user in users:
                line = f"  user_id={user.pk} user_type={user.user_type} is_active={user.is_active} before={user.is_test_account}"
                if value and is_protected_account(user):
                    self.stdout.write(f"{line} refused=main_administrator")
                    continue
                if bool(user.is_test_account) == value:
                    self.stdout.write(f"{line} unchanged")
                    continue
                if apply:
                    set_test_account_flag(user, value, source="flag_test_accounts command")
                    changed += 1
                    self.stdout.write(f"{line} after={value} applied=True")
                else:
                    self.stdout.write(f"{line} would_set={value}")
        self.stdout.write(f"changed={changed}" if apply else "dry_run=True (nothing written)")

    def _report(self, report):
        self.stdout.write(f"flagged_test_accounts={len(report['flagged'])}")
        for row in report["flagged"]:
            self.stdout.write("  " + " ".join(f"{k}={v}" for k, v in row.items()))
        self.stdout.write(f"unflagged_candidates={len(report['candidates'])}")
        for row in report["candidates"]:
            self.stdout.write("  " + " ".join(f"{k}={v}" for k, v in row.items()))
        self.stdout.write(
            f"sric_follow_up_days={report['sric_follow_up_days']} shown={len(report['sric_follow_up'])} "
            f"hidden_as_test={len(report['sric_follow_up_hidden_test'])}"
        )
        for row in report["sric_follow_up"]:
            self.stdout.write("  shown " + " ".join(f"{k}={v}" for k, v in row.items()))
        for row in report["sric_follow_up_hidden_test"]:
            self.stdout.write("  hidden_test " + " ".join(f"{k}={v}" for k, v in row.items()))
