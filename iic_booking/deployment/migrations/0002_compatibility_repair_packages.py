# Phase 2 Deployment Center compatibility + repair packages

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("deployment", "0001_equipment_pc_wizard_release"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentpcwizardrelease",
            name="compatibility",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='e.g. {"min_portal":"1.0","min_dsa":"1.0","min_raa":"1.0"}',
            ),
        ),
        migrations.AddField(
            model_name="equipmentpcwizardrelease",
            name="rollback_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="rollbacks",
                to="deployment.equipmentpcwizardrelease",
            ),
        ),
        migrations.AddField(
            model_name="equipmentpcwizardrelease",
            name="repair_file",
            field=models.FileField(
                blank=True,
                max_length=512,
                null=True,
                upload_to="equipment_pc_wizard/%Y/%m/%d/repair/",
            ),
        ),
        migrations.AddField(
            model_name="equipmentpcwizardrelease",
            name="emergency_file",
            field=models.FileField(
                blank=True,
                max_length=512,
                null=True,
                upload_to="equipment_pc_wizard/%Y/%m/%d/emergency/",
            ),
        ),
    ]
