"""
Import legacy wallet balances from CSV exported from MySQL (users + user_wallet.balance).

Export on legacy server (MySQL database `admin`):

  mysql -u root -p admin -e "
    SELECT u.emp_id, u.email, uw.balance
    FROM users u
    INNER JOIN user_wallet uw ON uw.user_id = u.id
    WHERE uw.balance <> 0 AND u.emp_id IS NOT NULL AND TRIM(u.emp_id) <> ''
  " | sed 's/\\t/,/g' > legacy_wallet_balances.csv

Usage (staging first):

  python manage.py import_legacy_wallet_balances legacy_wallet_balances.csv \\
    --batch-id TEST-001 --department general --dry-run

  python manage.py import_legacy_wallet_balances legacy_wallet_balances.csv \\
    --batch-id TEST-001 --department general
"""

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_wallet_import import import_legacy_wallet_balances, read_legacy_wallet_csv


class Command(BaseCommand):
    help = "Import legacy user_wallet.balance from CSV (matched by emp_id) into sub-wallets"

    def add_arguments(self, parser):
        parser.add_argument("file", help="CSV with emp_id,balance[,email] from legacy MySQL export")
        parser.add_argument(
            "--batch-id",
            required=True,
            help="Unique migration batch id (stored in transaction description; prevents double import)",
        )
        parser.add_argument(
            "--department",
            choices=["general", "user"],
            default="general",
            help="Credit target: 'general' sub-wallet (default) or user's internal HR department",
        )
        parser.add_argument(
            "--department-id",
            type=int,
            default=None,
            metavar="ID",
            help="Internal department ID to credit (overrides --department)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report only; do not credit wallets",
        )

    def handle(self, *args, **options):
        path = options["file"]
        batch_id = options["batch_id"].strip()
        if not batch_id:
            self.stderr.write(self.style.ERROR("--batch-id is required"))
            return

        try:
            rows = read_legacy_wallet_csv(path)
        except (OSError, ValueError) as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        self.stdout.write(f"Read {len(rows)} row(s) from {path}")

        result = import_legacy_wallet_balances(
            rows,
            batch_id=batch_id,
            department_id=options["department_id"],
            use_general_department=options["department"] == "general",
            dry_run=options["dry_run"],
        )

        mode = "DRY RUN" if options["dry_run"] else "APPLIED"
        self.stdout.write(self.style.SUCCESS(f"\n[{mode}] Batch {result['batch_id']}"))
        self.stdout.write(f"  Credited: {result['credited']}")
        self.stdout.write(f"  Skipped:  {result['skipped']}")
        self.stdout.write(f"  Total ₹:  {result['total_amount']}")

        for err in result["errors"][:50]:
            self.stdout.write(self.style.WARNING(f"  ! {err}"))
        if len(result["errors"]) > 50:
            self.stdout.write(self.style.WARNING(f"  ... and {len(result['errors']) - 50} more errors"))
