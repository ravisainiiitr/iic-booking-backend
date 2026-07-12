"""
Import IIC wallet recharge text file (e.g. IIC Wallet-27-02-2026.txt).

File has headers: SlNo, Dated, ReceiptNo, Credited to Project No., Amount(Rs), Payment Details, Received From, Remarks.
Received From contains EMP NO-100584 and optionally DEPT-OF HYDROLOGY.
Each receipt is credited to the user's sub-wallet (user matched by emp_id). Same ReceiptNo in same financial year is skipped (no double-credit).
"""

from django.core.management.base import BaseCommand

from iic_booking.users.wallet_recharge_parser import parse_wallet_recharge_file
from iic_booking.users.wallet_recharge_import import import_wallet_recharge_rows


class Command(BaseCommand):
    help = "Import IIC wallet recharge text file; credit sub-wallets by emp_id from 'Received From'"

    def add_arguments(self, parser):
        parser.add_argument(
            "file",
            help="Path to the wallet recharge text file (e.g. IIC Wallet-27-02-2026.txt)",
        )
        parser.add_argument(
            "--delimiter",
            choices=["tab", "comma"],
            default=None,
            help="Column delimiter (default: auto-detect from first line)",
        )
        parser.add_argument(
            "--default-department",
            type=int,
            default=None,
            metavar="ID",
            help="Internal department ID to credit when receipt has no dept hint; used before falling back to user's HR department",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate only; do not credit or create import records",
        )

    def handle(self, *args, **options):
        path = options["file"]
        delimiter = {"tab": "\t", "comma": ","}.get(options["delimiter"]) if options["delimiter"] else None
        default_department_id = options["default_department"]
        dry_run = options["dry_run"]

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(f"File not found: {path}"))
            raise SystemExit(1)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Could not read file: {e}"))
            raise SystemExit(1)

        rows = parse_wallet_recharge_file(content, delimiter=delimiter)
        if not rows:
            self.stdout.write(self.style.WARNING("No rows parsed. Check file format (header: SlNo, Dated, ReceiptNo, Amount(Rs), Received From, ...)."))
            return

        self.stdout.write(self.style.SUCCESS(f"Parsed {len(rows)} row(s)."))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run – no credits or records will be created."))

        credited, skipped, errors, _ = import_wallet_recharge_rows(
            rows,
            default_department_id=default_department_id,
            dry_run=dry_run,
        )

        for msg in errors:
            self.stdout.write(self.style.WARNING(msg))
        self.stdout.write(self.style.SUCCESS(f"Credited: {credited}, Skipped: {skipped}."))
