# Add actual weight/time fields for post-print charge adjustment (Admin/OIC).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0148_seed_sample_print_3d_equipment"),
    ]

    operations = [
        migrations.AddField(
            model_name="printanalysis",
            name="actual_weight_grams",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Post-print actual filament weight (g), set by Admin/OIC for charge adjustment.",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="actual_time_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Post-print actual machine time (min), set by Admin/OIC for charge adjustment.",
                null=True,
            ),
        ),
    ]
