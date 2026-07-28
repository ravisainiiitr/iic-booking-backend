# Generated manually — replace DepartmentSyncAgent.laboratory with equipment.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0180_equipment_gps_coordinates"),
        ("sync", "0013_production_hardening_rc"),
    ]

    operations = [
        migrations.AddField(
            model_name="departmentsyncagent",
            name="equipment",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Primary equipment for this agent. Choices are limited to the selected department. "
                    "Additional instruments can still be assigned via Equipment Sync Profiles."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="primary_sync_agents",
                to="equipment.equipment",
                verbose_name="Equipment",
            ),
        ),
        migrations.RemoveIndex(
            model_name="departmentsyncagent",
            name="sync_depart_laborat_441246_idx",
        ),
        migrations.RemoveField(
            model_name="departmentsyncagent",
            name="laboratory",
        ),
        migrations.AddIndex(
            model_name="departmentsyncagent",
            index=models.Index(fields=["equipment", "is_active"], name="sync_depart_equipme_idx"),
        ),
    ]
