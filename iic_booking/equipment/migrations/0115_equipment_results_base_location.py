from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0114_equipment_booking_not_utilize_window_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="results_base_location",
            field=models.CharField(
                default="D:\\Results",
                help_text='Base folder where sample-analysis folders are created when Sample Lifecycle moves to "In Analysis".',
                max_length=500,
                verbose_name="Results Base Location",
            ),
        ),
    ]

