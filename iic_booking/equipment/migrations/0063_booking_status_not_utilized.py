# Add BookingStatus.BOOKING_NOT_UTILIZED and increase status max_length

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0062_booking_not_utilized_schedule"),
    ]

    operations = [
        migrations.AlterField(
            model_name="booking",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("BOOKED", "Booked"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Absent"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                default="PENDING",
                help_text="Booking status",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="bookingevent",
            name="previous_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING", "Pending"),
                    ("BOOKED", "Booked"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Absent"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                help_text="Previous booking status (if status changed)",
                max_length=30,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="bookingevent",
            name="new_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING", "Pending"),
                    ("BOOKED", "Booked"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Absent"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                help_text="New booking status (if status changed)",
                max_length=30,
                null=True,
            ),
        ),
    ]
