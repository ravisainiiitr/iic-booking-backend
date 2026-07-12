from django.db import migrations, models
from django.db.models import Q


def backfill_weekend_holiday_available_to_not_available(apps, schema_editor):
    """Legacy rows: admin grid used allow_holiday=True so weekend/holiday slots were created as AVAILABLE."""
    DailySlot = apps.get_model("equipment", "DailySlot")
    Holiday = apps.get_model("equipment", "Holiday")
    holiday_dates = Holiday.objects.filter(is_active=True).values_list("date", flat=True)
    DailySlot.objects.filter(
        status="AVAILABLE",
        booking__isnull=True,
        reserved_for_external=False,
    ).filter(Q(date__week_day=1) | Q(date__week_day=7) | Q(date__in=holiday_dates)).update(
        status="NOT_AVAILABLE"
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0126_equipment_auto_slot_selection_default"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dailyslot",
            name="status",
            field=models.CharField(
                choices=[
                    ("AVAILABLE", "Available"),
                    ("NOT_AVAILABLE", "Not Available"),
                    ("BOOKED", "Booked"),
                    ("BLOCKED", "Blocked"),
                    ("UNDER_MAINTENANCE", "Under Maintenance"),
                    ("OPERATOR_ABSENT", "Operator Absent"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                default="AVAILABLE",
                help_text="Availability status of this slot",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_weekend_holiday_available_to_not_available, noop_reverse),
    ]
