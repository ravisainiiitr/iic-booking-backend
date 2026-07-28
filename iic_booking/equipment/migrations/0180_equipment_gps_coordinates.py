from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0179_equipment_google_maps_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="latitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=7,
                help_text="Optional GPS latitude for the laboratory location",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="longitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=7,
                help_text="Optional GPS longitude for the laboratory location",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="google_maps_url",
            field=models.URLField(
                blank=True,
                help_text="Optional Google Maps URL for opening the exact lab location",
                max_length=500,
                null=True,
            ),
        ),
    ]
