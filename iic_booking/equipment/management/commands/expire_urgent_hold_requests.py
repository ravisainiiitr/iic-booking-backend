"""
Expire urgent request holds that have exceeded the configured hold_expiry_hours.
Run periodically via cron (e.g. every hour):
  python manage.py expire_urgent_hold_requests
"""
from django.core.management.base import BaseCommand
from iic_booking.equipment.api_views import expire_urgent_hold_requests


class Command(BaseCommand):
    help = "Expire PENDING urgent requests with hold bookings that are past hold_expiry_hours; release hold and set status to EXPIRED."

    def handle(self, *args, **options):
        count = expire_urgent_hold_requests()
        if count > 0:
            self.stdout.write(self.style.SUCCESS(f"Expired {count} urgent hold request(s)."))
        else:
            self.stdout.write("No urgent hold requests to expire.")
