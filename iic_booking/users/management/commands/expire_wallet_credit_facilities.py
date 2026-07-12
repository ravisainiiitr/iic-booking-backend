"""Mark expired faculty recharge credit windows and notify wallet owners (run via cron)."""

from django.core.management.base import BaseCommand

from iic_booking.users.wallet_credit_facility import expire_due_wallet_credit_facilities


class Command(BaseCommand):
    help = "Expire wallet recharge credit facilities past their window; email faculty if parse credit missing."

    def handle(self, *args, **options):
        n = expire_due_wallet_credit_facilities()
        self.stdout.write(self.style.SUCCESS(f"Expired {n} wallet recharge credit facility window(s)."))
