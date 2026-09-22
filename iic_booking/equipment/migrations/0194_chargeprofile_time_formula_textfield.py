# Generated manually: widen time_formula for statement scripts

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0193_generic_charge_profile_formulas"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chargeprofile",
            name="time_formula",
            field=models.TextField(
                blank=True,
                help_text=(
                    'Formula for time calculation. SAMPLE/HOUR may use a single expression '
                    '(e.g. "(A * C) + B"). GENERIC: restricted Python script — assign time '
                    '(minutes), e.g. "time = A * SLOT_DURATION_MINUTES" with if/else or for-loops. '
                    'HOUR: leave blank or set to "B" for legacy B×slot-duration behavior. '
                    'A single expression is still accepted (legacy).'
                ),
                null=True,
            ),
        ),
    ]