# Generated manually for GENERIC charge profile fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0192_publication_impact_factor_assigned_reviewer"),
    ]

    operations = [
        migrations.AddField(
            model_name="chargeprofile",
            name="charge_formula",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "GENERIC: restricted expression for total charge. "
                    "Variables: pc (primary unit charge), sc (secondary unit charge), "
                    "A-Z input fields, TIME (minutes after time formula), SLOT_DURATION_MINUTES. "
                    "Supports comparisons and if/else expressions."
                ),
                verbose_name="Charge formula",
            ),
        ),
        migrations.AddField(
            model_name="chargeprofile",
            name="display_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Shown on the Charges by user category table for this user type.",
                verbose_name="Display text",
            ),
        ),
        migrations.AddField(
            model_name="multiparamdefinition",
            name="display_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Optional text shown for this option on the Charges by user category table.",
                verbose_name="Display text",
            ),
        ),
    ]
