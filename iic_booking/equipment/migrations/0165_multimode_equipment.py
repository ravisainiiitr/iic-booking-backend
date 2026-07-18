# Generated manually for multi-mode equipment hierarchy + schedules

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0164_accessory_is_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="parent_equipment",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "When set, this equipment is an alternate operating mode of the parent (base) instrument. "
                    "Leave empty for standalone equipment or for the base/parent mode itself."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="mode_children",
                to="equipment.equipment",
                verbose_name="Parent Equipment (multi-mode)",
            ),
        ),
        migrations.CreateModel(
            name="EquipmentModeSchedule",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("start_date", models.DateField(verbose_name="Start Date")),
                ("end_date", models.DateField(verbose_name="End Date")),
                (
                    "behavior",
                    models.CharField(
                        choices=[("PARALLEL", "Parallel"), ("EXCLUSIVE", "Mutually Exclusive")],
                        default="PARALLEL",
                        max_length=20,
                        verbose_name="Behavior",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_mode_schedules",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "mode_equipment",
                    models.ForeignKey(
                        help_text="Child mode equipment being enabled for the date range.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="as_mode_schedules",
                        to="equipment.equipment",
                        verbose_name="Mode Equipment",
                    ),
                ),
                (
                    "parent_equipment",
                    models.ForeignKey(
                        help_text="Base/parent instrument this schedule applies to.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mode_schedules",
                        to="equipment.equipment",
                        verbose_name="Parent Equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment Mode Schedule",
                "verbose_name_plural": "Equipment Mode Schedules",
                "ordering": ["-start_date", "-end_date", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="equipmentmodeschedule",
            index=models.Index(
                fields=["parent_equipment", "start_date", "end_date"],
                name="equipment_e_parent__7a1b2c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="equipmentmodeschedule",
            index=models.Index(
                fields=["mode_equipment", "start_date", "end_date"],
                name="equipment_e_mode_eq_8d3e4f_idx",
            ),
        ),
    ]
