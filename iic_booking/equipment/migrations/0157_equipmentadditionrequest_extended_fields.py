# Generated manually for EquipmentAdditionRequest expanded fields

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0156_equipmentadditionrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="year_of_installation",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="specifications",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="sample_requirements",
            field=models.TextField(
                blank=True,
                default="",
                verbose_name="Sample requirements and preparation",
            ),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="slots_per_day",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="slot_duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="slot_start_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="slot_end_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_calculation_basis",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="time_calculation_basis",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_iitr_student",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_iitr_faculty",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_external_educational_student",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_external_govt_rnd",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_industry",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_startup_incubated_iitr",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="charge_external_startup_msme",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="equipment_image",
            field=models.ImageField(
                blank=True,
                max_length=512,
                null=True,
                upload_to="equipment_addition_requests/images/%Y/%m/%d/",
                verbose_name="Equipment image",
            ),
        ),
        migrations.AddField(
            model_name="equipmentadditionrequest",
            name="supporting_document",
            field=models.FileField(
                blank=True,
                max_length=512,
                null=True,
                upload_to="equipment_addition_requests/documents/%Y/%m/%d/",
                verbose_name="Supporting document",
            ),
        ),
        migrations.AlterField(
            model_name="equipmentadditionrequest",
            name="internal_department",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={
                    "department_type": "internal",
                    "internal_subcategory": "iit_roorkee_dept_centres",
                },
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="equipment_addition_requests",
                to="users.department",
                verbose_name="Internal department",
            ),
        ),
    ]
