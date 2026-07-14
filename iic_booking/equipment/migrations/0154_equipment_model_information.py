from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0153_equipment_print_3d_stl_notification_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="model_information",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    'Model information (e.g. "Sigma 300", "FE-SEM"). '
                    "Shown on equipment catalog cards when enabled."
                ),
                max_length=255,
                verbose_name="Model",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="show_model_on_card",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, Model is displayed on catalog cards "
                    "below Make / department information."
                ),
                verbose_name="Show Model on equipment card",
            ),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="show_make_on_card",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, Make is displayed on catalog cards "
                    "below department information."
                ),
                verbose_name="Show Make on equipment card",
            ),
        ),
    ]
