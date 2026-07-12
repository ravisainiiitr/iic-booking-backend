# Add BookingStatus.HOLD and UrgentBookingRequest.hold_booking FK

from django.db import migrations, models
import django.db.models.deletion


def _booking_status_choices_with_hold():
    return [
        ("PENDING", "Pending"),
        ("BOOKED", "Booked"),
        ("HOLD", "Hold"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
        ("ABSENT", "Operator Unavailable"),
        ("REFUNDED", "Refunded"),
        ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
    ]


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0066_urgentbookingrequest_redesign"),
    ]

    operations = [
        migrations.AlterField(
            model_name="booking",
            name="status",
            field=models.CharField(
                choices=_booking_status_choices_with_hold(),
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
                choices=_booking_status_choices_with_hold(),
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
                choices=_booking_status_choices_with_hold(),
                help_text="New booking status (if status changed)",
                max_length=30,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="hold_booking",
            field=models.ForeignKey(
                blank=True,
                help_text="Booking in HOLD status linked to this urgent request; converted to BOOKED on approval",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="urgent_booking_request",
                to="equipment.booking",
                verbose_name="Hold booking",
            ),
        ),
    ]
