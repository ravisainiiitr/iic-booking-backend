"""
Bring faculty IIC wallets up to date with their old-portal balance (same path as the login sync).

Dry run by default; nothing is written. Output has user ids and amounts only.

  python manage.py sync_faculty_legacy_wallets
  python manage.py sync_faculty_legacy_wallets --user-ids 1335,1349
  python manage.py sync_faculty_legacy_wallets --apply

Runs only while the Main Administrator faculty wallet sync deadline is open. Deductions that would
take an IIC sub-wallet below zero are refused and listed. The daily 02:30 IST run uses the same code.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.faculty_wallet_batch_sync import (
    record_run,
    run_faculty_wallet_batch_sync,
    summary_log_line,
)


def _parse_ids(raw: str) -> list[int]:
    ids = []
    for part in (raw or "").replace(" ", ",").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise CommandError(f"--user-ids must be numeric user ids, got {part!r}")
        ids.append(int(part))
    return ids


class Command(BaseCommand):
    help = "Sync faculty IIC wallets with the old portal balance (dry run unless --apply)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Post the wallet entries (default: dry run)")
        parser.add_argument("--user-ids", default="", help="Comma-separated user ids (default: all faculty)")
        parser.add_argument("--json", action="store_true", help="Print the full summary as JSON")

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        user_ids = _parse_ids(options["user_ids"])
        summary = run_faculty_wallet_batch_sync(apply=apply, trigger="manual", user_ids=user_ids or None)
        if apply and summary["status"] != "already_running":
            record_run(summary)

        for row in summary["changes"]:
            self.stdout.write(
                f"{'POSTED' if apply else 'WOULD POST'} user_id={row['user_id']} {row['action']} "
                f"delta={row['delta']} iic_balance {row['iic_balance_before']} -> {row['iic_balance_after']}"
            )
        for row in summary["blocked_below_zero"]:
            self.stdout.write(
                f"BLOCKED (below zero) user_id={row['user_id']} delta={row['delta']} iic_balance={row['iic_balance']}"
            )
        for uid in summary["skipped_admin_mapped_user_ids"]:
            self.stdout.write(f"SKIPPED (admin-mapped to another legacy user) user_id={uid}")
        for row in summary["failed"]:
            self.stdout.write(f"FAILED user_id={row['user_id']} reason={row['reason']}")
        if user_ids:
            seen = {r["user_id"] for r in summary["changes"]} | {r["user_id"] for r in summary["blocked_below_zero"]}
            self.stdout.write(f"requested={len(user_ids)} with_change={len(seen & set(user_ids))}")
        self.stdout.write(summary_log_line(summary))
        self.stdout.write(f"skipped_by_reason={json.dumps(summary['skipped'], sort_keys=True)}")
        if options["json"]:
            self.stdout.write(json.dumps(summary, sort_keys=True))
        if summary["status"] != "completed":
            raise CommandError(f"Sync did not run: {summary['status']}")
