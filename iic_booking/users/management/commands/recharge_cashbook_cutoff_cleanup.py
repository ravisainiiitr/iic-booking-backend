from django.core.management.base import BaseCommand

from iic_booking.users import wallet_cashbook_cutoff_cleanup


class Command(BaseCommand):
    help = "Report (and with --apply clear) cash-book matches from entries dated before the matching cutoff."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Clear pre-cutoff links on unapproved requests.")

    def handle(self, *args, **options):
        report = wallet_cashbook_cutoff_cleanup.run(apply=options["apply"])
        for key, value in report.items():
            self.stdout.write(f"{key}={value}")
