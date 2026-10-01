import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0215_booking_input_edit_payment_window"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingSlotRange",
            fields=[
                (
                    "booking",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="released_slot_range",
                        serialize=False,
                        to="equipment.booking",
                    ),
                ),
                ("start_datetime", models.DateTimeField(blank=True, null=True)),
                ("end_datetime", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Booking slot range",
                "verbose_name_plural": "Booking slot ranges",
            },
        ),
    ]
