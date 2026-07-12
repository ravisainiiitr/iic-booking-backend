# Generated manually to add BookingStatus.UNDER_MAINTENANCE

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0119_booking_disruption_pending"),
    ]

    operations = [
        migrations.AlterField(
            model_name="booking",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("WAITLISTED", "Waitlisted"),
                    ("BOOKED", "Booked"),
                    ("DISRUPTION_PENDING", "Awaiting your choice (disruption)"),
                    ("UNDER_MAINTENANCE", "Under Maintenance"),
                    ("HOLD", "Hold"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Operator Unavailable"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                default="PENDING",
                help_text="Booking status",
                max_length=30,
            ),
        ),
    ]

