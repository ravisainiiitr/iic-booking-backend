from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('training', '0003_module_settings_equipment_enable'),
    ]

    operations = [
        migrations.AddField(
            model_name='trainingmodulesettings',
            name='course_demos_free',
            field=models.BooleanField(default=False, help_text="Course/curricular demonstrations are free. Off: every demonstration is charged at the equipment's internal IITR rate and deducted from the faculty member's wallet."),
        ),
    ]
