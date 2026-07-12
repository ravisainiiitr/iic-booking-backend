"""Probe weekly/monthly quota for a user + equipment without creating a booking.

Usage:
  python manage.py probe_booking_quota --email user@iitr.ac.in --equipment-id 1 --minutes 60
  python manage.py probe_booking_quota --email user@iitr.ac.in --equipment-code GEM --minutes 90
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from iic_booking.equipment.models import Equipment, QuotaType
from iic_booking.equipment.quota_utils import QuotaChecker, booking_quota_should_skip
from iic_booking.users.models import User


class Command(BaseCommand):
    help = "Check whether a hypothetical booking would pass weekly/monthly quota limits."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="User email")
        parser.add_argument("--equipment-id", type=int, default=None)
        parser.add_argument("--equipment-code", default=None)
        parser.add_argument("--minutes", type=int, default=60, help="Additional minutes to book")
        parser.add_argument("--bookings", type=int, default=1, help="Additional booking count")
        parser.add_argument(
            "--charge",
            type=str,
            default="0",
            help="Additional charge (for CHARGE-type equipment quotas)",
        )
        parser.add_argument(
            "--booking-date",
            default=None,
            help="Optional ISO datetime for period anchor (default: now). e.g. 2026-08-01T00:30:00",
        )

    def handle(self, *args, **options):
        from decimal import Decimal
        from django.conf import settings

        email = options["email"].strip()
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user with email {email}") from exc

        if options["equipment_id"]:
            equipment = Equipment.objects.filter(equipment_id=options["equipment_id"]).first()
        elif options["equipment_code"]:
            equipment = Equipment.objects.filter(code__iexact=options["equipment_code"]).first()
        else:
            raise CommandError("Provide --equipment-id or --equipment-code")
        if not equipment:
            raise CommandError("Equipment not found")

        minutes = int(options["minutes"])
        bookings = int(options["bookings"])
        charge = Decimal(str(options["charge"] or "0"))

        if options["booking_date"]:
            raw = options["booking_date"].replace("Z", "+00:00")
            booking_date = datetime.fromisoformat(raw)
            if timezone.is_naive(booking_date):
                booking_date = timezone.make_aware(
                    booking_date, timezone.get_current_timezone()
                )
        else:
            booking_date = timezone.now()

        self.stdout.write(self.style.NOTICE(
            f"User={user.email} type={user.user_type} | Equipment={equipment.code} "
            f"group={getattr(equipment.equipment_group, 'name', None)} "
            f"skip_equipment={equipment.skip_quota_check}"
        ))
        self.stdout.write(self.style.NOTICE(
            f"SKIP_BOOKING_QUOTA_CHECK={getattr(settings, 'SKIP_BOOKING_QUOTA_CHECK', None)} "
            f"| booking_quota_should_skip={booking_quota_should_skip(equipment)}"
        ))
        self.stdout.write(self.style.NOTICE(
            f"Probe +{minutes} min / +{bookings} booking / +INR {charge} at {timezone.localtime(booking_date)}"
        ))

        if booking_quota_should_skip(equipment):
            self.stdout.write(self.style.WARNING(
                "Quota checks are SKIPPED for this equipment/settings — booking API will not enforce limits."
            ))
            self.stdout.write(
                "Set SKIP_BOOKING_QUOTA_CHECK=0 in .env and ensure equipment.skip_quota_check=False to enforce."
            )

        for quota_type in (QuotaType.WEEKLY, QuotaType.MONTHLY):
            start, end = QuotaChecker._get_quota_period(quota_type, booking_date)
            ok, err = QuotaChecker.check_user_quota(
                user=user,
                equipment=equipment,
                quota_type=quota_type,
                additional_time_minutes=minutes,
                additional_bookings=bookings,
                additional_charge=charge,
                booking_date=booking_date,
            )
            period = f"{timezone.localtime(start)} → {timezone.localtime(end)}"
            if ok:
                self.stdout.write(self.style.SUCCESS(f"{quota_type}: ALLOW  period={period}"))
            else:
                self.stdout.write(self.style.ERROR(f"{quota_type}: BLOCK  {err}"))
                self.stdout.write(f"  period={period}")
