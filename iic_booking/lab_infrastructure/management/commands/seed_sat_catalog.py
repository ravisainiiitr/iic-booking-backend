"""Seed Phase 2.5 SAT catalog into the database."""

from django.core.management.base import BaseCommand

from iic_booking.lab_infrastructure.services.testing import ensure_catalog
from iic_booking.lab_infrastructure.models import SatTestCase


class Command(BaseCommand):
    help = "Seed / refresh the SAT test case catalog for the Main Admin Test Dashboard."

    def handle(self, *args, **options):
        created = ensure_catalog()
        total = SatTestCase.objects.filter(is_active=True).count()
        self.stdout.write(self.style.SUCCESS(f"Catalog ready: {total} cases ({created} newly created)."))
