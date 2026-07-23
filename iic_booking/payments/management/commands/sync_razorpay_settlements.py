from django.core.management.base import BaseCommand

from iic_booking.payments.razorpay_service import RazorpayNotConfigured, RazorpayServiceError, sync_settlements


class Command(BaseCommand):
    help = "Sync Razorpay settlements into PaymentSettlement for bank UTR reconciliation."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Lookback hint (API may ignore).")

    def handle(self, *args, **options):
        try:
            count = sync_settlements(days=options["days"])
        except RazorpayNotConfigured as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return
        except RazorpayServiceError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return
        self.stdout.write(self.style.SUCCESS(f"Upserted {count} settlement(s)."))
