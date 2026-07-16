from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0159_chargeprofile_show_charge_breakdown"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingsampletrace",
            name="results_folder_path",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Filesystem path created when status is In Analysis (PROCESSING).",
                max_length=1000,
                verbose_name="Results folder path",
            ),
        ),
    ]
