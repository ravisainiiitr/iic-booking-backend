"""Celery / management entry for lab health detectors."""

from django.core.management.base import BaseCommand

from iic_booking.lab_infrastructure.services.detectors import run_health_detectors


class Command(BaseCommand):
    help = "Run Laboratory Infrastructure health detectors (offline, drift, disk, duplicates)."

    def handle(self, *args, **options):
        result = run_health_detectors()
        self.stdout.write(self.style.SUCCESS(str(result)))
