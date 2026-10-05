import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0224_charge_copy_batch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="mode_availability",
            field=models.CharField(
                choices=[("ALWAYS", "Always available"), ("SCHEDULED_ONLY", "Only on scheduled days")],
                default="ALWAYS",
                help_text=(
                    "Only used when this equipment is a mode of a base instrument. "
                    "Always available: bookable unless a mutually exclusive schedule of another mode is active. "
                    "Only on scheduled days: bookable only while one of its mode schedules is active."
                ),
                max_length=20,
                verbose_name="Mode availability",
            ),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="parent_equipment",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "When set, this equipment is an alternate operating mode of the parent (base) instrument. "
                    "Managed from the Multi-mode equipment page; the base is flagged automatically. "
                    "Leave empty for standalone equipment or for the base/parent mode itself."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="mode_children",
                to="equipment.equipment",
                verbose_name="Parent Equipment (multi-mode)",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmodeschedule",
            name="weekdays",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Optional list of weekdays within the date range (0 = Monday … 6 = Sunday). "
                    "Empty means every day."
                ),
                verbose_name="Repeat on weekdays",
            ),
        ),
        migrations.CreateModel(
            name="EquipmentModeAuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("equipment_code", models.CharField(blank=True, default="", max_length=255)),
                ("action", models.CharField(db_index=True, max_length=40)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_mode_audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mode_audit_logs",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment Mode Audit Log",
                "verbose_name_plural": "Equipment Mode Audit Logs",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
