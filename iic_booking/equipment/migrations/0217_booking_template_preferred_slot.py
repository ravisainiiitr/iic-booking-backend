import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0216_booking_slot_range"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="preferred_weekday",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Preferred slot weekday (0 = Monday … 6 = Sunday); pre-selected when the template is loaded.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="preferred_start_time",
            field=models.TimeField(blank=True, help_text="Preferred slot start time (local).", null=True),
        ),
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="preferred_slot_count",
            field=models.PositiveSmallIntegerField(
                blank=True, help_text="Number of consecutive slots in the preferred slot.", null=True
            ),
        ),
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="preferred_slot_master",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional slot definition the preferred slot was picked from.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="equipment.slotmaster",
            ),
        ),
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="if_slot_taken",
            field=models.CharField(
                choices=[
                    ("ask", "Ask me"),
                    ("next_available_same_day", "Book the next available slot the same day"),
                    ("next_available_any", "Book the next available slot in the open booking window"),
                ],
                db_default="ask",
                default="ask",
                help_text="What to do at submit time when the preferred slot has just been taken.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="bookinginputtemplate",
            name="if_slot_taken_consented_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user agreed to automatic booking of the next available slot.",
                null=True,
            ),
        ),
    ]
