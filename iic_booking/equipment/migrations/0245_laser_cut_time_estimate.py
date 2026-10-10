"""Machine-time estimate for 2D laser / profile cutting (additive, nullable: safe before and after the deploy).

Equipment.laser_estimate_profile: machine type preset, parameter overrides and per-material cutting speeds.
LaserCutAnalysis.cut_features: cut path measured from the DXF (filled at upload; older parts by
``backfill_laser_cut_features``).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0244_equipment_flash_messages"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="laser_estimate_profile",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text=(
                    "For 2D laser / profile cutting equipment: machine type preset, parameter overrides and "
                    "per-material cutting speeds used for the machine-time estimate of uploaded DXF files. Blank "
                    "uses the preset detected from Make / Model / Name."
                ),
                null=True,
                verbose_name="Profile cutting time estimate profile",
            ),
        ),
        migrations.AddField(
            model_name="lasercutanalysis",
            name="cut_features",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Cut path measured from the DXF (drawing units) for the machine-time estimate.",
                null=True,
            ),
        ),
    ]
