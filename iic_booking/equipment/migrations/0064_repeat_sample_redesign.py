# Repeat sample redesign: enable flag on Booking, source_booking for repeat bookings, new event types

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0063_booking_status_not_utilized"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="repeat_sample_enabled",
            field=models.BooleanField(
                default=False,
                help_text="If True, admin/OIC has enabled repeat sample for this completed booking; the user can create one replica booking.",
                verbose_name="Repeat sample enabled",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="source_booking",
            field=models.ForeignKey(
                blank=True,
                help_text="If set, this booking is a repeat sample of the source booking; excluded from weekly/monthly limits.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="repeat_bookings",
                to="equipment.booking",
                verbose_name="Source booking (repeat of)",
            ),
        ),
        migrations.AlterField(
            model_name="bookingevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("CREATED", "Created"),
                    ("CONFIRMED", "Confirmed"),
                    ("CANCELLED", "Cancelled"),
                    ("RESCHEDULED", "Rescheduled"),
                    ("COMPLETED", "Completed"),
                    ("REFUNDED", "Refunded"),
                    ("ABSENT", "Operator Unavailable"),
                    ("COMMENT", "Comment"),
                    ("STATUS_CHANGED", "Status Changed"),
                    ("CHARGE_RECALCULATED", "Charge Recalculated"),
                    ("REPEAT_SAMPLE_OFFERED", "Repeat sample offered"),
                    ("REPEAT_SAMPLE_CREATED", "Repeat sample booking created"),
                ],
                max_length=32,
            ),
        ),
    ]
