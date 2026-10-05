from datetime import date

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Expire lapsed AMC / service contracts and send one-time expiry reminders (Procurement & Assets)."

    def add_arguments(self, parser):
        parser.add_argument("--today", type=date.fromisoformat, help="Evaluate as of this date (YYYY-MM-DD).")

    def handle(self, *args, **opts):
        from iic_booking.procurement_management.amc import send_reminders

        result = send_reminders(today=opts.get("today"))
        self.stdout.write(f"expired={result['expired']} reminded={result['reminded']}")
