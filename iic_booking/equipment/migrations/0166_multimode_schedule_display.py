# Generated manually for multi-mode schedule display fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0165_multimode_equipment"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="start_time",
            field=models.TimeField(
                blank=True,
                help_text="Optional. If set with end time, only slots within this daily window are mode-active.",
                null=True,
                verbose_name="Start Time",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="end_time",
            field=models.TimeField(
                blank=True,
                help_text="Optional. If set with start time, only slots within this daily window are mode-active.",
                null=True,
                verbose_name="End Time",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="unavailable_label",
            field=models.CharField(
                blank=True,
                default="Mode not scheduled",
                help_text="Shown on child mode slots outside the configured schedule window.",
                max_length=120,
                verbose_name="Unavailable Status Label",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="unavailable_color",
            field=models.CharField(
                blank=True,
                default="#9ca3af",
                help_text="Background color for child slots outside the schedule (default grey).",
                max_length=20,
                verbose_name="Unavailable Background Color",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="exclusive_blocked_label",
            field=models.CharField(
                blank=True,
                default="Alternate mode active",
                help_text="Shown on parent/base slots while a mutually exclusive child mode is active.",
                max_length=120,
                verbose_name="Blocked Slot Label (exclusive)",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="exclusive_blocked_color",
            field=models.CharField(
                blank=True,
                default="#9ca3af",
                help_text="Background color for parent slots during exclusive mode (default grey).",
                max_length=20,
                verbose_name="Blocked Background Color (exclusive)",
            ),
        ),
    ]
