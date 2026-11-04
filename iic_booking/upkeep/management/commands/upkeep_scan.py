from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Raise and resolve upkeep alerts (maintenance due/overdue, uptime below target, repeated breakdowns, UPS)."

    def handle(self, *args, **options):
        from iic_booking.upkeep.service import scan

        self.stdout.write(str(scan()))
